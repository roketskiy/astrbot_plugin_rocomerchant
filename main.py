from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

try:
    from .merchant_data import (
        DEFAULT_PUSH_TIMES,
        FALLBACK_URL,
        PRIMARY_URL,
        current_beijing_datetime,
        current_beijing_schedule,
        current_round_slot,
        load_snapshot,
        normalize_push_times,
        normalize_payload,
        render_message_text,
        save_snapshot,
    )
except ImportError:
    from merchant_data import (
        DEFAULT_PUSH_TIMES,
        FALLBACK_URL,
        PRIMARY_URL,
        current_beijing_datetime,
        current_beijing_schedule,
        current_round_slot,
        load_snapshot,
        normalize_push_times,
        normalize_payload,
        render_message_text,
        save_snapshot,
    )

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:  # pragma: no cover - 兼容较旧版本或本地静态检查
    get_astrbot_data_path = None


PLUGIN_NAME = "astrbot_plugin_rocomerchant"
SNAPSHOT_FILENAME = "latest.json"
SUBSCRIPTIONS_KEY = "subscriptions"
PUSH_STATE_PREFIX = "push_state:"
LAST_SENT_SLOT_PREFIX = "last_sent_slot:"


@dataclass(slots=True)
class PluginConfigView:
    enable_push: bool = True
    push_times: tuple[str, ...] = DEFAULT_PUSH_TIMES
    retry_interval_minutes: int = 3
    max_retry_attempts: int = 4
    request_timeout_seconds: int = 10
    snapshot_fallback_for_now: bool = True
    message_header: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "PluginConfigView":
        data = dict(raw or {})
        legacy_first_push_delay_minutes = max(
            0, int(data.get("first_push_delay_minutes", 10))
        )
        return cls(
            enable_push=bool(data.get("enable_push", True)),
            push_times=normalize_push_times(
                data.get("push_times"),
                first_push_delay_minutes=legacy_first_push_delay_minutes,
            ),
            retry_interval_minutes=max(1, int(data.get("retry_interval_minutes", 3))),
            max_retry_attempts=max(0, int(data.get("max_retry_attempts", 4))),
            request_timeout_seconds=max(1, int(data.get("request_timeout_seconds", 10))),
            snapshot_fallback_for_now=bool(data.get("snapshot_fallback_for_now", True)),
            message_header=str(data.get("message_header", "") or ""),
        )


class MerchantStore:
    def __init__(self, star: "RocoMerchantPlugin") -> None:
        self.star = star

    @staticmethod
    def encode_umo(umo: str) -> str:
        encoded = base64.urlsafe_b64encode(umo.encode("utf-8")).decode("ascii")
        return encoded.rstrip("=")

    async def list_subscriptions(self) -> list[dict[str, Any]]:
        subscriptions = await self.star.get_kv_data(SUBSCRIPTIONS_KEY, [])
        return subscriptions if isinstance(subscriptions, list) else []

    async def is_subscribed(self, umo: str) -> bool:
        subscriptions = await self.list_subscriptions()
        return any(item.get("umo") == umo and item.get("enabled", True) for item in subscriptions)

    async def add_subscription(self, umo: str) -> bool:
        subscriptions = await self.list_subscriptions()
        if any(item.get("umo") == umo for item in subscriptions):
            return False

        subscriptions.append({"umo": umo, "enabled": True})
        await self.star.put_kv_data(SUBSCRIPTIONS_KEY, subscriptions)
        return True

    async def remove_subscription(self, umo: str) -> bool:
        subscriptions = await self.list_subscriptions()
        filtered = [item for item in subscriptions if item.get("umo") != umo]
        changed = len(filtered) != len(subscriptions)
        if changed:
            await self.star.put_kv_data(SUBSCRIPTIONS_KEY, filtered)
            await self.star.delete_kv_data(f"{LAST_SENT_SLOT_PREFIX}{self.encode_umo(umo)}")
        return changed

    async def get_last_sent_slot(self, umo: str) -> str | None:
        return await self.star.get_kv_data(
            f"{LAST_SENT_SLOT_PREFIX}{self.encode_umo(umo)}",
            None,
        )

    async def set_last_sent_slot(self, umo: str, slot: str) -> None:
        await self.star.put_kv_data(f"{LAST_SENT_SLOT_PREFIX}{self.encode_umo(umo)}", slot)

    async def get_push_state(self, slot: str) -> dict[str, Any]:
        state = await self.star.get_kv_data(f"{PUSH_STATE_PREFIX}{slot}", {})
        return state if isinstance(state, dict) else {}

    async def set_push_state(self, slot: str, state: dict[str, Any]) -> None:
        await self.star.put_kv_data(f"{PUSH_STATE_PREFIX}{slot}", state)


