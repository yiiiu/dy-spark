"""运行状态与日志。运行结果持久化到 data/runtime.json，日志同时写文件与内存环形缓冲。"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from pathlib import Path

from .config import DATA_DIR

RUNTIME_PATH = DATA_DIR / "runtime.json"
LOG_DIR = DATA_DIR / "logs"

_lock = threading.Lock()
_ring: deque[str] = deque(maxlen=600)


def _default() -> dict:
    return {
        "session_status": "unknown",
        "running": False,
        "last_run": None,
        "history": [],
        "video_queue_cursor": 0,
        "video_deliveries": [],
        "liked_videos": [],
        "liked_videos_at": None,
        "liked_videos_error": None,
        "liked_videos_fetching": False,
    }


def load_runtime() -> dict:
    rt = _default()
    if RUNTIME_PATH.exists():
        try:
            data = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                rt.update(data)
        except Exception:
            pass
    return rt


def _save(rt: dict) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_PATH.write_text(json.dumps(rt, ensure_ascii=False, indent=2), encoding="utf-8")


def set_running(value: bool) -> None:
    rt = load_runtime()
    rt["running"] = bool(value)
    _save(rt)


def record_run(result: dict) -> None:
    rt = load_runtime()
    rt["last_run"] = result
    history = rt.get("history", [])
    history.insert(0, result)
    rt["history"] = history[:30]

    if result.get("logged_out"):
        rt["session_status"] = "expired"
    elif result.get("ok") and not result.get("failed"):
        rt["session_status"] = "ok"
    elif result.get("ok"):
        rt["session_status"] = "partial"
    elif not result.get("failed"):
        rt["session_status"] = "ok"
    else:
        rt["session_status"] = "failed"
    _save(rt)


def record_contacts(data: dict) -> None:
    rt = load_runtime()
    rt["contacts"] = data.get("names", [])
    rt["contacts_at"] = data.get("at")
    rt["contacts_error"] = data.get("error")
    _save(rt)


def update_runtime(**fields) -> None:
    rt = load_runtime()
    rt.update(fields)
    _save(rt)


def video_was_sent(video_id: str, name: str) -> bool:
    rt = load_runtime()
    return any(
        isinstance(item, dict)
        and item.get("video_id") == video_id
        and item.get("name") == name
        for item in rt.get("video_deliveries", [])
    )


def record_video_delivery(video_id: str, name: str, *, fallback: bool = False, at: str | None = None) -> None:
    rt = load_runtime()
    deliveries = [x for x in rt.get("video_deliveries", []) if isinstance(x, dict)]
    if not any(x.get("video_id") == video_id and x.get("name") == name for x in deliveries):
        deliveries.append({"video_id": video_id, "name": name, "fallback": bool(fallback), "at": at})
    rt["video_deliveries"] = deliveries[-1000:]
    _save(rt)


def advance_video_cursor(queue_length: int) -> int:
    rt = load_runtime()
    if queue_length <= 0:
        rt["video_queue_cursor"] = 0
    else:
        rt["video_queue_cursor"] = (int(rt.get("video_queue_cursor", 0) or 0) + 1) % queue_length
    _save(rt)
    return int(rt["video_queue_cursor"])


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _ring.append(self.format(record))
        except Exception:
            pass


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("douyin-spark")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    rh = RingHandler()
    rh.setFormatter(fmt)
    logger.addHandler(rh)
    return logger


def recent_logs(n: int = 300) -> list[str]:
    return list(_ring)[-n:]
