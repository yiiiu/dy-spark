"""Douyin Spark Keeper：单账号抖音续火花 Web 服务入口。"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import automation, scheduler
from core.config import DATA_DIR, load_config, save_config
from core.runtime import (
    load_runtime,
    recent_logs,
    record_contacts,
    record_run,
    set_running,
    setup_logging,
    update_runtime,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATE_PATH = DATA_DIR / "state.json"


def _load_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env()
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()
logger = setup_logging()

run_lock = threading.Lock()
contacts_fetching = False
likes_fetching = False


def _check_auth(token: str) -> None:
    if AUTH_TOKEN and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="访问令牌不正确")


def _start_run(
    dry: bool,
    only_names: list[str] | None = None,
    force_video: dict | None = None,
    advance_video: bool = False,
) -> None:
    if not run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")

    def worker() -> None:
        try:
            set_running(True)
            try:
                result = automation.run_send(
                    dry_run=dry,
                    only_names=only_names,
                    force_video=force_video,
                    advance_video=advance_video,
                )
                record_run(result)
                logger.info(
                    "本次发送完成：成功 %s 人，失败 %s 人，dry=%s",
                    len(result.get("ok", [])),
                    len(result.get("failed", [])),
                    dry,
                )
                if not dry and result.get("failed") and not result.get("logged_out"):
                    failed_names = [
                        f["name"]
                        for f in result.get("failed", [])
                        if isinstance(f, dict)
                        and isinstance(f.get("name"), str)
                        and f["name"] != "_system"
                    ]
                    if failed_names:
                        rt = load_runtime()
                        today = datetime.now().date().isoformat()
                        if rt.get("retry_date") != today:
                            update_runtime(retry_date=today)
                            retry_video = result.get("video") if isinstance(result.get("video"), dict) else None
                            scheduler.schedule_retry(lambda: _start_run(False, failed_names, retry_video, False))
                elif not dry:
                    scheduler.cancel_retry()
            finally:
                set_running(False)
        finally:
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def _start_fetch_contacts() -> None:
    global contacts_fetching
    if not run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")

    def worker() -> None:
        global contacts_fetching
        try:
            contacts_fetching = True
            try:
                record_contacts(automation.fetch_chat_contacts())
            except Exception as e:
                logger.error("后台获取联系人异常: %s", e)
                record_contacts({
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "names": [],
                    "error": str(e),
                })
            finally:
                contacts_fetching = False
        finally:
            run_lock.release()

    contacts_fetching = True
    threading.Thread(target=worker, daemon=True).start()


def _start_fetch_likes() -> None:
    global likes_fetching
    if not run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")

    def worker() -> None:
        global likes_fetching
        try:
            likes_fetching = True
            try:
                result = automation.fetch_liked_videos(limit=20)
            except Exception as e:  # pragma: no cover - defensive worker boundary
                result = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "videos": [], "error": str(e)}
            update_runtime(
                liked_videos=result.get("videos", []),
                liked_videos_at=result.get("at"),
                liked_videos_error=result.get("error"),
                liked_videos_fetching=False,
            )
        finally:
            likes_fetching = False
            run_lock.release()

    update_runtime(liked_videos_fetching=True, liked_videos_error=None)
    threading.Thread(target=worker, daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    pid_path = DATA_DIR / "app.pid"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        scheduler.configure(lambda: _start_run(False))
    except Exception as e:  # pragma: no cover
        logger.warning("调度器启动失败: %s", e)
    yield
    scheduler.shutdown()
    if pid_path.exists():
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass


app = FastAPI(title="Douyin Spark Keeper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ConfigBody(BaseModel):
    config: dict


class RunBody(BaseModel):
    dry: bool = False


class VideoQueueBody(BaseModel):
    videos: list[dict] = []


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    ico_path = STATIC_DIR / "favicon.ico"
    if ico_path.exists():
        return FileResponse(ico_path, media_type="image/x-icon")
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/status")
def api_status(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    return {
        "state_file_exists": STATE_PATH.exists(),
        "session_status": rt.get("session_status", "unknown"),
        "running": rt.get("running", False),
        "last_run": rt.get("last_run"),
        "next_run": scheduler.next_run_time(),
        "history_count": len(rt.get("history", [])),
        "auth_required": bool(AUTH_TOKEN),
        "version": "0.1.0",
    }


@app.get("/api/config")
def api_config(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return load_config()


@app.get("/api/contacts")
def api_contacts(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    return {
        "contacts": rt.get("contacts", []),
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": contacts_fetching,
    }


@app.post("/api/contacts/fetch")
def api_contacts_fetch(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        _start_fetch_contacts()
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.put("/api/config")
def api_config_save(body: ConfigBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        cfg = save_config(body.config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler.apply_schedule()
    return {"ok": True, "config": cfg}


@app.post("/api/run")
def api_run(body: RunBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        _start_run(bool(body.dry))
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.get("/api/videos/likes")
def api_video_likes(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    return {
        "videos": rt.get("liked_videos", []),
        "at": rt.get("liked_videos_at"),
        "error": rt.get("liked_videos_error"),
        "fetching": likes_fetching or bool(rt.get("liked_videos_fetching")),
    }


@app.post("/api/videos/likes/fetch")
def api_video_likes_fetch(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _start_fetch_likes()
    return {"ok": True, "started": True}


@app.get("/api/videos/queue")
def api_video_queue(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    cfg = load_config()
    rt = load_runtime()
    queue = cfg.get("video_queue") or []
    cursor = int(rt.get("video_queue_cursor", 0) or 0)
    current = queue[cursor % len(queue)] if queue else None
    next_item = queue[(cursor + 1) % len(queue)] if len(queue) > 1 else None
    sent = [x for x in rt.get("video_deliveries", []) if isinstance(x, dict)]
    return {
        "enabled": bool(cfg.get("video_mode_enabled")),
        "videos": queue,
        "cursor": cursor,
        "current": current,
        "next": next_item,
        "sent_count": len(sent),
    }


@app.put("/api/videos/queue")
def api_video_queue_save(body: VideoQueueBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    cfg = load_config()
    try:
        saved = save_config({**cfg, "video_queue": body.videos})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    rt = load_runtime()
    if saved["video_queue"]:
        update_runtime(video_queue_cursor=min(int(rt.get("video_queue_cursor", 0) or 0), len(saved["video_queue"]) - 1))
    else:
        update_runtime(video_queue_cursor=0)
    return api_video_queue(token)


@app.post("/api/videos/run")
def api_video_run(body: RunBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    cfg = load_config()
    queue = cfg.get("video_queue") or []
    if not queue:
        raise HTTPException(status_code=400, detail="视频队列为空")
    rt = load_runtime()
    cursor = int(rt.get("video_queue_cursor", 0) or 0) % len(queue)
    _start_run(bool(body.dry), force_video=queue[cursor], advance_video=True)
    return {"ok": True, "started": True, "video": queue[cursor]}


@app.post("/api/upload-state")
async def api_upload_state(
    file: UploadFile = File(...),
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="不是合法的 JSON 文件")
    if not isinstance(data.get("cookies"), list) or not data["cookies"]:
        raise HTTPException(status_code=400, detail="缺少 cookies 字段，请确认是 Playwright 导出的登录态文件")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_bytes(raw)
    logger.info("已更新登录态 state.json（%s 字节）", len(raw))
    return {"ok": True, "size": len(raw)}


@app.get("/api/logs")
def api_logs(n: int = 300, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return {"logs": "\n".join(recent_logs(max(10, min(n, 600))))}


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")
