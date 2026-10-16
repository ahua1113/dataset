from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "collector.py"

cmd = [
    sys.executable,
    str(COLLECTOR),
    "--categories", "methods",
    "--limit", "100",
    "--download",
    "--out", str(ROOT / "chemistry_dataset" / "methods_dataset"),
    "--contact", "1316400989@qq.com",
]

subprocess.run(cmd, cwd=ROOT, check=True)
