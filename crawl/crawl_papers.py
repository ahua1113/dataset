from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "collector.py"

# V6.1 可行性目标：3 本白名单期刊，每刊至少 3 篇有效 OA 论文。
# TARGET = 300
PER_JOURNAL_TARGET = 3

cmd = [
    sys.executable,
    str(COLLECTOR),
    "--categories", "papers",
    # "--limit", str(TARGET),
    "--per-journal-target", str(PER_JOURNAL_TARGET),
    "--download",
    "--journals-file", str(ROOT / "config" / "journals_v6_1.json"),
    "--out", str(ROOT / "chemistry_dataset" / "papers_dataset"),
    "--contact", "1316400989@qq.com",
]

subprocess.run(cmd, cwd=ROOT, check=True)
