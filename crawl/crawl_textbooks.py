"""教材资源采集启动脚本：使用教材种子配置运行对应采集流程。"""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "collector.py"

cmd = [
    sys.executable,
    str(COLLECTOR),
    "--categories", "textbooks",
    "--limit", "100",
    "--download",
    "--books-csv", str(ROOT / "config" / "textbook_seeds.csv"),
    "--out", str(ROOT / "chemistry_dataset" / "textbooks_dataset"),
]

subprocess.run(cmd, cwd=ROOT, check=True)
