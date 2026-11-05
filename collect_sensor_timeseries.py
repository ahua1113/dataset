"""Collect monthly NEON sensor observations. Run with --help for examples."""

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests

from sensor_timeseries.neon import PRODUCTS, collect_month


def month(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise argparse.ArgumentTypeError("月份格式应为 YYYY-MM")
    try:
        return date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main():
    parser = argparse.ArgumentParser(description="按站点和月份采集 NEON 传感器原始 CSV 和变量说明")
    parser.add_argument("--product", choices=PRODUCTS, required=True)
    parser.add_argument("--site", required=True, type=str.upper, help="NEON 四字母站点，例如 BARC")
    parser.add_argument("--start", required=True, type=month, help="起始月份 YYYY-MM")
    parser.add_argument("--end", type=month, help="结束月份（含），默认仅采集起始月")
    parser.add_argument("--out", type=Path, default=Path("chemistry_dataset/sensor_timeseries"))
    args = parser.parse_args()
    current, end = args.start, args.end or args.start
    if not re.fullmatch(r"[A-Z]{4}", args.site) or current > end:
        parser.error("请检查四字母站点代码和月份范围")
    token = os.getenv("NEON_API_TOKEN")
    if not token:
        parser.error("请先设置 NEON_API_TOKEN 环境变量")
    failed = False
    with requests.Session() as session:
        while True:
            label = current.strftime("%Y-%m")
            try:
                count = collect_month(session, token, args.product, args.site, label, args.out)
                print(f"{args.site} {label}: {count} 个 CSV 文件已校验", flush=True)
            except (ValueError, OSError, RuntimeError, requests.RequestException) as exc:
                # Requests 异常可能包含临时签名 URL，勿输出完整 URL。
                detail = type(exc).__name__ if isinstance(exc, requests.RequestException) else str(exc)
                print(f"{args.site} {label}: {detail}", file=sys.stderr)
                failed = True
            if current == end:
                break
            current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
