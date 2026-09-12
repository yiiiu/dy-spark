"""配置读写。配置保存在 data/config.json，由网页端编辑。"""

from __future__ import annotations

import json
import hashlib
import threading
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "schedule_time": "21:00",   # 每天发送时间 HH:MM（服务器时区 Asia/Shanghai）
    "jitter_minutes": 30,       # 时间抖动窗口：实际在 [schedule_time, schedule_time+30min] 内随机开始
    "send_gap_min": 6,          # 相邻两个好友之间的最小间隔（秒）
    "send_gap_max": 12,         # 相邻两个好友之间的最大间隔（秒）
    "max_friends_per_run": 20,  # 每次最多发送的好友数（0 表示不限制）
    "friends": [],              # 好友列表：聊天列表里显示的备注 / 昵称 / 抖音号
    "messages": ["🔥 续火花", "晚安，明天见", "今天也要开心哦"],
    "video_mode_enabled": False,
    "video_queue": [],
}

_lock = threading.Lock()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_CONFIG)
    if cfg:
        merged.update(cfg)

    merged["friends"] = [str(x).strip() for x in merged.get("friends", []) if str(x).strip()]
    merged["messages"] = [str(x) for x in merged.get("messages", []) if str(x).strip()]
    if not merged["messages"]:
        merged["messages"] = ["🔥"]

    merged["video_mode_enabled"] = bool(merged.get("video_mode_enabled", False))
    queue = []
    seen = set()
    for raw in merged.get("video_queue", []) or []:
        if isinstance(raw, str):
            raw = {"url": raw}
        if not isinstance(raw, dict):
            continue
        url = normalize_video_url(raw.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        queue.append({
            "id": str(raw.get("id") or video_id(url)),
            "url": url,
            "title": str(raw.get("title") or "").strip()[:200],
            "source": str(raw.get("source") or "manual").strip()[:20],
            "added_at": str(raw.get("added_at") or ""),
        })
    merged["video_queue"] = queue

    schedule = str(merged.get("schedule_time", "21:00"))
    try:
        hh, mm = schedule.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
        merged["schedule_time"] = f"{int(hh):02d}:{int(mm):02d}"
    except Exception:
        raise ValueError("schedule_time 必须是 HH:MM 格式")

    for key in ("jitter_minutes", "send_gap_min", "send_gap_max", "max_friends_per_run"):
        try:
            merged[key] = max(0, int(merged.get(key, DEFAULT_CONFIG[key])))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")
    if merged["send_gap_max"] < merged["send_gap_min"]:
        merged["send_gap_max"] = merged["send_gap_min"]

    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def normalize_video_url(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.lower().split(":", 1)[0]
    if not (host == "douyin.com" or host.endswith(".douyin.com")):
        return ""
    return value.split("#", 1)[0]


def video_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
