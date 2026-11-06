"""USGS Water Data 连续传感器观测采集。"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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


def interval_paths(
    station: str,
    start: date,
    end: date,
    root: Path,
    parameter_code: str | None,
) -> tuple[Path, Path]:
    directory = root / "usgs" / "continuous" / station
    stem = f"{start.isoformat()}_{end.isoformat()}"
    if parameter_code:
        stem += f"_{parameter_code}"
    return directory / f"{stem}.jsonl", directory / f"{stem}.manifest.json"


def audit_interval(
    station: str,
    start: date,
    end: date,
    root: Path,
    parameter_code: str | None,
) -> dict:
    """Read a saved partition and report integrity and observation quality."""
    observations_path, manifest_path = interval_paths(
        station, start, end, root, parameter_code
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"USGS 清单必须是 JSON 对象：{manifest_path}")
    expected = {
        "station": station,
        "parameter_code": parameter_code,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "file": observations_path.name,
    }
    sha256_matches = manifest.get("sha256") == file_digest(observations_path, "sha256")
    manifest_matches = all(manifest.get(key) == value for key, value in expected.items())

    counts: Counter[str] = Counter()
    units: Counter[str] = Counter()
    quality_flags: Counter[str] = Counter()
    seen: set[tuple[str, datetime]] = set()
    first = last = None
    with observations_path.open(encoding="utf-8") as observations:
        for line_number, line in enumerate(observations, start=1):
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"USGS 观测第 {line_number} 行必须是 JSON 对象")
            counts["observations"] += 1
            units[str(row.get("unit") or "(missing)")] += 1
            quality_flags[str(row.get("quality_flag") or "(missing)")] += 1

            value = row.get("value")
            if value is None or str(value).strip() == "":
                counts["missing_values"] += 1
            else:
                try:
                    if not Decimal(str(value)).is_finite():
                        counts["invalid_values"] += 1
                except InvalidOperation:
                    counts["invalid_values"] += 1

            sensor = row.get("sensor_id")
            if (
                not isinstance(sensor, str) or not sensor
                or row.get("station_id") != station
                or (parameter_code is not None and row.get("variable") != parameter_code)
            ):
                counts["invalid_series"] += 1
                sensor = None

            try:
                stamp = datetime.fromisoformat(row.get("timestamp_utc"))
                if stamp.tzinfo is None:
                    raise ValueError("timestamp requires a UTC offset")
                stamp = stamp.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                counts["invalid_timestamps"] += 1
                continue
            first = min(first or stamp, stamp)
            last = max(last or stamp, stamp)
            if not start <= stamp.date() <= end:
                counts["outside_interval"] += 1
            if sensor is not None:
                key = (sensor, stamp)
                if key in seen:
                    counts["duplicate_observations"] += 1
                seen.add(key)

    observations = counts.pop("observations", 0)
    row_count_matches = observations == manifest.get("observation_count")
    return {
        **expected,
        "observation_count": observations,
        "first_timestamp_utc": first.isoformat() if first else None,
        "last_timestamp_utc": last.isoformat() if last else None,
        "missing_values": counts["missing_values"],
        "invalid_values": counts["invalid_values"],
        "invalid_series": counts["invalid_series"],
        "invalid_timestamps": counts["invalid_timestamps"],
        "outside_interval": counts["outside_interval"],
        "duplicate_observations": counts["duplicate_observations"],
        "units": dict(units),
        "quality_flags": dict(quality_flags),
        "sha256_matches": sha256_matches,
        "manifest_matches": manifest_matches,
        "row_count_matches": row_count_matches,
        "passed": (
            observations > 0 and sha256_matches and manifest_matches
            and row_count_matches and not counts
        ),
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
    observations_path, manifest_path = interval_paths(
        station, start, end, root, parameter_code
    )
    if observations_path.exists() and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            manifest = None
        cached_count = manifest.get("observation_count") if isinstance(manifest, dict) else None
        if (
            type(cached_count) is int and cached_count > 0
            and manifest.get("sha256") == file_digest(observations_path, "sha256")
        ):
            return cached_count

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
    directory = observations_path.parent
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
                if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
                    raise ValueError("USGS 响应缺少有效的 features 数组")
                links = payload.get("links", [])
                if not isinstance(links, list) or any(not isinstance(link, dict) for link in links):
                    raise ValueError("USGS 响应的分页链接格式无效")
                for feature in payload["features"]:
                    json.dump(normalize_feature(feature), temporary, ensure_ascii=False)
                    temporary.write("\n")
                    count += 1
                next_link = next((link for link in links if link.get("rel") == "next"), None)
                next_url = next_link.get("href") if next_link is not None else None
                if next_link is not None and (not isinstance(next_url, str) or not next_url):
                    raise ValueError("USGS 响应的下一页链接无效")
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
