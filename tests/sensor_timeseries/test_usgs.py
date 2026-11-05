from datetime import date
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from sensor_timeseries.sources.usgs import annual_intervals, collect_interval


class UsgsContinuousTest(unittest.TestCase):
    def test_pagination_normalization_and_resume(self):
        first, second = MagicMock(), MagicMock()
        for response in (first, second):
            response.__enter__.return_value = response
            response.status_code = 200
        feature = {
            "id": "observation-1",
            "geometry": {"coordinates": [-77.1, 38.9]},
            "properties": {
                "monitoring_location_id": "USGS-01646500",
                "time_series_id": "series-1",
                "parameter_code": "00060",
                "time": "2024-01-01T00:00:00Z",
                "value": "123",
                "unit_of_measure": "ft3/s",
                "approval_status": "Approved",
            },
        }
        first.json.return_value = {
            "features": [feature],
            "links": [
                {
                    "rel": "next",
                    "href": "https://api.waterdata.usgs.gov/page/2",
                }
            ],
        }
        second.json.return_value = {"features": [], "links": []}
        session = MagicMock()
        session.get.side_effect = [first, second]
        with tempfile.TemporaryDirectory() as temporary:
            args = (
                session,
                "key",
                "USGS-01646500",
                date(2024, 1, 1),
                date(2024, 1, 2),
                Path(temporary),
            )
            self.assertEqual(
                collect_interval(
                    *args, parameter_code="00060", page_size=100
                ),
                1,
            )
            record_path = next(Path(temporary).rglob("*.jsonl"))
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["station_id"], "USGS-01646500")
            self.assertEqual(record["value"], "123")
            self.assertEqual(record["longitude"], -77.1)
            self.assertEqual(
                session.get.call_args_list[0].kwargs["headers"],
                {"X-Api-Key": "key"},
            )
            self.assertEqual(
                collect_interval(
                    *args, parameter_code="00060", page_size=100
                ),
                1,
            )
            self.assertEqual(session.get.call_count, 2)

    def test_long_range_is_split_by_calendar_year(self):
        intervals = list(
            annual_intervals(date(2023, 12, 30), date(2025, 1, 2))
        )
        self.assertEqual(len(intervals), 3)
        self.assertEqual(
            intervals[1],
            (date(2024, 1, 1), date(2024, 12, 31)),
        )
