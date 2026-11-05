"""USGS Water Data 连续传感器观测采集。"""

from __future__ import annotations

from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlencode, urlsplit

from sensor_timeseries.common import atomic_write_json, checked_get, file_digest


API = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/continuous/items"
HOST = "api.waterdata.usgs.gov"


def annual_intervals(start: date, end: date):
    current = start
    while current <= end:
        interval_end = min(end, date(current.year, 12, 31))
        yield current, interval_end
        current = interval_end + timedelta(days=1)


def normalize_station(site: str) -> str:
    station = site.upper()
    if station.isdigit():
        station = f"USGS-{station}"
    if not station.startswith("USGS-") or not station[5:].isdigit():
        raise ValueError("USGS站点应为数字编号或 USGS-数字编号")
    return station


def normalize_feature(feature: dict) -> dict:
    properties = feature.get("properties", {})
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates") or [None, None]
    return {
        "source": "USGS Water Data",
        "domain": "water",
        "observation_id": feature.get("id"),
        "station_id": properties.get("monitoring_location_id"),
        "sensor_id": properties.get("time_series_id") or properties.get("timeseries_id"),
        "variable": properties.get("parameter_code"),
        "unit": properties.get("unit_of_measure"),
        "timestamp_utc": properties.get("time"),
        "value": properties.get("value"),
        "quality_flag": properties.get("approval_status")
        or properties.get("approvals_status"),
        "qualifier": properties.get("qualifier"),
        "longitude": coordinates[0] if len(coordinates) > 0 else None,
        "latitude": coordinates[1] if len(coordinates) > 1 else None,
        "raw_properties": properties,
    }


def collect_interval(
    session,
    api_key: str,
    station: str,
    start: date,
    end: date,
    root: Path,
    *,
    parameter_code: str | None,
    page_size: int,
) -> int:
    directory = root / "usgs" / "continuous" / station
    stem = f"{start.isoformat()}_{end.isoformat()}"
    if parameter_code:
        stem += f"_{parameter_code}"
    observations_path = directory / f"{stem}.jsonl"
    manifest_path = directory / f"{stem}.manifest.json"
    if observations_path.exists() and manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if manifest.get("sha256") == file_digest(observations_path, "sha256"):
            return int(manifest["observation_count"])

    parameters: dict[str, object] = {
        "f": "json",
        "limit": page_size,
        "monitoring_location_id": station,
        "datetime": (
            f"{start.isoformat()}T00:00:00Z/"
            f"{end.isoformat()}T23:59:59Z"
        ),
    }
    if parameter_code:
        parameters["parameter_code"] = parameter_code
    first_url = f"{API}?{urlencode(parameters)}"
    auth_headers = {"X-Api-Key": api_key} if api_key else None
    directory.mkdir(parents=True, exist_ok=True)
    count = 0
    next_url: str | None = first_url
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            while next_url:
                if urlsplit(next_url).netloc != HOST:
                    raise ValueError("USGS 分页链接域名异常")
                with checked_get(
                    session,
                    next_url,
                    auth_headers=auth_headers,
                    credential_host=HOST,
                ) as response:
                    payload = response.json()
                for feature in payload.get("features", []):
                    json.dump(normalize_feature(feature), temporary, ensure_ascii=False)
                    temporary.write("\n")
                    count += 1
                next_url = next(
                    (
                        link.get("href")
                        for link in payload.get("links", [])
                        if link.get("rel") == "next"
                    ),
                    None,
                )
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, observations_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    atomic_write_json(
        manifest_path,
        {
            "source": "USGS Water Data",
            "collection": "continuous",
            "station": station,
            "parameter_code": parameter_code,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "observation_count": count,
            "file": observations_path.name,
            "sha256": file_digest(observations_path, "sha256"),
            "source_url": first_url,
        },
    )
    return count


def collect(
    session,
    api_key: str,
    site: str,
    start: date,
    end: date,
    root: Path,
    *,
    parameter_code: str | None,
    page_size: int,
) -> tuple[str, int, int]:
    station = normalize_station(site)
    total = 0
    intervals = 0
    for interval_start, interval_end in annual_intervals(start, end):
        total += collect_interval(
            session,
            api_key,
            station,
            interval_start,
            interval_end,
            root,
            parameter_code=parameter_code,
            page_size=page_size,
        )
        intervals += 1
    return station, intervals, total