class RocoMerchantPlugin(Star):
    _BACKGROUND_REGISTRY_KEY = "_astrbot_plugin_rocomerchant_background_tasks"

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._instance_id = f"{id(self):x}"
        self.context = context
        self.config = PluginConfigView.from_raw(config)
        self.store = MerchantStore(self)
        self.http_client = httpx.AsyncClient(
            timeout=self.config.request_timeout_seconds,
            headers={
                "User-Agent": "astrbot-plugin-rocomerchant/1.0",
                "Accept": "application/json, text/plain, */*",
            },
        )
        self._scheduler_task: asyncio.Task[None] | None = None
        self.snapshot_path = self._build_snapshot_path()
        try:
            self._cancel_stale_background_tasks()
            self._ensure_scheduler_started()
        except RuntimeError:
            logger.warning("rocomerchant: 初始化时未拿到运行中的事件循环，将在后续时机重试启动调度器")

    def _build_snapshot_path(self) -> Path:
        if get_astrbot_data_path is not None:
            return Path(get_astrbot_data_path()) / "plugin_data" / self.name / SNAPSHOT_FILENAME
        return Path("data") / "plugin_data" / getattr(self, "name", PLUGIN_NAME) / SNAPSHOT_FILENAME

    def _background_task_registry(self) -> dict[str, asyncio.Task[None]]:
        loop = asyncio.get_running_loop()
        registry = getattr(loop, self._BACKGROUND_REGISTRY_KEY, None)
        if not isinstance(registry, dict):
            registry = {}
            setattr(loop, self._BACKGROUND_REGISTRY_KEY, registry)
        return registry

    def _cancel_stale_background_tasks(self) -> None:
        registry = self._background_task_registry()
        for name, task in list(registry.items()):
            if task and not task.done():
                logger.warning("rocomerchant: 取消旧后台任务 %s", name)
                task.cancel()
        registry.clear()

    def _register_background_task(
        self,
        name: str,
        coro: Any,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(
            coro,
            name=f"rocomerchant:{name}:{self._instance_id}",
        )
        self._background_task_registry()[name] = task
        return task

    def _unregister_background_task(
        self,
        name: str,
        task: asyncio.Task[None] | None,
    ) -> None:
        if not task:
            return
        registry = self._background_task_registry()
        if registry.get(name) is task:
            registry.pop(name, None)

    def _ensure_scheduler_started(self) -> None:
        if not self.config.enable_push:
            return

        registry = self._background_task_registry()
        current_task = registry.get("push_scheduler")
        if current_task and not current_task.done():
            self._scheduler_task = current_task
            return

        self._scheduler_task = self._register_background_task(
            "push_scheduler",
            self._scheduler_loop(),
        )
        logger.info(
            "rocomerchant: 定时推送任务已启动，触发时刻=%s，instance=%s",
            " / ".join(self.config.push_times),
            self._instance_id,
        )

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        self._ensure_scheduler_started()

    async def terminate(self):
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._unregister_background_task("push_scheduler", self._scheduler_task)
            self._scheduler_task = None
        await self.http_client.aclose()

    def _push_check_times(self, now: datetime | None = None) -> list[datetime]:
        current = current_beijing_datetime(now)
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        times: list[datetime] = []
        for value in self.config.push_times:
            hour, minute = map(int, value.split(":"))
            times.append(
                day_start.replace(
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0,
                )
            )
        return times

    def _next_push_check_time(self, now: datetime | None = None) -> datetime:
        current = current_beijing_datetime(now)
        for check_time in self._push_check_times(current):
            if check_time > current:
                return check_time
        next_day = current + timedelta(days=1)
        return self._push_check_times(next_day)[0]

    async def _scheduler_loop(self) -> None:
        logger.info("rocomerchant: 定时推送循环任务已启动（instance=%s）", self._instance_id)
        while True:
            try:
                now = current_beijing_datetime()
                next_check = self._next_push_check_time(now)
                sleep_seconds = max(1, (next_check - now).total_seconds())
                logger.info(
                    "rocomerchant: 下次推送检查时间 %s（instance=%s）",
                    next_check.strftime("%Y-%m-%d %H:%M:%S"),
                    self._instance_id,
                )
                await asyncio.sleep(sleep_seconds)
                await self._run_push_window()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rocomerchant: 定时推送循环异常")
                await asyncio.sleep(60)

    async def _run_push_window(self) -> None:
        window_key = current_beijing_datetime().strftime("%Y-%m-%d %H:%M")
        for retry_index in range(self.config.max_retry_attempts + 1):
            if retry_index > 0:
                delay_seconds = max(1, self.config.retry_interval_minutes * 60)
                logger.warning(
                    "rocomerchant: 本轮推送等待 %s 秒后执行第 %s 次重试",
                    delay_seconds,
                    retry_index,
                )
                await asyncio.sleep(delay_seconds)

            status = await self._run_push_attempt(retry_index, window_key)
            if status != "retry":
                return

        logger.warning("rocomerchant: 当前轮次重试仍未成功，结束本轮推送窗口")

    async def _run_push_attempt(self, attempt_index: int, window_key: str) -> str:
        schedule = current_beijing_schedule()
        slot = current_round_slot()
        if schedule["status"] != "open" or schedule["round"] is None or slot is None:
            logger.info("rocomerchant: 当前不在开放轮次，跳过本次尝试")
            return "done"

        subscriptions = await self.store.list_subscriptions()
        enabled_umos: list[str] = []
        pending_umos: list[str] = []
        for item in subscriptions:
            umo = item.get("umo")
            if not isinstance(umo, str) or not umo or not item.get("enabled", True):
                continue
            enabled_umos.append(umo)
            last_sent_slot = await self.store.get_last_sent_slot(umo)
            if last_sent_slot != slot:
                pending_umos.append(umo)

        push_state = await self.store.get_push_state(slot)
        if not pending_umos:
            if push_state.get("completed") is True:
                logger.info(
                    "rocomerchant: 槽位 %s 已完成推送，跳过本次尝试（订阅数=%s，待推送=0）",
                    slot,
                    len(enabled_umos),
                )
                return "done"

            logger.info(
                "rocomerchant: 当前槽位没有待推送订阅，跳过本次调度（订阅数=%s）",
                len(enabled_umos),
            )
            await self.store.set_push_state(
                slot,
                {
                    "last_attempt_index": attempt_index,
                    "window_key": window_key,
                    "completed": True,
                },
            )
            return "done"

        last_attempt_index = int(push_state.get("last_attempt_index", -1))
        if push_state.get("window_key") == window_key and attempt_index <= last_attempt_index:
            logger.info(
                "rocomerchant: 槽位 %s 已在触发窗口 %s 完成第 %s 次尝试，当前无需重复执行",
                slot,
                window_key,
                last_attempt_index + 1,
            )
            return "done"

        logger.info(
            "rocomerchant: 开始执行槽位 %s 的第 %s 次推送尝试，订阅数=%s，待推送订阅数=%s",
            slot,
            attempt_index + 1,
            len(enabled_umos),
            len(pending_umos),
        )

        await self.store.set_push_state(
            slot,
            {
                "last_attempt_index": attempt_index,
                "window_key": window_key,
                "completed": False,
            },
        )

        try:
            data = await self.fetch_merchant_data()
        except Exception as exc:
            logger.warning("rocomerchant: 槽位 %s 抓取失败，等待重试: %s", slot, exc)
            return "retry"

        if not data["schedule_check"]["matches_expected_schedule"]:
            logger.warning("rocomerchant: 当前数据未匹配预期轮次，等待下次重试")
            return "retry"

        chain = MessageChain().message(
            render_message_text(data, message_header=self.config.message_header)
        )
        sent_count = 0
        failed_count = 0
        for umo in pending_umos:
            try:
                sent = await self.context.send_message(umo, chain)
                if not sent:
                    logger.warning("rocomerchant: 会话 %s 未找到可用平台，跳过标记成功", umo)
                    failed_count += 1
                    continue
                await self.store.set_last_sent_slot(umo, slot)
                sent_count += 1
            except Exception:
                failed_count += 1
                logger.exception("rocomerchant: 向会话 %s 推送失败", umo)

        if sent_count == len(pending_umos):
            logger.info(
                "rocomerchant: 槽位 %s 推送完成，成功会话数=%s，失败会话数=%s",
                slot,
                sent_count,
                failed_count,
            )
            await self.store.set_push_state(
                slot,
                {
                    "last_attempt_index": attempt_index,
                    "window_key": window_key,
                    "completed": True,
                },
            )
            return "done"

        if sent_count > 0:
            logger.warning(
                "rocomerchant: 槽位 %s 部分推送成功，成功会话数=%s，失败会话数=%s，将继续重试未成功会话",
                slot,
                sent_count,
                failed_count,
            )
            return "retry"

        logger.warning("rocomerchant: 槽位 %s 本次未成功推送到任何会话，将等待下次重试", slot)
        return "retry"

    async def fetch_merchant_data(self) -> dict[str, Any]:
        errors: list[str] = []
        for url in (PRIMARY_URL, FALLBACK_URL):
            try:
                response = await self.http_client.get(url)
                response.raise_for_status()
                payload = response.json()
                normalized = normalize_payload(payload, source_url=url)
                save_snapshot(normalized, self.snapshot_path)
                return normalized
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{url}: {exc}")
        raise RuntimeError("两个接口都抓取失败:\n" + "\n".join(errors))

    async def reply_now_text(self) -> str:
        try:
            data = await self.fetch_merchant_data()
            return render_message_text(data, message_header=self.config.message_header)
        except Exception:
            logger.exception("rocomerchant: 手动查询抓取失败")
            if not self.config.snapshot_fallback_for_now:
                return "远行商人数据抓取失败，请稍后再试。"

            snapshot = load_snapshot(self.snapshot_path)
            if snapshot is None:
                return "远行商人数据抓取失败，且没有可用缓存。"
            return render_message_text(
                snapshot,
                message_header=self.config.message_header,
                stale=True,
            )

    @filter.command_group("merchant")
    def merchant(self):
        pass

    @merchant.command("now")
    async def merchant_now(self, event: AstrMessageEvent):
        self._ensure_scheduler_started()
        yield event.plain_result(await self.reply_now_text())

    @merchant.command("subscribe")
    async def merchant_subscribe(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        if not isinstance(umo, str) or not umo:
            yield event.plain_result("当前会话不支持订阅。")
            return

        created = await self.store.add_subscription(umo)
        self._ensure_scheduler_started()
        if created:
            yield event.plain_result("当前会话已订阅远行商人定时推送。")
            return
        yield event.plain_result("当前会话已订阅，无需重复操作。")

    @merchant.command("unsubscribe")
    async def merchant_unsubscribe(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        if not isinstance(umo, str) or not umo:
            yield event.plain_result("当前会话不支持退订。")
            return

        removed = await self.store.remove_subscription(umo)
        if removed:
            yield event.plain_result("当前会话已取消订阅远行商人定时推送。")
            return
        yield event.plain_result("当前会话尚未订阅。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @merchant.command("status")
    async def merchant_status(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        subscriptions = await self.store.list_subscriptions()
        subscribed = isinstance(umo, str) and await self.store.is_subscribed(umo)
        lines = [
            f"当前会话订阅状态：{'已订阅' if subscribed else '未订阅'}",
            f"总订阅数：{len(subscriptions)}",
        ]
        if isinstance(umo, str) and subscribed:
            last_slot = await self.store.get_last_sent_slot(umo)
            lines.append(f"最近成功推送槽位：{last_slot or '-'}")
        yield event.plain_result("\n".join(lines))
