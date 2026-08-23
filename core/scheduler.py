"""每天定时触发发送任务（按账号独立注册 job）。"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .accounts import list_accounts
from .config import DEFAULT_ACCOUNT_ID, load_config

logger = logging.getLogger("douyin-cloud-streak")
TZ = "Asia/Shanghai"

_scheduler: BackgroundScheduler | None = None
_run_func: Callable | None = None
_harvest_func: Callable | None = None


def _job_id(account_id: str, kind: str) -> str:
    return f"{kind}_{account_id}"


def _daily_job(account_id: str) -> None:
    cfg = load_config(account_id)
    if not bool(cfg.get("auto_run_enabled", True)):
        logger.info("[%s] 自动运行已关闭（auto_run_enabled=false），本次定时任务跳过", account_id)
        return
    jitter = max(0, int(cfg.get("jitter_minutes", 30) or 30))
    if jitter:
        delay = random.uniform(0, jitter * 60)
        logger.info("[%s] 随机延迟 %.0f 秒后开始发送（抖动窗口 %s 分钟）", account_id, delay, jitter)
        time.sleep(delay)
    if _run_func:
        _run_func(account_id=account_id)


def _harvest_job(account_id: str) -> None:
    if _harvest_func:
        _harvest_func(account_id=account_id)


def configure(run_func: Callable, harvest_func: Callable | None = None) -> None:
    """注册每日发送任务与（可选）周级 creator 采集任务，按账号逐个注册。"""
    global _scheduler, _run_func, _harvest_func
    _run_func = run_func
    _harvest_func = harvest_func
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TZ)
        _scheduler.start()
    apply_schedule()


def apply_schedule(account_id: str | None = None) -> None:
    """按账号应用/更新定时任务。account_id 为 None 时对全部账号执行。"""
    if _scheduler is None:
        return

    accounts = list_accounts()
    if account_id is not None:
        accounts = [a for a in accounts if a["id"] == account_id]
    if not accounts:
        return

    for acc in accounts:
        aid = acc["id"]
        if not acc.get("enabled", True):
            _scheduler.remove_job(_job_id(aid, "daily_send"))
            _scheduler.remove_job(_job_id(aid, "weekly_harvest"))
            logger.info("[%s] 账号已停用，定时任务已移除", aid)
            continue

        cfg = load_config(aid)
        hh, mm = cfg.get("schedule_time", "21:00").split(":")
        _scheduler.add_job(
            _daily_job,
            CronTrigger(hour=int(hh), minute=int(mm), timezone=TZ),
            args=[aid],
            id=_job_id(aid, "daily_send"),
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("[%s] 定时任务已更新：每天 %s:%s (%s)", aid, hh, mm, TZ)

        # 周级 creator 抖音号采集（默认周一 03:00；off/空 = 关闭）
        day = str(cfg.get("schedule_harvest_day") or "off").strip().lower()
        if day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"} and _harvest_func:
            _scheduler.add_job(
                _harvest_job,
                CronTrigger(day_of_week=day, hour=3, minute=0, timezone=TZ),
                args=[aid],
                id=_job_id(aid, "weekly_harvest"),
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=3600,
            )
            logger.info("[%s] 周级采集已更新：每周 %s 03:00 (%s)", aid, day, TZ)
        else:
            _scheduler.remove_job(_job_id(aid, "weekly_harvest"))
            if day not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
                logger.info("[%s] 周级采集已关闭", aid)


def _next_run(job) -> str | None:
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def next_run_time(account_id: str | None = None) -> str | None:
    if _scheduler is None:
        return None
    if account_id is None:
        return next_run_time(DEFAULT_ACCOUNT_ID)
    return _next_run(_scheduler.get_job(_job_id(account_id, "daily_send")))


def next_harvest_time(account_id: str | None = None) -> str | None:
    if _scheduler is None:
        return None
    if account_id is None:
        return next_harvest_time(DEFAULT_ACCOUNT_ID)
    return _next_run(_scheduler.get_job(_job_id(account_id, "weekly_harvest")))


def schedule_retry(run_func: Callable, delay_minutes: int = 45, account_id: str | None = None) -> None:
    if _scheduler is None:
        return
    job_id = _job_id(account_id or DEFAULT_ACCOUNT_ID, "retry")
    if _scheduler.get_job(job_id):
        return
    run_at = datetime.now() + timedelta(minutes=delay_minutes)
    _scheduler.add_job(
        run_func,
        DateTrigger(run_date=run_at, timezone=TZ),
        id=job_id,
        replace_existing=True,
    )
    logger.info("[%s] 已安排 %s 分钟后自动补发本次失败的好友", account_id or DEFAULT_ACCOUNT_ID, delay_minutes)


def cancel_retry(account_id: str | None = None) -> None:
    job_id = _job_id(account_id or DEFAULT_ACCOUNT_ID, "retry")
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info("[%s] 已取消待执行的补发任务", account_id or DEFAULT_ACCOUNT_ID)


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
