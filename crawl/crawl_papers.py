from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "collector.py"

# 当前论文目标：130 篇。
# 如果以后需要 200 篇，只修改 TARGET 即可。
TARGET = 300

cmd = [
    sys.executable,
    str(COLLECTOR),
    "--categories", "papers",
    "--limit", str(TARGET),
    "--download",
    "--journals-file", str(ROOT / "config" / "journals_v6.json"),
    "--out", str(ROOT / "chemistry_dataset" / "papers_dataset"),
    "--contact", "1316400989@qq.com",
]

subprocess.run(cmd, cwd=ROOT, check=True)
