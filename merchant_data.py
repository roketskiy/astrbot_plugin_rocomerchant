from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PRIMARY_URL = "https://rocokingdomworld.org/api/merchant/live"
FALLBACK_URL = "https://rocokingdomworld.org/data/merchant.json"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_PUSH_TIMES = ("08:10", "12:10", "16:10", "20:10")


def current_beijing_datetime(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(BEIJING_TZ)


def format_beijing(dt: datetime) -> str:
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_beijing_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)


def current_beijing_schedule(now: datetime | None = None) -> dict[str, Any]:
    beijing_now = current_beijing_datetime(now)
    day_start = beijing_now.replace(hour=0, minute=0, second=0, microsecond=0)
    rounds = (
        (1, 8, 12),
        (2, 12, 16),
        (3, 16, 20),
        (4, 20, 24),
    )

    for round_number, start_hour, end_hour in rounds:
        start = day_start + timedelta(hours=start_hour)
        end = day_start + timedelta(hours=end_hour)
        if start <= beijing_now < end:
            return {
                "status": "open",
                "round": round_number,
                "started_at_beijing": format_beijing(start),
                "next_refresh_beijing": format_beijing(end),
            }

    next_open = day_start + timedelta(hours=8)
    if beijing_now >= next_open:
        next_open += timedelta(days=1)

    return {
        "status": "closed",
        "round": None,
        "started_at_beijing": None,
        "next_refresh_beijing": format_beijing(next_open),
    }


def current_round_slot(now: datetime | None = None) -> str | None:
    beijing_now = current_beijing_datetime(now)
    schedule = current_beijing_schedule(beijing_now)
    round_number = schedule["round"]
    if round_number is None:
        return None
    return f"{beijing_now.strftime('%Y-%m-%d')}-r{round_number}"


def build_default_push_times(
    first_push_delay_minutes: int = 10,
) -> tuple[str, str, str, str]:
    base_hours = (8, 12, 16, 20)
    times: list[str] = []
    for hour in base_hours:
        dt = datetime(2000, 1, 1, hour, 0) + timedelta(
            minutes=max(0, first_push_delay_minutes)
        )
        times.append(dt.strftime("%H:%M"))
    return tuple(times)  # type: ignore[return-value]


def normalize_push_times(
    raw_value: Any,
    first_push_delay_minutes: int = 10,
) -> tuple[str, ...]:
    default_times = build_default_push_times(first_push_delay_minutes)

    if isinstance(raw_value, str):
        candidates = [part.strip() for part in raw_value.split(",") if part.strip()]
    elif isinstance(raw_value, list):
        candidates = [str(part).strip() for part in raw_value if str(part).strip()]
    else:
        return default_times

    if not candidates:
        return default_times

    normalized: list[str] = []
    for value in candidates:
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError:
            return default_times
        normalized.append(parsed.strftime("%H:%M"))
    return tuple(normalized)


def current_push_window(
    now: datetime | None = None,
    push_times: tuple[str, str, str, str] = DEFAULT_PUSH_TIMES,
    retry_interval_minutes: int = 3,
    max_retry_attempts: int = 4,
) -> dict[str, Any] | None:
    beijing_now = current_beijing_datetime(now)
    schedule = current_beijing_schedule(beijing_now)
    if schedule["status"] != "open" or schedule["round"] is None:
        return None

    started_at = parse_beijing_time(schedule["started_at_beijing"])
    if started_at is None:
        return None

    round_number = int(schedule["round"])
    push_hour, push_minute = map(int, push_times[round_number - 1].split(":"))
    first_attempt_time = started_at.replace(
        hour=push_hour,
        minute=push_minute,
        second=0,
        microsecond=0,
    )

    attempt_count = max_retry_attempts + 1
    attempt_times = [
        first_attempt_time + timedelta(minutes=retry_interval_minutes * index)
        for index in range(attempt_count)
    ]
    due_attempt_index = None
    for index, attempt_time in enumerate(attempt_times):
        if beijing_now >= attempt_time:
            due_attempt_index = index

    return {
        "slot": current_round_slot(beijing_now),
        "round": round_number,
        "started_at_beijing": schedule["started_at_beijing"],
        "next_refresh_beijing": schedule["next_refresh_beijing"],
        "attempt_times": attempt_times,
        "due_attempt_index": due_attempt_index,
        "max_retry_attempts": max_retry_attempts,
    }


