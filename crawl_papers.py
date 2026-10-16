
import subprocess
import sys

cmd = [
    sys.executable,
    "collector.py",
    "--categories", "papers",
    "--limit", "120",
    "--download",
    "--out", "chemistry_dataset/papers_dataset",
    "--contact", "1316400989@qq.com"
]

subprocess.run(cmd, check=True)
