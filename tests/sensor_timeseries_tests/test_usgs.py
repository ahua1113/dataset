from contextlib import redirect_stdout
from datetime import date
from io import StringIO
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sensor_timeseries.cli import main
from sensor_timeseries.common import file_digest
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
            audit = [
                "--source", "usgs", "--product", "continuous",
                "--site", "01646500", "--parameter-code", "00060",
                "--start", "2024-01-01", "--end", "2024-01-02",
                "--out", temporary, "--audit-only",
            ]
            with patch("requests.sessions.Session.get") as get:
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(audit), 0)
                report = json.loads(output.getvalue())["partitions"][0]
                self.assertEqual(report["observation_count"], 1)
                self.assertEqual(report["units"], {"ft3/s": 1})
                self.assertEqual(report["quality_flags"], {"Approved": 1})

                duplicate = dict(record, timestamp_utc="2023-12-31T19:00:00-05:00", value=None)
                invalid = dict(record, timestamp_utc="2024-01-01T00:15:00", value="NaN")
                with record_path.open("a", encoding="utf-8") as records:
                    records.write(json.dumps(duplicate) + "\n")
                    records.write(json.dumps(invalid) + "\n")
                manifest_path = next(Path(temporary).rglob("*.manifest.json"))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest.update(observation_count=3, sha256=file_digest(record_path, "sha256"))
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(audit), 1)
                report = json.loads(output.getvalue())["partitions"][0]
                self.assertTrue(report["sha256_matches"])
                self.assertTrue(report["row_count_matches"])
                self.assertEqual(report["duplicate_observations"], 1)
                self.assertEqual(report["missing_values"], 1)
                self.assertEqual(report["invalid_values"], 1)
                self.assertEqual(report["invalid_timestamps"], 1)

                manifest["sha256"] = "different hash"
                manifest["observation_count"] = 4
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(audit), 1)
                report = json.loads(output.getvalue())["partitions"][0]
                self.assertFalse(report["sha256_matches"])
                self.assertFalse(report["row_count_matches"])
                get.assert_not_called()

    def test_rejects_malformed_page_and_retries_empty_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            day = date(2024, 1, 1)
            session = MagicMock()
            response = session.get.return_value
            response.__enter__.return_value = response
            response.status_code = 200
            response.json.side_effect = [
                {"unexpected": []},
                {"features": []},
                {"features": [{"id": "observation-1"}]},
            ]
            args = (session, "", "USGS-01646500", day, day, root)
            options = {"parameter_code": "00060", "page_size": 100}

            with self.assertRaisesRegex(ValueError, "features"):
                collect_interval(*args, **options)
            self.assertEqual(list(root.rglob("*.json*")), [])
            self.assertEqual(collect_interval(*args, **options), 0)
            self.assertEqual(collect_interval(*args, **options), 1)
            self.assertEqual(session.get.call_count, 3)

    def test_long_range_is_split_by_calendar_year(self):
        intervals = list(
            annual_intervals(date(2023, 12, 30), date(2025, 1, 2))
        )
        self.assertEqual(len(intervals), 3)
        self.assertEqual(
            intervals[1],
            (date(2024, 1, 1), date(2024, 12, 31)),
        )
