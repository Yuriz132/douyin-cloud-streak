"""每天定时触发发送任务。"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import load_config

logger = logging.getLogger("douyin-cloud-streak")
TZ = "Asia/Shanghai"

_scheduler: BackgroundScheduler | None = None
_run_func: Callable | None = None
_harvest_func: Callable | None = None


def _daily_job() -> None:
    cfg = load_config()
    if not bool(cfg.get("auto_run_enabled", True)):
        logger.info("自动运行已关闭（auto_run_enabled=false），本次定时任务跳过")
        return
    jitter = max(0, int(cfg.get("jitter_minutes", 30) or 30))
    if jitter:
        delay = random.uniform(0, jitter * 60)
        logger.info("随机延迟 %.0f 秒后开始发送（抖动窗口 %s 分钟）", delay, jitter)
        time.sleep(delay)
    if _run_func:
        _run_func()


def configure(run_func: Callable, harvest_func: Callable | None = None) -> None:
    """注册每日发送任务与（可选）周级 creator 采集任务。"""
    global _scheduler, _run_func, _harvest_func
    _run_func = run_func
    _harvest_func = harvest_func
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TZ)
        _scheduler.start()
    apply_schedule()


def apply_schedule() -> None:
    if _scheduler is None:
        return
    cfg = load_config()
    hh, mm = cfg.get("schedule_time", "21:00").split(":")
    _scheduler.add_job(
        _daily_job,
        CronTrigger(hour=int(hh), minute=int(mm), timezone=TZ),
        id="daily_send",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.info("定时任务已更新：每天 %s:%s (%s)", hh, mm, TZ)

    # 周级 creator 抖音号采集（默认周一 03:00；off/空 = 关闭）
    day = str(cfg.get("schedule_harvest_day") or "off").strip().lower()
    if day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"} and _harvest_func:
        _scheduler.add_job(
            _harvest_func,
            CronTrigger(day_of_week=day, hour=3, minute=0, timezone=TZ),
            id="weekly_harvest",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("周级采集已更新：每周 %s 03:00 (%s)", day, TZ)
    elif _scheduler.get_job("weekly_harvest"):
        _scheduler.remove_job("weekly_harvest")
        logger.info("周级采集已关闭")


def next_run_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("daily_send")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def next_harvest_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("weekly_harvest")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def schedule_retry(run_func: Callable, delay_minutes: int = 45) -> None:
    if _scheduler is None:
        return
    if _scheduler.get_job("retry_send"):
        return
    run_at = datetime.now() + timedelta(minutes=delay_minutes)
    _scheduler.add_job(
        run_func,
        DateTrigger(run_date=run_at, timezone=TZ),
        id="retry_send",
        replace_existing=True,
    )
    logger.info("已安排 %s 分钟后自动补发本次失败的好友", delay_minutes)


def cancel_retry() -> None:
    if _scheduler and _scheduler.get_job("retry_send"):
        _scheduler.remove_job("retry_send")
        logger.info("已取消待执行的补发任务")


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
