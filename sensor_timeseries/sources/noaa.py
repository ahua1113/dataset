"""NOAA CO-OPS 连续水温观测采集。"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
from urllib.parse import urlencode

from sensor_timeseries.common import atomic_write_json, checked_get, file_digest


API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
PRODUCT = "water_temperature"


def summarize_observations(rows: list, start: date, end: date) -> dict:
    timestamps: set[datetime] = set()
    flags: Counter[str] = Counter()
    missing = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("NOAA 观测必须为 JSON 对象")
        try:
            stamp = datetime.strptime(row.get("t"), "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("NOAA 观测时间格式无效") from exc
        if not start <= stamp.date() <= end:
            raise ValueError("NOAA 观测时间超出请求范围")
        if stamp in timestamps:
            raise ValueError("NOAA 响应含有重复观测时间")
        timestamps.add(stamp)
        value = row.get("v")
        if value is None or str(value).strip() == "":
            missing += 1
        else:
            try:
                valid = math.isfinite(float(value))
            except (TypeError, ValueError, OverflowError):
                valid = False
            if not valid:
                raise ValueError("NOAA 水温值必须为有限数值")
        flags[str(row.get("f") or "(missing)")] += 1
    return {
        "observation_count": len(timestamps),
        "first_timestamp_utc": min(timestamps).isoformat(),
        "last_timestamp_utc": max(timestamps).isoformat(),
        "missing_values": missing,
        "duplicate_observations": 0,
        "quality_flags": dict(flags),
    }


def collect(
    session, product: str, site: str, start: date, end: date, root: Path
) -> int:
    if product != PRODUCT:
        raise ValueError(f"NOAA product 当前支持 {PRODUCT}")
    if not re.fullmatch(r"[0-9]{7}", site):
        raise ValueError("NOAA 水温站点应为七位数字")
    if not 0 <= (end - start).days < 30:
        raise ValueError("NOAA 水温采集日期范围应为 1 至 30 天")
    params = {
        "product": product,
        "station": site,
        "begin_date": start.strftime("%Y%m%d") + " 00:00",
        "end_date": end.strftime("%Y%m%d") + " 23:59",
        "time_zone": "gmt",
        "units": "metric",
        "format": "json",
        "application": "chemistry_source_collector",
    }
    source = f"{API}?{urlencode(params)}"
    directory = root / "noaa" / product / site
    stem = f"{start.isoformat()}_{end.isoformat()}"
    raw_path = directory / f"{stem}.raw.json"
    manifest_path = directory / f"{stem}.manifest.json"
    if raw_path.is_file() and manifest_path.is_file():
        try:
            saved = json.loads(manifest_path.read_bytes())
        except ValueError:
            saved = None
        if (
            isinstance(saved, dict)
            and saved.get("source_url") == source
            and saved.get("sha256") == file_digest(raw_path, "sha256")
        ):
            count = saved.get("observation_count")
            if type(count) is int and count > 0:
                return count

    with checked_get(session, source, max_retries=2) as response:
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("NOAA 响应必须为 JSON 对象")
    if payload.get("error"):
        raise ValueError(f"NOAA API 返回错误：{str(payload['error'])[:250]}")
    metadata = payload.get("metadata")
    rows = payload.get("data")
    if not isinstance(metadata, dict) or metadata.get("id") != site:
        raise ValueError("NOAA 响应缺少匹配的站点元数据")
    if not isinstance(rows, list) or not rows:
        raise ValueError("NOAA 响应缺少水温观测")
    summary = summarize_observations(rows, start, end)
    atomic_write_json(raw_path, payload)
    atomic_write_json(
        manifest_path,
        {
            "source": "NOAA CO-OPS",
            "source_url": source,
            "station": site,
            "product": product,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "time_zone": "UTC",
            "unit": "degC",
            "raw_file": raw_path.name,
            "sha256": file_digest(raw_path, "sha256"),
            **summary,
        },
    )
    return summary["observation_count"]
