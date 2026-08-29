"""运行状态与日志。运行结果持久化到账号目录 runtime.json，日志同时写文件与内存环形缓冲。"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from pathlib import Path

from .config import DATA_DIR, account_dir, DEFAULT_ACCOUNT_ID

LOG_DIR = DATA_DIR / "logs"


def runtime_path(account_id: str | None = None) -> Path:
    return account_dir(account_id) / "runtime.json"


_lock = threading.Lock()
_ring: deque[str] = deque(maxlen=600)


def _default() -> dict:
    return {"session_status": "unknown", "running": False, "last_run": None, "history": []}


def load_runtime(account_id: str | None = None) -> dict:
    rt = _default()
    rp = runtime_path(account_id)
    if rp.exists():
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                rt.update(data)
        except Exception:
            pass
    return rt


def _save(rt: dict, account_id: str | None = None) -> None:
    with _lock:
        d = account_dir(account_id)
        d.mkdir(parents=True, exist_ok=True)
        rp = runtime_path(account_id)
        rp.write_text(json.dumps(rt, ensure_ascii=False, indent=2), encoding="utf-8")


def set_running(value: bool, account_id: str | None = None) -> None:
    rt = load_runtime(account_id)
    rt["running"] = bool(value)
    _save(rt, account_id)


def record_run(result: dict, account_id: str | None = None) -> None:
    rt = load_runtime(account_id)
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
    _save(rt, account_id)


def record_contacts(data: dict, account_id: str | None = None) -> None:
    rt = load_runtime(account_id)
    rt["contacts"] = data.get("names", [])
    rt["contacts_at"] = data.get("at")
    rt["contacts_error"] = data.get("error")
    _save(rt, account_id)


def update_runtime(account_id: str | None = None, **fields) -> None:
    rt = load_runtime(account_id)
    rt.update(fields)
    _save(rt, account_id)


def record_harvest(harvest_last: dict | None, account_id: str | None = None) -> None:
    """持久化最近一次 creator 采集摘要，服务重启后不丢（台账数据本身持久化不受影响）。"""
    rt = load_runtime(account_id)
    if harvest_last is None:
        rt.pop("harvest_last", None)
    else:
        rt["harvest_last"] = harvest_last
    _save(rt, account_id)


def load_harvest_last(account_id: str | None = None) -> dict | None:
    """读取持久化的采集摘要；无记录返回 None。"""
    return load_runtime(account_id).get("harvest_last")


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _ring.append(self.format(record))
        except Exception:
            pass


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("douyin-cloud-streak")
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
