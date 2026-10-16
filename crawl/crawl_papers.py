from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "collector_v5_1.py"

cmd = [
    sys.executable,
    str(COLLECTOR),
    "--categories", "papers",
    "--limit", "128",
    "--download",
    "--paper-sources", "arxiv,nature,science",
    "--out", str(ROOT / "chemistry_dataset" / "papers_dataset"),
    "--contact", "1316400989@qq.com",
]

subprocess.run(cmd, cwd=ROOT, check=True)
