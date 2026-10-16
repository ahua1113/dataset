
import subprocess
import sys

cmd = [
    sys.executable,
    "collector.py",
    "--categories", "textbooks",
    "--limit", "1",
    "--download",
    "--out", "chemistry_dataset/textbooks_dataset",
    "--books-csv", "textbook_seeds.csv",
    "--contact", "1316400989@qq.com"
]

subprocess.run(cmd, check=True)
