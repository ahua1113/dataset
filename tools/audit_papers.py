"""PyCharm 右键运行此文件：纯离线审计已下载的化学物理论文。
不会访问 arXiv API，不下载/删除/修改 PDF，不改动原 manifest.jsonl。
请确保早期原始采集确实使用 cat:physics.chem-ph 这一条件。
"""
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