def normalize_item(raw_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": raw_item.get("name", ""),
        "price": str(raw_item.get("price", "")),
        "limit": str(raw_item.get("limit", "")),
        "category": raw_item.get("category", ""),
        "description": raw_item.get("description", ""),
        "image": raw_item.get("image", ""),
    }


def normalize_payload(
    payload: dict[str, Any],
    source_url: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    round_number = payload.get("round")
    items = payload.get("items")
    rounds = payload.get("rounds") or {}

    if not isinstance(items, list):
        items = rounds.get(str(round_number), [])
    if not isinstance(items, list):
        items = []

    normalized_items = [normalize_item(item) for item in items if isinstance(item, dict)]
    schedule = current_beijing_schedule(now)
    normalized = {
        "source_url": source_url,
        "source_page": payload.get("sourceUrl", ""),
        "fetched_at": payload.get("fetchedAt"),
        "timezone": payload.get("timezone"),
        "status": payload.get("status"),
        "round": round_number,
        "started_at_beijing": payload.get("startedAtBeijing"),
        "next_refresh_beijing": payload.get("nextRefreshBeijing"),
        "merchant_position": payload.get("merchantPosition", ""),
        "duration_hours": payload.get("durationHours"),
        "items": normalized_items,
        "item_count": len(normalized_items),
        "schedule_check": {
            "expected_status": schedule["status"],
            "expected_round": schedule["round"],
            "expected_started_at_beijing": schedule["started_at_beijing"],
            "expected_next_refresh_beijing": schedule["next_refresh_beijing"],
            "matches_expected_schedule": (
                payload.get("status") == schedule["status"]
                and payload.get("round") == schedule["round"]
                and payload.get("startedAtBeijing") == schedule["started_at_beijing"]
                and payload.get("nextRefreshBeijing") == schedule["next_refresh_beijing"]
            ),
        },
    }
    return normalized


def format_price(value: str) -> str:
    try:
        number = int(str(value).replace(",", ""))
    except ValueError:
        return value or "-"
    return f"{number:,}"


def format_display_time(value: str | None) -> str:
    if not value:
        return "-"
    parsed = parse_beijing_time(value)
    if parsed is None:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M")


def render_message_text(
    data: dict[str, Any],
    message_header: str = "",
    stale: bool = False,
) -> str:
    lines: list[str] = []
    if message_header.strip():
        lines.append(message_header.strip())

    if stale:
        lines.append("远行商人数据查询失败，以下为最近一次成功缓存")
    elif data.get("status") == "closed":
        lines.append("远行商人当前为关闭时段")
    else:
        round_number = data.get("round")
        lines.append(f"远行商人 第 {round_number} 轮" if round_number else "远行商人 当前数据")

    lines.append(f"本轮开始  {format_display_time(data.get('started_at_beijing'))}")
    lines.append(f"下次刷新  {format_display_time(data.get('next_refresh_beijing'))}")

    items = data.get("items") or []
    if items:
        lines.append("")
        lines.append("商品清单")
        for index, item in enumerate(items, start=1):
            name = item.get("name") or "-"
            price = format_price(item.get("price", ""))
            limit = item.get("limit") or "-"
            category = item.get("category") or ""
            title = f"{index}. {name}"
            if category:
                title += f" [{category}]"
            lines.append(title)
            lines.append(f"   价格  {price} 洛克贝    限购  {limit}")
    else:
        lines.append("")
        lines.append("当前没有可展示商品。")
    return "\n".join(lines)


def save_snapshot(data: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_snapshot(snapshot_path: Path) -> dict[str, Any] | None:
    if not snapshot_path.exists():
        return None
    try:
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
