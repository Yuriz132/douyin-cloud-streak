"""Douyin Cloud Streak：多账号抖音续火花 Web 服务入口。

多账号模型：
- 每账号独立 state/config/ledger/runtime 数据目录；
- 全局并发信号量限制同时活跃的浏览器会话数（MAX_CONCURRENT_BROWSERS=5）；
- 调度器按账号注册定时任务。
兼容旧版：默认账号 `default` 沿用 data/ 根目录，旧数据零迁移生效。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
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
import mimetypes
mimetypes.add_type("image/webp", ".webp")
from fastapi import Body, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, model_validator

from core import accounts, automation, avatar, ledger, login_session, scheduler
from core.config import (
    DATA_DIR,
    DEFAULT_ACCOUNT_ID,
    DEFAULT_CONFIG,
    ROOT_STATE_PATH,
    account_dir,
    account_state_path,
    get_valid_state_path,
    load_config,
    save_config,
)
from core.executor import executor
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
PID_PATH = DATA_DIR / "server.pid"  # 单实例锁文件：防旧实例 scheduler 残留再发消息

logger = setup_logging()
# 并发锁：每个账号一把，防同一账号并发执行
account_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
contacts_fetching: set[str] = set()  # 正在同步联系人的账号集合
harvesting: set[str] = set()  # 正在 creator 采集的账号集合
# harvest_last 现从 runtime.json 持久化读取（服务重启后采集摘要不丢）


def _lock_for(account_id: str) -> threading.Lock:
    with _locks_guard:
        lock = account_locks.get(account_id)
        if lock is None:
            lock = threading.Lock()
            account_locks[account_id] = lock
        return lock


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


def _resolve_account(account_id: str) -> str:
    """校验账号是否存在，返回规范化 account_id。"""
    aid = (account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    if not accounts.account_exists(aid):
        raise HTTPException(status_code=404, detail=f"账号不存在：{aid}")
    return aid


# ── 并发控制 ──────────────────────────────────────────────────────────────


def _acquire_lock(account_id: str, blocking: bool = True) -> bool:
    """获取指定账号的运行锁，如果该账号正在采集则返回 False。"""
    if account_id in harvesting:
        raise HTTPException(status_code=409, detail="creator 采集进行中，请稍后再试")
    return _lock_for(account_id).acquire(blocking=blocking)


def _release_lock(account_id: str) -> None:
    _lock_for(account_id).release()


# ── 后台任务 ──────────────────────────────────────────────────────────────


def _start_run(account_id: str, dry: bool, only_names: list[str] | None = None) -> None:
    aid = _resolve_account(account_id)
    if not _acquire_lock(aid, blocking=False):
        raise HTTPException(status_code=409, detail="该账号已有任务在运行")

    def worker() -> None:
        try:
            set_running(True, aid)
            try:
                result = automation.run_send(dry_run=dry, only_names=only_names, account_id=aid)
                record_run(result, aid)
                logger.info("[%s] 本次发送完成：成功 %s 人，失败 %s 人，dry=%s",
                            aid, len(result.get("ok", [])), len(result.get("failed", [])), dry)
                if result.get("failed"):
                    _fails = []
                    for f in result.get("failed", []):
                        if isinstance(f, dict):
                            _fails.append(f"{f.get('name', '?')}: {f.get('reason', '')}")
                        else:
                            _fails.append(str(f))
                    logger.warning("[%s] 失败明细: %s", aid, " | ".join(_fails))
                if result.get("skipped"):
                    logger.info("[%s] 跳过 %s 人: %s", aid, len(result.get("skipped", [])),
                                " | ".join(str(s) for s in result.get("skipped", [])[:10]))
                if not dry and result.get("failed") and not result.get("logged_out"):
                    _schedule_retry(aid, result)
                elif not dry:
                    scheduler.cancel_retry(aid)
            finally:
                set_running(False, aid)
        finally:
            _release_lock(aid)

    executor.submit(worker)


def _schedule_retry(account_id: str, result: dict) -> None:
    """安排 45 分钟后补发失败好友。"""
    failed_names = [
        f["name"] for f in result.get("failed", [])
        if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"] != "_system"
    ]
    if not failed_names:
        return
    rt = load_runtime(account_id)
    today = datetime.now().date().isoformat()
    if rt.get("retry_date") != today:
        update_runtime(account_id, retry_date=today)
        scheduler.schedule_retry(
            lambda: _start_run(account_id, False, failed_names),
            account_id=account_id,
        )


def _start_fetch_contacts(account_id: str) -> None:
    aid = _resolve_account(account_id)
    if not _acquire_lock(aid, blocking=False):
        raise HTTPException(status_code=409, detail="该账号已有任务在运行")

    def worker() -> None:
        try:
            contacts_fetching.add(aid)
            try:
                data = automation.fetch_chat_contacts(aid)
                record_contacts(data, aid)
                if data.get("names"):
                    stats = ledger.merge_consumer_contacts(data["names"], aid)
                    logger.info("[%s] 台账已同步：新增 %s 人，更新 %s 人，共 %s 人",
                                aid, stats["added"], stats["updated"], stats["total"])
            finally:
                contacts_fetching.discard(aid)
        finally:
            _release_lock(aid)

    executor.submit(worker)


def _start_harvest_creator(account_id: str) -> None:
    """后台线程执行 creator 抖音号采集 + 台账合并（只读，不发送消息）。"""
    aid = _resolve_account(account_id)
    if aid in harvesting:
        raise HTTPException(status_code=409, detail="creator 采集已在进行中")
    if _lock_for(aid).locked():
        raise HTTPException(status_code=409, detail="发送/同步任务进行中，请稍后再试")
    harvesting.add(aid)

    def worker() -> None:
        try:
            res = creator_map.collect_short_id_map(account_id=aid)
            merge_stats = None
            if res.get("mapping"):
                merge_stats = ledger.merge_creator_map(res["mapping"], aid)
                res["merge"] = merge_stats
                logger.info("[%s] creator 采集合并完成：%s 条映射，join %s 人，新增 %s 人，共 %s 人",
                            aid, res["count"], merge_stats["joined"], merge_stats["added"],
                            merge_stats["total"])
            harvest_last = {
                "at": res.get("at"), "count": res.get("count"),
                "hit": res.get("hit"), "error": res.get("error"), "merge": merge_stats,
            }
            record_harvest(harvest_last, aid)
        finally:
            harvesting.discard(aid)

    executor.submit(worker)


def _scheduled_harvest(account_id: str) -> None:
    try:
        _start_harvest_creator(account_id)
    except HTTPException as e:
        logger.warning("[%s] 周级采集跳过：%s", account_id, e.detail)


# ── FastAPI ────────────────────────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """检查 PID 对应的进程是否仍在运行（跨平台）。"""
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


def _proc_identity(pid: int | None = None) -> tuple[str, str]:
    """返回 (主机标识, 进程启动时间)，用于识别 PID 复用与跨容器残留锁。

    容器每次重启 PID 命名空间都会重置，仅凭 PID 数字无法区分
    「旧容器残留」和「本容器自身」，需借助主机名与 /proc 启动时间。
    """
    pid = pid if pid is not None else os.getpid()
    start = ""
    if os.name == "posix":
        try:
            # stat 第22字段为 starttime(jiffies)；comm 字段可能含空格，先按右括号切
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            start = fields[19]
        except (OSError, IndexError):
            start = ""
    return socket.gethostname(), start


def _acquire_instance_lock() -> None:
    """单实例自检：若已有活跃实例运行则拒绝启动，防旧实例 scheduler 残留再发消息。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cur_host, cur_start = _proc_identity()
    if PID_PATH.exists():
        old_pid: int | None = None
        old_host, old_start = "", ""
        try:
            raw = json.loads(PID_PATH.read_text().strip())
            old_pid = int(raw["pid"])
            old_host = str(raw.get("host", ""))
            old_start = str(raw.get("start", ""))
        except (ValueError, OSError, KeyError, TypeError):
            old_pid = None  # 旧版纯数字格式无法验证来源，视为残留直接接管
        if old_pid:
            if old_host and old_host != cur_host:
                logger.info("发现其他容器/主机（%s）的残留锁（PID %s），可安全接管", old_host, old_pid)
            elif _pid_alive(old_pid) and old_start and _proc_identity(old_pid)[1] == old_start:
                # 同主机且该 PID 的启动时间与锁记录一致 → 确为旧实例本体
                logger.error(
                    "检测到已有 sparkkeeper 实例在运行（PID %s），拒绝启动。"
                    "请先停止旧实例再重试，避免多实例重复发送。",
                    old_pid,
                )
                raise SystemExit(f"已有实例在运行（PID {old_pid}），请先停止旧实例")
            else:
                logger.info("旧实例锁已失效（进程退出或 PID 被复用，PID %s），可安全接管", old_pid)
    PID_PATH.write_text(
        json.dumps({"pid": os.getpid(), "host": cur_host, "start": cur_start}),
        encoding="utf-8",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _acquire_instance_lock()
    try:
        accounts.list_accounts()  # 确保账号注册表目录就绪

        # 清理上次进程退出残留的 running 锁：进程被重启/杀死时 finally 未执行，
        # runtime.json 里 running=True 会让前端按钮永久显示"任务执行中"。
        for _acct in accounts.list_accounts():
            _aid = _acct.get("id") or "default"
            try:
                if load_runtime(_aid).get("running"):
                    set_running(False, _aid)
                    logger.warning("[%s] 检测到残留运行锁（上次进程退出未清理），已自动重置", _aid)
            except Exception:
                pass

        def _on_scheduled_outcome(account_id: str, ok: bool, detail: str) -> None:
            """定时任务触发结果写回运行时，使『上次定时发送是否成功/为何失败』在状态页可见。"""
            try:
                update_runtime(
                    account_id,
                    last_scheduled={"ok": ok, "detail": detail, "at": datetime.now().astimezone().isoformat(timespec="seconds")},
                )
            except Exception:
                pass

        scheduler.configure(
            lambda account_id: _start_run(account_id, False),
            harvest_func=_scheduled_harvest,
            on_scheduled_outcome=_on_scheduled_outcome,
        )
    except Exception as e:
        logger.warning("调度器启动失败: %s", e)
    yield
    scheduler.shutdown()
    try:
        # 在单工作线程内回收浏览器池实例，避免跨线程 close
        from core.browser import shutdown_pool
        executor.submit_and_wait(shutdown_pool, timeout=20)
    except Exception:
        pass
    executor.shutdown()
    try:
        PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


app = FastAPI(title="Douyin Cloud Streak", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/avatars", StaticFiles(directory=DATA_DIR / "avatars", check_dir=False), name="avatars")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """禁缓存：杜绝预览/浏览器缓存旧版 HTML 误触真实发送。"""
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


class CustomMessageBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    display_name: str
    message: str = ""


class AccountBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""
    device: str = ""
    enabled: bool | None = None


# ── API 路由：账号管理 ─────────────────────────────────────────────────────


@app.get("/")
def index() -> HTMLResponse:
    html_file = STATIC_DIR / "index.html"
    html_content = html_file.read_text(encoding="utf-8")
    # 自动注入云端实际生效的 AUTH_TOKEN，实现网页端零配置秒级免输令牌访问！
    token_inject = f'<script>window.__SERVER_AUTH_TOKEN__ = "{AUTH_TOKEN}";</script>'
    if "</head>" in html_content:
        html_content = html_content.replace("</head>", f"  {token_inject}\n</head>")
    return HTMLResponse(
        content=html_content,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


def _account_summary(a: dict) -> dict:
    aid = a["id"]
    rt = load_runtime(aid)
    return {
        **a,
        "session_status": rt.get("session_status", "unknown"),
        "running": rt.get("running", False),
        "last_run": rt.get("last_run"),
        "last_scheduled": rt.get("last_scheduled"),
        "next_run": scheduler.next_run_time(aid),
        "next_harvest": scheduler.next_harvest_time(aid),
        "contacts_fetching": aid in contacts_fetching,
        "harvesting": aid in harvesting,
    }


_ip_info_cache: dict = {"ts": 0.0, "data": None}


@app.get("/api/ip-info")
def api_ip_info(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """服务器出口 IP 与地理位置概要（缓存 1 小时；进程重启后自动重新查询）。

    沙箱休眠唤醒会更换容器与出口 IP，此接口让用户在概览页直观看到当前
    出口 IP 与归属地，辅助判断登录态风控风险。
    """
    _check_auth(token)
    now = time.time()
    if _ip_info_cache["data"] and now - _ip_info_cache["ts"] < 3600:
        return _ip_info_cache["data"]
    import requests as _rq
    from datetime import datetime as _dt
    info = {
        "ok": False, "ip": "", "country": "", "region": "", "city": "", "isp": "",
        "queried_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 源 1：ip-api.com（免费 HTTP，含中文归属地与运营商）
    try:
        r = _rq.get(
            "http://ip-api.com/json/?lang=zh-CN&fields=status,country,regionName,city,isp,query",
            timeout=8,
        )
        d = r.json()
        if d.get("status") == "success":
            info.update({
                "ok": True, "ip": d.get("query", ""),
                "country": d.get("country", ""), "region": d.get("regionName", ""),
                "city": d.get("city", ""), "isp": d.get("isp", ""),
            })
    except Exception as e:
        logger.info("ip-api 查询失败: %s", e)
    # 源 2：3322.org 裸 IP 兜底（仅 IP，无归属地）
    if not info["ip"]:
        try:
            r = _rq.get("http://members.3322.org/dyndns/getip", timeout=8)
            ip = (r.text or "").strip()
            if ip:
                info.update({"ok": True, "ip": ip, "country": "未知", "isp": "未知"})
        except Exception as e:
            logger.info("3322.org IP 查询失败: %s", e)
    if info["ip"]:
        # 显示友好化：常见 ISP 与地区名简化
        _isp_map = [("Tencent Cloud", "腾讯云"), ("Alibaba", "阿里云"), ("Huawei", "华为云"),
                    ("China Telecom", "中国电信"), ("China Unicom", "中国联通"), ("China Mobile", "中国移动")]
        for k, v in _isp_map:
            if k.lower() in (info["isp"] or "").lower():
                info["isp"] = v
                break
        info["country"] = (info["country"] or "").replace("中華人民共和國", "中国").replace("中华人民共和国", "中国")
        _ip_info_cache["data"] = info
        _ip_info_cache["ts"] = now
    return info


@app.get("/api/accounts")
def api_accounts(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    accts = [_account_summary(a) for a in accounts.list_accounts()]
    return {
        "accounts": accts,
        "current": DEFAULT_ACCOUNT_ID,
        "max_concurrent": accounts.MAX_CONCURRENT_BROWSERS,
        "browser_slots_available": accounts.browser_slots_available(),
        "version": "0.3.0-multi",
    }


@app.post("/api/accounts")
def api_account_create(body: AccountBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    acc = accounts.create_account(name=body.name, device=body.device)
    scheduler.apply_schedule(acc["id"])
    logger.info("已创建新账号：%s（%s）", acc["name"], acc["id"])
    return {"ok": True, "account": _account_summary(acc)}


@app.put("/api/accounts/{account_id}")
def api_account_update(
    account_id: str,
    body: AccountBody,
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    aid = _resolve_account(account_id)
    acc = accounts.update_account(aid, name=body.name, device=body.device, enabled=body.enabled)
    if acc is None:
        raise HTTPException(status_code=400, detail="账号不存在")
    scheduler.apply_schedule(aid)
    return {"ok": True, "account": _account_summary(acc)}


@app.delete("/api/accounts/{account_id}")
def api_account_delete(account_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    aid = _resolve_account(account_id)
    if aid == DEFAULT_ACCOUNT_ID:
        raise HTTPException(status_code=400, detail="默认账号不允许删除")
    if _lock_for(aid).locked() or aid in harvesting:
        raise HTTPException(status_code=409, detail="该账号正在执行任务，请稍后再试")
    login_session.cancel(aid)
    ok = accounts.remove_account(aid)
    if not ok:
        raise HTTPException(status_code=404, detail=f"账号不存在：{aid}")
    scheduler.apply_schedule(aid)
    logger.info("已删除账号：%s", aid)
    return {"ok": True}


# ── API 路由：状态 / 配置 ──────────────────────────────────────────────────


@app.get("/api/status")
def api_status(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    aid = _resolve_account(account_id)
    rt = load_runtime(aid)
    valid_state = get_valid_state_path(aid)
    return {
        "state_file_exists": valid_state is not None,
        "state_file_path": str(valid_state) if valid_state else None,
        "session_status": rt.get("session_status", "unknown"),
        "running": rt.get("running", False),
        "last_run": rt.get("last_run"),
        "last_scheduled": rt.get("last_scheduled"),
        "next_run": scheduler.next_run_time(aid),
        "next_harvest": scheduler.next_harvest_time(aid),
        "history_count": len(rt.get("history", [])),
        "auth_required": bool(AUTH_TOKEN),
        "account_id": aid,
        "version": "0.3.0-multi",
    }


@app.get("/api/config")
def api_config(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    return load_config(_resolve_account(account_id))


@app.get("/api/contacts")
def api_contacts(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    aid = _resolve_account(account_id)
    rt = load_runtime(aid)
    return {
        "contacts": rt.get("contacts", []),
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": aid in contacts_fetching,
        "account_id": aid,
    }


@app.post("/api/contacts/fetch")
def api_contacts_fetch(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    _start_fetch_contacts(_resolve_account(account_id))
    return {"ok": True, "started": True}


@app.get("/api/ledger")
def api_ledger(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    aid = _resolve_account(account_id)
    rt = load_runtime(aid)
    entries = ledger.load_ledger(aid)
    contacts = []
    for e in entries:
        name = e.get("display_name") or e.get("nickname") or ""
        spark = int(e.get("streak_days") or e.get("spark_days") or 0)
        contacts.append({
            "display_name": name,
            "nickname": name,
            "avatar": e.get("avatar") or "",
            "custom_message": e.get("custom_message") or "",
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
            for e in automation.compute_pending(account_id=aid)
        ],
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": aid in contacts_fetching,
        "harvesting": aid in harvesting,
        "harvest_last": load_harvest_last(aid),
        "b_channel_daily": {
            "date": b_daily.get("date"),
            "count": b_daily.get("count", 0),
        },
        "account_id": aid,
    }


@app.post("/api/sync")
def api_sync(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    """与前端同步接口对齐"""
    _check_auth(token)
    _start_fetch_contacts(_resolve_account(account_id))
    return {"ok": True, "started": True}


@app.post("/api/ledger/selection")
def api_ledger_selection(
    body: SelectionBody,
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    """一键保存选中的好友列表"""
    _check_auth(token)
    aid = _resolve_account(account_id)
    selected_set = set(body.selected_names or [])
    entries = ledger.load_ledger(aid)
    changes = []
    for e in entries:
        name = e.get("display_name") or e.get("nickname")
        if name:
            changes.append({
                "display_name": name,
                "selected": name in selected_set
            })
    stats = ledger.set_selected(changes, aid)
    return {"ok": True, **stats}


@app.post("/api/ledger/custom-message")
def api_ledger_custom_message(
    body: CustomMessageBody,
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    """设置或清除单个好友的专属自定义发送文案"""
    _check_auth(token)
    aid = _resolve_account(account_id)
    ok = ledger.set_custom_message(body.display_name, body.message, aid)
    if not ok:
        raise HTTPException(status_code=404, detail="未在台账中找到该好友")
    return {"ok": True, "display_name": body.display_name, "custom_message": body.message}


@app.post("/api/ledger/harvest-creator")
def api_harvest_creator(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    _start_harvest_creator(_resolve_account(account_id))
    return {"ok": True, "started": True}


@app.get("/api/ledger/stats")
def api_ledger_stats(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    return ledger.stats(_resolve_account(account_id))


@app.put("/api/ledger")
def api_ledger_save(
    body: LedgerBody,
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    aid = _resolve_account(account_id)
    changes: list[dict] = []
    for e in body.entries or []:
        name = str(e.get("display_name", "")).strip()
        if name and isinstance(e.get("selected"), bool):
            changes.append({
                "display_name": name,
                "selected": e["selected"],
                "selected_order": e.get("selected_order"),
            })
    stats = ledger.set_selected(changes, aid)
    return {"ok": True, **stats}


@app.put("/api/config")
@app.post("/api/config")
def api_config_save(
    body: dict = Body(...),
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    aid = _resolve_account(account_id)
    try:
        raw_cfg = body.get("config") if isinstance(body, dict) and "config" in body and isinstance(body["config"], dict) else body
        cfg = save_config(raw_cfg, aid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler.apply_schedule(aid)
    return {"ok": True, "config": cfg}


@app.post("/api/run")
def api_run(
    body: RunBody,
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    aid = _resolve_account(account_id)
    targets = ledger.get_selected(aid)
    if not targets:
        cfg = load_config(aid)
        if not cfg.get("friends"):
            raise HTTPException(status_code=400, detail="未勾选任何好友！请先在「好友与消息」中勾选好友后再执行。")
    _start_run(aid, bool(body.dry or body.dry_run))
    return {"ok": True, "started": True}


@app.post("/api/reset-running")
def api_reset_running(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    """强制重置运行锁与 running 状态，避免卡死。"""
    _check_auth(token)
    aid = _resolve_account(account_id)
    harvesting.discard(aid)
    contacts_fetching.discard(aid)
    set_running(False, aid)
    lock = _lock_for(aid)
    if lock.locked():
        try:
            lock.release()
        except Exception:
            pass
    logger.info("[%s] 已强制重置后台运行状态", aid)
    return {"ok": True, "message": "运行状态已强制重置"}


@app.post("/api/login/start")
def api_login_start(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    """网页端扫码登录：为当前账号启动无头浏览器并生成登录二维码。"""
    _check_auth(token)
    aid = _resolve_account(account_id)
    if _lock_for(aid).locked():
        raise HTTPException(status_code=409, detail="该账号正在执行任务，请稍后再试")
    res = login_session.start(aid)
    logger.info("[%s] 已发起网页扫码登录", aid)
    return res


@app.get("/api/login/status")
def api_login_status(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    """轮询扫码会话状态与二维码（status: idle/queuing/starting/waiting_scan/success/failed/expired/cancelled）。"""
    _check_auth(token)
    aid = _resolve_account(account_id)
    return login_session.status(aid)


@app.post("/api/login/cancel")
def api_login_cancel(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    aid = _resolve_account(account_id)
    return login_session.cancel(aid)


@app.post("/api/login/check")
def api_login_check(
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    """实时检测登录态：真实打开浏览器访问抖音验证（Cookie 未过期 ≠ 服务端认可）。

    检测结果同步写回 runtime.session_status，控制台状态立即反映真实情况。
    浏览器操作必须经 executor 单工作线程执行（sync Playwright 实例绑定创建线程，
    直接在请求线程打开浏览器会污染实例池，导致后续发送任务跨线程崩溃）。
    """
    _check_auth(token)
    aid = _resolve_account(account_id)
    if not _acquire_lock(aid, blocking=False):
        raise HTTPException(status_code=409, detail="该账号正在执行任务，请稍后再试")

    def _probe() -> dict:
        state = get_valid_state_path(aid)
        if not state:
            return {"logged_in": False, "status": "expired", "reason": "本账号尚无登录态文件，请先扫码登录"}
        from core.browser import open_browser

        with open_browser(state_path=state) as (p, browser, context, page):
            if not automation._open_chat_page(page):
                return {"logged_in": False, "status": "expired", "reason": "私信页打开失败，登录态可能已失效"}
            time.sleep(3)
            automation._dismiss_dialogs(page)
            logged, why = automation.check_login(page)
        if logged:
            logger.info("[%s] 登录态实时检测：有效", aid)
            return {"logged_in": True, "status": "ok", "reason": "登录态有效"}
        logger.warning("[%s] 登录态实时检测：已失效（%s）", aid, why)
        return {"logged_in": False, "status": "expired", "reason": why}

    try:
        res = executor.submit_and_wait(_probe, timeout=180)
        update_runtime(aid, session_status=res.get("status", "unknown"))
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[%s] 登录态检测异常: %s", aid, e)
        return {"logged_in": False, "status": "unknown", "reason": f"检测异常: {e}"}
    finally:
        _release_lock(aid)


@app.post("/api/upload-state")
@app.post("/api/credentials/upload")
async def api_upload_state(
    file: UploadFile = File(...),
    token: str = Header(default="", alias="X-Auth-Token"),
    account_id: str | None = None,
) -> dict:
    _check_auth(token)
    aid = _resolve_account(account_id)
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="不是合法的 JSON 文件")
    if not isinstance(data.get("cookies"), list) or not data["cookies"]:
        raise HTTPException(status_code=400, detail="缺少 cookies 字段，请确认是 Playwright 导出的登录态文件")
    d = account_dir(aid)
    d.mkdir(parents=True, exist_ok=True)
    state_path = account_state_path(aid)
    state_path.write_bytes(raw)
    if aid == DEFAULT_ACCOUNT_ID:
        try:
            ROOT_STATE_PATH.write_bytes(raw)
        except Exception:
            pass
    logger.info("[%s] 已更新登录态 state.json（%s 字节）", aid, len(raw))
    return {"ok": True, "size": len(raw)}


@app.get("/api/logs")
def api_logs(
    n: int = 300,
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    return {"logs": "\n".join(recent_logs(max(10, min(n, 600))))}


def _port_in_use(port: int) -> bool:
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _find_free_port(start: int) -> int:
    for _ in range(50):
        if not _port_in_use(start):
            return start
        start += 1
    return start


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    if _port_in_use(port):
        port = _find_free_port(port + 1)
        print(f"[提示] 端口 {os.environ.get('PORT', '8000')} 已被占用（可能是上次后台未关闭），本次改用端口 {port}。")
        print(f"[提示] 请在浏览器访问 http://127.0.0.1:{port}")
    if os.environ.get("AUTO_OPEN_BROWSER", "1") != "0":
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
