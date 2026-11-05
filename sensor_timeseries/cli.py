"""NEON 与 USGS Water Data 采集命令行。"""

from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path
import re
import sys

import requests

from sensor_timeseries.sources import neon, usgs


def parse_month(value: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise argparse.ArgumentTypeError("月份格式应为 YYYY-MM")
    try:
        return date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式应为 YYYY-MM-DD") from exc


def next_month(value: date) -> date:
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="公开传感器时序数据采集器")
    parser.add_argument("--source", choices=("neon", "usgs"), default="neon")
    parser.add_argument("--product", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end")
    parser.add_argument("--parameter-code", help="USGS五位参数代码，例如流量00060")
    parser.add_argument("--page-size", type=int, default=10000)
    parser.add_argument(
        "--out", type=Path, default=Path("chemistry_dataset/sensor_timeseries")
    )
    return parser


def run_neon(args: argparse.Namespace, session) -> int:
    if args.product not in neon.PRODUCTS:
        raise ValueError("NEON product 应为 water-quality 或 groundwater")
    if not re.fullmatch(r"[A-Za-z]{4}", args.site):
        raise ValueError("NEON站点应为四字母代码")
    token = os.getenv("NEON_API_TOKEN", "").strip()
    if not token:
        raise ValueError("请先设置 NEON_API_TOKEN 环境变量")
    current = parse_month(args.start)
    end = parse_month(args.end) if args.end else current
    if current > end:
        raise ValueError("起始月份不能晚于结束月份")
    failed = False
    while current <= end:
        label = current.strftime("%Y-%m")
        try:
            count = neon.collect_month(
                session, token, args.product, args.site.upper(), label, args.out
            )
            print(f"NEON {args.site.upper()} {label}: {count} 个CSV已校验")
        except Exception as exc:
            print(f"NEON {args.site.upper()} {label}: {exc}", file=sys.stderr)
            failed = True
        current = next_month(current)
    return int(failed)


def run_usgs(args: argparse.Namespace, session) -> int:
    if args.product != "continuous":
        raise ValueError("USGS product 当前支持 continuous")
    if args.parameter_code and not re.fullmatch(r"[0-9]{5}", args.parameter_code):
        raise ValueError("USGS参数代码应为五位数字")
    start = parse_day(args.start)
    end = parse_day(args.end) if args.end else start
    if start > end:
        raise ValueError("起始日期不能晚于结束日期")
    station, intervals, count = usgs.collect(
        session,
        os.getenv("USGS_API_KEY", "").strip(),
        args.site,
        start,
        end,
        args.out,
        parameter_code=args.parameter_code,
        page_size=args.page_size,
    )
    print(f"USGS {station}: {intervals} 个时间段，共 {count} 条观测")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.page_size <= 10000:
        parser.error("--page-size 必须在1到10000之间")
    try:
        with requests.Session() as session:
            if args.source == "neon":
                return run_neon(args, session)
            return run_usgs(args, session)
    except (ValueError, OSError, RuntimeError, requests.RequestException) as exc:
        parser.exit(1, f"采集失败：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
