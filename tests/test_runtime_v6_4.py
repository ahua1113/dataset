"""V6.4 连续运行、滚动配额与单实例保护测试。"""
import json
from pathlib import Path
import tempfile
import unittest

from chemistry_collector.disciplines import load_disciplines
from chemistry_collector.papers import _rolling_disciplines
from chemistry_collector.runtime import RunLock, write_runtime_state


ROOT = Path(__file__).resolve().parents[1]
DISCIPLINES = ROOT / "config" / "disciplines_v6_4.json"


class RuntimeV64Tests(unittest.TestCase):
    def test_rolling_target_fills_minimum_then_advances_by_twenty(self):
        disciplines = load_disciplines(DISCIPLINES)
        records = []
        for discipline in disciplines:
            for index in range(20):
                records.append({
                    "doi": f"10.9999/{discipline.id}-{index}",
                    "primary_discipline": discipline.id,
                    "journal": f"Journal {index % 3}",
                })
        rolling, target = _rolling_disciplines(records, disciplines, 20)
        self.assertEqual(40, target)
        self.assertEqual({40}, {discipline.target for discipline in rolling})

        records.pop()
        rolling, target = _rolling_disciplines(records, disciplines, 20)
        self.assertEqual(20, target)
        self.assertEqual({20}, {discipline.target for discipline in rolling})

    def test_run_lock_rejects_second_writer_and_state_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "collector.lock"
            state_path = root / "runtime_state.json"
            with RunLock(lock_path):
                self.assertTrue(lock_path.is_file())
                with self.assertRaisesRegex(RuntimeError, "采集器已在运行"):
                    RunLock(lock_path).acquire()
                write_runtime_state(state_path, status="running", cycle=3)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual("V6.4", state["version"])
                self.assertEqual(3, state["cycle"])
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
