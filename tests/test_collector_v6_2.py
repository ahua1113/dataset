"""V6.2 期刊扩展配置及数据校验的离线回归测试。"""
import json
from pathlib import Path
import tempfile
import unittest

import collector


ROOT = Path(__file__).resolve().parents[1]
JOURNALS = ROOT / "config" / "journals_v6_2.json"
PREVIOUS = ROOT / "config" / "journals_v6_1.json"


# 本测试类集中验证配置与采集规则的兼容性，避免依赖在线服务。
class CollectorV62JournalExpansionTests(unittest.TestCase):
    def test_whitelist_has_thirty_four_unique_valid_journals(self):
        journals = collector.load_journals(JOURNALS)
        self.assertEqual(34, len(journals))
        self.assertEqual(34, len({j.name.casefold() for j in journals}))
        self.assertEqual(34, len({j.issn.upper() for j in journals}))

    def test_previous_twenty_are_retained_and_fourteen_are_added(self):
        previous = {j.name for j in collector.load_journals(PREVIOUS)}
        current = {j.name for j in collector.load_journals(JOURNALS)}
        self.assertTrue(previous.issubset(current))
        self.assertEqual(14, len(current - previous))
        self.assertNotIn("Angewandte Chemie International Edition", current)
        self.assertIn("Nature Reviews Chemistry", current)

    def test_broad_nature_and_science_are_deliberately_excluded(self):
        names = {j.name for j in collector.load_journals(JOURNALS)}
        self.assertNotIn("Nature", names)
        self.assertNotIn("Science", names)
        self.assertIn("Communications Chemistry", names)
        self.assertIn("Nature Chemical Biology", names)

    def test_matter_uses_its_online_issn_not_one_earth_issn(self):
        rows = json.loads(JOURNALS.read_text(encoding="utf-8"))
        matter = next(row for row in rows if row["name"] == "Matter")
        self.assertEqual("2590-2385", matter["issn"])

    def test_v6_2_config_rejects_wrong_size(self):
        rows = json.loads(JOURNALS.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journals_v6_2.json"
            path.write_text(json.dumps(rows[:-1]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly 34"):
                collector.load_journals(path)


if __name__ == "__main__":
    unittest.main()
