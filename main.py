from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

try:
    from .merchant_data import (
        FALLBACK_URL,
        PRIMARY_URL,
        current_push_window,
        load_snapshot,
        normalize_payload,
        render_message_text,
        save_snapshot,
    )
except ImportError:
    from merchant_data import (
        FALLBACK_URL,
        PRIMARY_URL,
        current_push_window,
        load_snapshot,
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
    retry_interval_minutes: int = 3
    max_retry_attempts: int = 4
    request_timeout_seconds: int = 10
    snapshot_fallback_for_now: bool = True
    message_header: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "PluginConfigView":
        data = dict(raw or {})
        return cls(
            enable_push=bool(data.get("enable_push", True)),
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
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
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

    def _build_snapshot_path(self) -> Path:
        if get_astrbot_data_path is not None:
            return Path(get_astrbot_data_path()) / "plugin_data" / self.name / SNAPSHOT_FILENAME
        return Path("data") / "plugin_data" / getattr(self, "name", PLUGIN_NAME) / SNAPSHOT_FILENAME

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if not self.config.enable_push or self._scheduler_task is not None:
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("rocomerchant: 定时推送任务已启动")

    async def terminate(self):
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None
        await self.http_client.aclose()

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self._run_push_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rocomerchant: 定时推送检查失败")
            await asyncio.sleep(30)

    async def _run_push_cycle(self) -> None:
        window = current_push_window(
            retry_interval_minutes=self.config.retry_interval_minutes,
            max_retry_attempts=self.config.max_retry_attempts,
        )
        if not window or window["slot"] is None or window["due_attempt_index"] is None:
            return

        slot = window["slot"]
        push_state = await self.store.get_push_state(slot)
        if push_state.get("completed") is True:
            return

        last_attempt_index = int(push_state.get("last_attempt_index", -1))
        due_attempt_index = int(window["due_attempt_index"])
        if due_attempt_index <= last_attempt_index:
            return

        subscriptions = await self.store.list_subscriptions()
        pending_umos: list[str] = []
        for item in subscriptions:
            umo = item.get("umo")
            if not isinstance(umo, str) or not umo or not item.get("enabled", True):
                continue
            last_sent_slot = await self.store.get_last_sent_slot(umo)
            if last_sent_slot != slot:
                pending_umos.append(umo)

        await self.store.set_push_state(
            slot,
            {
                "last_attempt_index": due_attempt_index,
                "completed": False,
            },
        )

        if not pending_umos:
            await self.store.set_push_state(
                slot,
                {
                    "last_attempt_index": due_attempt_index,
                    "completed": True,
                },
            )
            return

        data = await self.fetch_merchant_data()
        if not data["schedule_check"]["matches_expected_schedule"]:
            logger.warning("rocomerchant: 当前数据未匹配预期轮次，等待下次重试")
            return

        chain = MessageChain().message(
            render_message_text(data, message_header=self.config.message_header)
        )
        sent_count = 0
        for umo in pending_umos:
            try:
                await self.context.send_message(umo, chain)
                await self.store.set_last_sent_slot(umo, slot)
                sent_count += 1
            except Exception:
                logger.exception("rocomerchant: 向会话 %s 推送失败", umo)

        if sent_count > 0:
            await self.store.set_push_state(
                slot,
                {
                    "last_attempt_index": due_attempt_index,
                    "completed": True,
                },
            )

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
        yield event.plain_result(await self.reply_now_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @merchant.command("subscribe")
    async def merchant_subscribe(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        if not isinstance(umo, str) or not umo:
            yield event.plain_result("当前会话不支持订阅。")
            return

        created = await self.store.add_subscription(umo)
        if created:
            yield event.plain_result("当前会话已订阅远行商人定时推送。")
            return
        yield event.plain_result("当前会话已订阅，无需重复操作。")

    @filter.permission_type(filter.PermissionType.ADMIN)
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
