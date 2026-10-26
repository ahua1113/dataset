from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "collector.py"

# V6.2 扩刊验证目标：40 本白名单期刊，每刊至少 1 篇有效 OA 论文。
# 下一阶段会改为按学科设定目标，期刊只作为来源和溯源维度。
# TARGET = 300
PER_JOURNAL_TARGET = 10

cmd = [
    sys.executable,
    str(COLLECTOR),
    "--categories", "papers",
    # "--limit", str(TARGET),
    "--per-journal-target", str(PER_JOURNAL_TARGET),
    "--download",
    # "--dry-run",
    "--journals-file", str(ROOT / "config" / "journals_v6_2.json"),
    "--out", str(ROOT / "chemistry_dataset" / "papers_dataset"),
    "--contact", "1316400989@qq.com",
]

subprocess.run(cmd, cwd=ROOT, check=True)
