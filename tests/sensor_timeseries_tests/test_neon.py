import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from sensor_timeseries.sources.neon import collect_month


class MonthlyDownloadTest(unittest.TestCase):
    def test_download_repeat_and_corruption(self):
        sample = b"startDateTime,pHMean\n2023-06-01T00:00:00Z,7.2\n"
        listing, file = MagicMock(), MagicMock()
        for response in (listing, file):
            response.__enter__.return_value = response
            response.status_code = 200
        listing.json.return_value = {"data": {"files": [{
            "name": "water.csv",
            "size": len(sample),
            "md5": hashlib.md5(sample).hexdigest(),
            "url": "https://storage.googleapis.com/example/water.csv",
        }]}}
        file.iter_content.return_value = [sample]
        session = MagicMock()
        session.get.side_effect = [listing, file, listing, listing, file]
        with tempfile.TemporaryDirectory() as temporary:
            args = (
                session,
                "token",
                "water-quality",
                "BARC",
                "2023-06",
                Path(temporary),
            )
            self.assertEqual(collect_month(*args), 1)
            self.assertEqual(session.get.call_args_list[1].kwargs["headers"], None)
            self.assertEqual(collect_month(*args), 1)
            target = next(Path(temporary).rglob("water.csv"))
            target.unlink()
            file.iter_content.return_value = [b"bad"]
            with self.assertRaisesRegex(ValueError, "校验失败"):
                collect_month(*args)
