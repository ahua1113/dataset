"""视频资源采集启动脚本：调用统一入口处理公开视频。"""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "collector.py"

cmd = [
    sys.executable,
    str(COLLECTOR),
    "--categories", "videos",
    "--limit", "100",
    "--download",
    "--out", str(ROOT / "chemistry_dataset" / "videos_dataset"),
]

subprocess.run(cmd, cwd=ROOT, check=True)
