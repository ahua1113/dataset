"""公开数据集采集器共享的状态、文件、日志和进程控制工具。"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sys
from typing import Any, Iterable, Iterator


_STOP_REQUESTED = False


class CollectorError(RuntimeError):
    """可预期的采集错误。"""


class CollectorLockError(CollectorError):
    """另一个公开数据集采集进程仍在运行。"""


class HttpRequestError(CollectorError):
    def __init__(self, status_code: int, message: str, url: str) -> None:
        super().__init__(f"HTTP {status_code}: {message} ({url})")
        self.status_code = status_code
        self.url = url


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def stop_requested() -> bool:
    return _STOP_REQUESTED


def reset_stop_request() -> None:
    """仅供新进程初始化与测试使用。"""
    global _STOP_REQUESTED
    _STOP_REQUESTED = False


def install_signal_handlers() -> None:
    def request_stop(signum: int, _frame: Any) -> None:
        global _STOP_REQUESTED
        _STOP_REQUESTED = True
        logging.warning("收到信号 %s，将在当前安全边界停止", signum)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a+b") as handle:
        needs_separator = False
        if handle.seek(0, os.SEEK_END):
            handle.seek(-1, os.SEEK_END)
            needs_separator = handle.read(1) != b"\n"
        for row in rows:
            encoded_row = (json_dumps(row) + "\n").encode("utf-8")
            if needs_separator:
                handle.write(b"\n")
                needs_separator = False
            handle.write(encoded_row)
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        logging.warning("状态文件无法读取，将重新生成：%s", path)
        return default


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def free_disk_gib(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / (1024**3)


def require_free_disk(path: Path, minimum_gib: float) -> None:
    available = free_disk_gib(path)
    if available < minimum_gib:
        raise CollectorError(
            f"磁盘可用空间仅 {available:.2f} GiB，低于阈值 {minimum_gib:.2f} GiB"
        )


def configure_logging(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(
        logging.FileHandler(root / "public_dataset_collector.log", encoding="utf-8")
    )
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def pid_is_running(pid: int) -> bool:
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


@contextlib.contextmanager
def process_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        state = load_json(path, {})
        previous_pid = int(state.get("pid", 0) or 0)
        if pid_is_running(previous_pid):
            raise CollectorLockError(
                f"公开数据集采集器已在运行（PID {previous_pid}）。锁文件：{path}"
            )
        logging.warning("移除失效锁文件：%s", path)
        path.unlink(missing_ok=True)

    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        payload = json_dumps({"pid": os.getpid(), "created_at": utc_now()}) + "\n"
        os.write(descriptor, payload.encode("utf-8"))
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
