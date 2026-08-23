"""Douyin Cloud Streak：单账号抖音续火花 Web 服务入口。"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

# 确保在 Windows 控制台下输出 Unicode/Emoji 正常
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uvicorn
from fastapi import Body, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, model_validator

from core import automation, ledger, scheduler
from core.config import DATA_DIR, DEFAULT_CONFIG, load_config, save_config
from core.harvester import creator_map
from core.runtime import (
    load_harvest_last,
    load_runtime,
    recent_logs,
    record_contacts,
    record_harvest,
    record_run,
    set_running,
    setup_logging,
    update_runtime,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATE_PATH = DATA_DIR / "state.json"
PID_PATH = DATA_DIR / "server.pid"  # 单实例锁文件：防旧实例 scheduler 残留再发消息

logger = setup_logging()
run_lock = threading.Lock()
contacts_fetching = False
harvesting = False
# harvest_last 现从 runtime.json 持久化读取（服务重启后采集摘要不丢）


# ── 环境变量 ──────────────────────────────────────────────────────────────


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


# ── 认证 ──────────────────────────────────────────────────────────────────


def _check_auth(token: str) -> None:
    if AUTH_TOKEN and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="访问令牌不正确")


# ── 并发控制 ──────────────────────────────────────────────────────────────


def _acquire_lock(blocking: bool = True) -> bool:
    """获取运行锁，如果采集正在进行则返回 False。"""
    global harvesting
    if harvesting:
        raise HTTPException(status_code=409, detail="creator 采集进行中，请稍后再试")
    return run_lock.acquire(blocking=blocking)


def _release_lock() -> None:
    run_lock.release()


# ── 后台任务 ──────────────────────────────────────────────────────────────


def _start_run(dry: bool, only_names: list[str] | None = None) -> None:
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")

    def worker() -> None:
        try:
            set_running(True)
            try:
                result = automation.run_send(dry_run=dry, only_names=only_names)
                record_run(result)
                logger.info("本次发送完成：成功 %s 人，失败 %s 人，dry=%s",
                            len(result.get("ok", [])), len(result.get("failed", [])), dry)
                if not dry and result.get("failed") and not result.get("logged_out"):
                    _schedule_retry(result)
                elif not dry:
                    scheduler.cancel_retry()
            finally:
                set_running(False)
        finally:
            _release_lock()

    threading.Thread(target=worker, daemon=True).start()


def _schedule_retry(result: dict) -> None:
    """安排 45 分钟后补发失败好友。"""
    failed_names = [
        f["name"] for f in result.get("failed", [])
        if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"] != "_system"
    ]
    if not failed_names:
        return
    rt = load_runtime()
    today = datetime.now().date().isoformat()
    if rt.get("retry_date") != today:
        update_runtime(retry_date=today)
        scheduler.schedule_retry(lambda: _start_run(False, failed_names))


def _start_fetch_contacts() -> None:
    global contacts_fetching
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")

    def worker() -> None:
        global contacts_fetching
        try:
            contacts_fetching = True
            try:
                data = automation.fetch_chat_contacts()
                record_contacts(data)
                if data.get("names"):
                    stats = ledger.merge_consumer_contacts(data["names"])
                    logger.info("台账已同步：新增 %s 人，更新 %s 人，共 %s 人",
                                 stats["added"], stats["updated"], stats["total"])
            finally:
                contacts_fetching = False
        finally:
            _release_lock()

    threading.Thread(target=worker, daemon=True).start()


def _start_harvest_creator() -> None:
    """后台线程执行 creator 抖音号采集 + 台账合并（只读，不发送消息）。"""
    global harvesting
    if harvesting:
        raise HTTPException(status_code=409, detail="creator 采集已在进行中")
    if run_lock.locked():
        raise HTTPException(status_code=409, detail="发送/同步任务进行中，请稍后再试")
    harvesting = True

    def worker() -> None:
        global harvesting
        try:
            res = creator_map.collect_short_id_map()
            merge_stats = None
            if res.get("mapping"):
                merge_stats = ledger.merge_creator_map(res["mapping"])
                res["merge"] = merge_stats
                logger.info("creator 采集合并完成：%s 条映射，join %s 人，新增 %s 人，共 %s 人",
                             res["count"], merge_stats["joined"], merge_stats["added"],
                             merge_stats["total"])
            harvest_last = {
                "at": res.get("at"), "count": res.get("count"),
                "hit": res.get("hit"), "error": res.get("error"), "merge": merge_stats,
            }
            record_harvest(harvest_last)
        finally:
            harvesting = False

    threading.Thread(target=worker, daemon=True).start()


def _scheduled_harvest() -> None:
    try:
        _start_harvest_creator()
    except HTTPException as e:
        logger.warning("周级采集跳过：%s", e.detail)


# ── FastAPI ────────────────────────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """检查 PID 对应的进程是否仍在运行（跨平台）。

    Windows 上 os.kill(pid, 0) 抛 WinError 87（不是 ProcessLookupError），
    故改用 tasklist；POSIX 用 os.kill(pid, 0)。
    """
    if os.name == "nt":  # Windows
        try:
            import subprocess
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in r.stdout and "python" in r.stdout.lower()
        except Exception:
            return False
    # POSIX
    try:
        os.kill(pid, 0)  # signal 0 = 探测进程是否存在，不实际发信号
    except ProcessLookupError:
        return False  # 进程不存在
    except PermissionError:
        return True  # 进程存在但无权限发信号
    except OSError:
        return False
    return True


def _acquire_instance_lock() -> None:
    """单实例自检：若已有活跃实例运行则拒绝启动，防旧实例 scheduler 残留再发消息。

    曾发生僵尸 python 进程导致定时超发。此锁确保同一时刻只有一个 sparkkeeper 实例
    持有调度器，避免多实例重复触发发送任务。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if PID_PATH.exists():
        try:
            old_pid = int(PID_PATH.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and _pid_alive(old_pid):
            logger.error(
                "检测到已有 sparkkeeper 实例在运行（PID %s），拒绝启动。"
                "请先停止旧实例再重试，避免多实例重复发送。",
                old_pid,
            )
            raise SystemExit(f"已有实例在运行（PID {old_pid}），请先停止旧实例")
        else:
            logger.info("发现旧 PID 文件但进程已退出（PID %s），可安全接管", old_pid)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _acquire_instance_lock()
    try:
        scheduler.configure(lambda: _start_run(False), harvest_func=_scheduled_harvest)
    except Exception as e:
        logger.warning("调度器启动失败: %s", e)
    yield
    scheduler.shutdown()
    try:
        PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


app = FastAPI(title="Douyin Cloud Streak", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """禁缓存：杜绝预览/浏览器缓存旧版 HTML（如缺少二次确认框的旧版）误触真实发送。"""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ── 请求体模型 ────────────────────────────────────────────────────────────


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict

    @model_validator(mode="after")
    def _check_known_keys(self) -> "ConfigBody":
        unknown = [k for k in self.config if k not in DEFAULT_CONFIG]
        if unknown:
            raise ValueError("未知配置项：" + ", ".join(map(str, unknown)))
        return self


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry: bool | None = None
    dry_run: bool | None = None

    @model_validator(mode="after")
    def _require_dry_flag(self) -> "RunBody":
        if self.dry is None and self.dry_run is None:
            raise ValueError("必须显式指定 dry（true=干跑 / false=真实发送）")
        return self


class LedgerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[dict]


class SelectionBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    selected_names: list[str] = []


# ── API 路由 ──────────────────────────────────────────────────────────────


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


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
        "next_harvest": scheduler.next_harvest_time(),
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
    _start_fetch_contacts()
    return {"ok": True, "started": True}


@app.get("/api/ledger")
def api_ledger(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    entries = ledger.load_ledger()
    contacts = []
    for e in entries:
        name = e.get("display_name") or e.get("nickname") or ""
        spark = int(e.get("streak_days") or e.get("spark_days") or 0)
        contacts.append({
            "display_name": name,
            "nickname": name,
            "streak_days": spark,
            "spark_days": spark,
            "selected": bool(e.get("selected", False)),
            "last_status": e.get("last_status") or ("success" if e.get("last_sent_at") else "pending"),
            "last_sent_at": e.get("last_sent_at")
        })
    b_daily = rt.get("b_channel_daily") or {}
    return {
        "entries": entries,
        "contacts": contacts,
        "selected_count": sum(1 for e in entries if e.get("selected")),
        "pending_send": [
            {"display_name": e["display_name"], "send_channel": e["send_channel"]}
            for e in automation.compute_pending()
        ],
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": contacts_fetching,
        "harvesting": harvesting,
        "harvest_last": load_harvest_last(),
        "b_channel_daily": {
            "date": b_daily.get("date"),
            "count": b_daily.get("count", 0),
        },
    }


@app.post("/api/sync")
def api_sync(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """与前端同步接口对齐"""
    _check_auth(token)
    _start_fetch_contacts()
    return {"ok": True, "started": True}


@app.post("/api/ledger/selection")
def api_ledger_selection(body: SelectionBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """一键保存选中的好友列表"""
    _check_auth(token)
    selected_set = set(body.selected_names or [])
    entries = ledger.load_ledger()
    changes = []
    for e in entries:
        name = e.get("display_name") or e.get("nickname")
        if name:
            changes.append({
                "display_name": name,
                "selected": name in selected_set
            })
    stats = ledger.set_selected(changes)
    return {"ok": True, **stats}


@app.post("/api/ledger/harvest-creator")
def api_harvest_creator(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _start_harvest_creator()
    return {"ok": True, "started": True}


@app.get("/api/ledger/stats")
def api_ledger_stats(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return ledger.stats()


@app.put("/api/ledger")
def api_ledger_save(body: LedgerBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    changes: list[dict] = []
    for e in body.entries or []:
        name = str(e.get("display_name", "")).strip()
        if name and isinstance(e.get("selected"), bool):
            changes.append({
                "display_name": name,
                "selected": e["selected"],
                "selected_order": e.get("selected_order"),
            })
    stats = ledger.set_selected(changes)
    return {"ok": True, **stats}


@app.put("/api/config")
@app.post("/api/config")
def api_config_save(body: dict = Body(...), token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        raw_cfg = body.get("config") if isinstance(body, dict) and "config" in body and isinstance(body["config"], dict) else body
        cfg = save_config(raw_cfg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler.apply_schedule()
    return {"ok": True, "config": cfg}


@app.post("/api/run")
def api_run(body: RunBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    targets = ledger.get_selected()
    if not targets:
        cfg = load_config()
        if not cfg.get("friends"):
            raise HTTPException(status_code=400, detail="未勾选任何好友！请先在「好友与消息」中勾选好友后再执行。")
    _start_run(bool(body.dry or body.dry_run))
    return {"ok": True, "started": True}


@app.post("/api/reset-running")
def api_reset_running(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """强制重置运行锁与 running 状态，避免卡死。"""
    _check_auth(token)
    global harvesting, contacts_fetching
    harvesting = False
    contacts_fetching = False
    set_running(False)
    if run_lock.locked():
        try:
            run_lock.release()
        except Exception:
            pass
    logger.info("已强制重置后台运行状态")
    return {"ok": True, "message": "运行状态已强制重置"}


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
