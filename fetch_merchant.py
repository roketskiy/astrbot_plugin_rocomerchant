#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from merchant_data import (
    FALLBACK_URL,
    PRIMARY_URL,
    format_price,
    normalize_payload,
    save_snapshot,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取 rocokingdomworld.org 当前远行商人商品数据。"
    )
    parser.add_argument(
        "--output",
        default="data/latest.json",
        help="保存标准化结果的 JSON 路径，默认: data/latest.json",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="单次 HTTP 请求超时时间（秒），默认: 10",
    )
    return parser.parse_args()


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "User-Agent": "rocomerchant-fetcher/1.0 (+https://rocokingdomworld.org/)",
            "Accept": "application/json, text/plain, */*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        if response.status < 200 or response.status >= 300:
            raise HTTPError(
                url,
                response.status,
                f"Unexpected status code: {response.status}",
                response.headers,
                None,
            )
        return json.loads(response.read().decode("utf-8"))


def fetch_merchant(timeout: float) -> dict[str, Any]:
    errors: list[str] = []
    for url in (PRIMARY_URL, FALLBACK_URL):
        try:
            payload = fetch_json(url, timeout=timeout)
            return normalize_payload(payload, source_url=url)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            errors.append(f"{url}: {exc}")

    raise RuntimeError("两个接口都抓取失败:\n" + "\n".join(errors))


def print_summary(data: dict[str, Any]) -> None:
    schedule_check = data["schedule_check"]
    print(f"抓取接口: {data['source_url']}")
    print(f"源站页面: {data.get('source_page') or '-'}")
    print(f"抓取时间(UTC): {data.get('fetched_at') or '-'}")
    print(f"状态: {data.get('status') or '-'}")
    print(f"轮次: {data.get('round') if data.get('round') is not None else '-'}")
    print(f"本轮开始(北京时间): {data.get('started_at_beijing') or '-'}")
    print(f"下次刷新(北京时间): {data.get('next_refresh_beijing') or '-'}")
    print(f"商品数量: {data.get('item_count', 0)}")
    print(
        "轮次校验: "
        + ("通过" if schedule_check["matches_expected_schedule"] else "未通过")
    )

    if not schedule_check["matches_expected_schedule"]:
        print(
            "期望轮次: "
            f"status={schedule_check['expected_status']}, "
            f"round={schedule_check['expected_round']}, "
            f"started={schedule_check['expected_started_at_beijing']}, "
            f"next={schedule_check['expected_next_refresh_beijing']}"
        )

    if not data["items"]:
        print("当前没有可展示商品。")
        return

    print("\n商品列表:")
    for index, item in enumerate(data["items"], start=1):
        name = item.get("name") or "-"
        price = format_price(item.get("price", ""))
        limit = item.get("limit") or "-"
        category = item.get("category") or "-"
        print(f"{index}. {name} | {price} 洛克贝 | 限购 {limit} | 分类 {category}")


def main() -> int:
    args = parse_args()
    output_path = Path(args.output)

    try:
        data = fetch_merchant(timeout=args.timeout)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    save_snapshot(data, output_path)
    print_summary(data)
    print(f"\n已保存到: {output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
