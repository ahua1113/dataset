from contextlib import redirect_stdout
from datetime import date
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from sensor_timeseries.cli import main
from sensor_timeseries.common import file_digest
from sensor_timeseries.sources.noaa import collect


class NoaaWaterTemperatureTest(unittest.TestCase):
    def setUp(self):
        self.day = date(2024, 1, 3)
        self.site = "9414290"
        self.payload = {
            "metadata": {"id": self.site, "name": "San Francisco", "lat": "37.8063", "lon": "-122.4659"},
            "data": [
                {"t": "2024-01-03 00:00", "v": "15.0", "f": "0,0,0"},
                {"t": "2024-01-03 00:06", "v": "", "f": "0,0,1"},
            ],
        }
        self.session = MagicMock()
        response = self.session.get.return_value
        response.__enter__.return_value = response
        response.status_code = 200
        response.json.return_value = self.payload

    def test_cli_preserves_flags_and_refetches_corrupt_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [
                "--source", "noaa", "--product", "water_temperature",
                "--site", self.site, "--start", self.day.isoformat(), "--out", temporary,
            ]
            with patch("sensor_timeseries.cli.requests.Session") as session, redirect_stdout(StringIO()):
                session.return_value.__enter__.return_value = self.session
                self.assertEqual(main(command), 0)
                self.assertEqual(main(command), 0)
                self.session.get.assert_called_once()
                query = parse_qs(urlsplit(self.session.get.call_args.args[0]).query)
                self.assertEqual(query["units"], ["metric"])
                self.assertEqual(query["time_zone"], ["gmt"])
                self.assertEqual(query["end_date"], ["20240103 23:59"])
                raw_path = next(root.rglob("*.raw.json"))
                manifest_path = next(root.rglob("*.manifest.json"))
                self.assertEqual(json.loads(raw_path.read_bytes()), self.payload)
                manifest = json.loads(manifest_path.read_bytes())
                self.assertEqual(manifest["sha256"], file_digest(raw_path, "sha256"))
                self.assertEqual(manifest["missing_values"], 1)
                self.assertEqual(manifest["quality_flags"], {"0,0,0": 1, "0,0,1": 1})
                raw_path.write_text("corrupt", encoding="utf-8")
                self.assertEqual(main(command), 0)
                self.assertEqual(self.session.get.call_count, 2)
                self.assertEqual(json.loads(raw_path.read_bytes()), self.payload)

    def test_does_not_cache_api_errors_or_duplicates(self):
        samples = [
            {"error": {"message": "No data was found"}},
            dict(self.payload, data=[self.payload["data"][0]] * 2),
        ]
        for payload in samples:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.session.get.return_value.json.return_value = payload
                with self.assertRaises(ValueError):
                    collect(self.session, "water_temperature", self.site, self.day, self.day, root)
                self.assertEqual(list(root.rglob("*.json")), [])
