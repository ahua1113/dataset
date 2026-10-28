"""服务器运行辅助：单实例锁、磁盘检查、终止信号与原子状态文件。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
from threading import Event


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RunLock:
    """通过原子创建 PID 文件，避免两个采集进程同时写入同一清单。"""

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                )
            except FileExistsError:
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                    pid = int(payload.get("pid") or 0)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pid = 0
                if _pid_alive(pid):
                    raise RuntimeError(
                        f"采集器已在运行：pid={pid}，锁文件={self.path}"
                    )
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {"pid": os.getpid(), "started_at": _now_utc()},
                    stream, ensure_ascii=False,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.acquired = True
            return
        raise RuntimeError(f"无法取得采集器锁：{self.path}")

    def release(self) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def write_runtime_state(path: Path, **state: object) -> None:
    """原子更新服务器状态，供 systemd、监控脚本或人工检查。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "V6.4",
        "updated_at": _now_utc(),
        "pid": os.getpid(),
        **state,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def free_disk_gib(path: Path) -> float:
    """返回输出文件系统的可用 GiB；目录不存在时先创建。"""
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / (1024 ** 3)


def install_stop_event() -> Event:
    """将 SIGINT/SIGTERM 转换为可中断等待的停止事件。"""
    stop_event = Event()

    def request_stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    return stop_event
