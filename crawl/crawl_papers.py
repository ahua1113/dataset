"""论文采集启动脚本：使用 V6.3 配置运行按学科配额的正式采集流程。"""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


# 统一指定期刊配置、学科规则和独立输出目录，避免混用旧版本数据。
def main() -> None:
    command = [
        sys.executable,
        str(ROOT / "collector.py"),
        "--categories", "papers",
        "--journal-stage", "all",
        "--download",
        "--journals-file", str(ROOT / "config" / "journals_v6_3.json"),
        "--disciplines-file", str(ROOT / "config" / "disciplines_v6_3.json"),
        "--journal-policy-file", str(ROOT / "config" / "journal_policy_v6_3.json"),
        "--out", str(ROOT / "chemistry_dataset" / "v6_3_papers"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
