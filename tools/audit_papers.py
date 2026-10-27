"""本地论文审计工具：在不联网的情况下检查论文文件与元数据。"""
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

cmd = [
    sys.executable,
    str(BASE_DIR / 'collector.py'),
    '--categories', 'papers',
    '--audit-only',
    '--audit-offline',
    '--legacy-arxiv-query', 'physics.chem-ph',
    '--out', str(BASE_DIR / 'chemistry_dataset' / 'papers_dataset'),
    '--contact', 'your-email@example.com',
]

subprocess.run(cmd, cwd=BASE_DIR, check=True)
