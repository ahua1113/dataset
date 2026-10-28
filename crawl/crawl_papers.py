"""论文采集启动脚本：以 V6.4 连续模式运行全学科 OA 采集。"""
from pathlib import Path
import os
import sys


ROOT = Path(__file__).resolve().parents[1]


# 统一使用合并后的论文目录；程序自动计入已有论文并更新量化报告。
def main() -> None:
    command = [
        sys.executable,
        str(ROOT / "collector.py"),
        "--categories", "papers",
        "--journal-stage", "all",
        "--download",
        "--continuous",
        "--journals-file", str(ROOT / "config" / "journals_v6_4.json"),
        "--disciplines-file", str(ROOT / "config" / "disciplines_v6_4.json"),
        "--journal-policy-file", str(ROOT / "config" / "journal_policy_v6_4.json"),
        "--out", str(ROOT / "chemistry_dataset" / "papers"),
        "--analysis-dir", str(ROOT / "chemistry_dataset" / "quantitative_analysis"),
    ]
    os.chdir(ROOT)
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
