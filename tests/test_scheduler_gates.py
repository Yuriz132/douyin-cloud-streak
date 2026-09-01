"""离线验证调度闸门与默认账号启停（不启动浏览器、不落真实注册表）。

覆盖用户反馈的两个 bug：
1. 定时开关关闭后仍会定时发送（补发 job 绕过开关 / 抖动窗口期关闭）
2. 默认账号（default）无法停用

运行：python3 tests/test_scheduler_gates.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import accounts, scheduler

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    print(("PASS" if cond else "FAIL"), name)
    PASS += cond
    FAIL += (not cond)


def make_cfg(auto=True, jitter=0, harvest_day="off"):
    return {"auto_run_enabled": auto, "jitter_minutes": jitter, "schedule_harvest_day": harvest_day, "schedule_time": "21:00"}


def make_accounts(*pairs):
    """pairs: [(id, enabled), ...] 构造假账号列表。"""
    return [
        {"id": aid, "name": aid, "enabled": bool(en), "is_default": aid == "default"}
        for aid, en in pairs
    ]


ALL_ON = lambda: make_accounts(("default", True), ("a1", True))          # noqa: E731
A1_OFF = lambda: make_accounts(("default", True), ("a1", False))         # noqa: E731
DEFAULT_OFF = lambda: make_accounts(("default", False), ("a1", True))    # noqa: E731


# ── 环境隔离：注册表写到临时目录，sleep 打桩 ───────────────────
tmp = Path(tempfile.mkdtemp(prefix="sched_gates_test_"))
accounts.REGISTRY_PATH = tmp / "accounts.json"

orig_load_config = scheduler.load_config
orig_list_accounts = scheduler.list_accounts
orig_sleep = scheduler.time.sleep
slept = []
scheduler.time.sleep = lambda sec: slept.append(sec)

try:
    # ── 1. _daily_job 闸门 ──────────────────────────────────
    fired, outcomes = [], []
    scheduler._run_func = lambda account_id=None: fired.append(account_id)
    scheduler._on_outcome = lambda aid, ok, detail: outcomes.append((aid, ok, detail))

    scheduler.list_accounts = ALL_ON
    scheduler.load_config = lambda aid=None: make_cfg(auto=False)
    scheduler._daily_job("a1")
    check("D1 开关关闭: 不触发发送", fired == [])
    check("D2 开关关闭: outcome 记录 auto_run_enabled=false", outcomes and outcomes[-1][2] == "auto_run_enabled=false")

    scheduler.list_accounts = A1_OFF
    scheduler.load_config = lambda aid=None: make_cfg(auto=True)
    scheduler._daily_job("a1")
    check("D3 账号停用: 不触发发送", fired == [])
    check("D4 账号停用: outcome 记录停用原因", outcomes and outcomes[-1][2] == "账号已停用")

    scheduler.list_accounts = ALL_ON
    scheduler._daily_job("a1")
    check("D5 启用+开关开: 正常触发", fired == ["a1"])

    # ── 2. 抖动窗口期关闭开关（二次确认）─────────────────────
    fired.clear()
    calls = {"n": 0}

    def cfg_switch_after_sleep(aid=None):
        calls["n"] += 1
        return make_cfg(auto=calls["n"] <= 1, jitter=5)

    scheduler.load_config = cfg_switch_after_sleep
    scheduler._daily_job("a1")
    check("D6 抖动窗口: sleep 被执行", len(slept) >= 1)
    check("D7 抖动期间关开关: 二次确认拦截, 不触发发送", fired == [])
    check("D8 抖动期间关开关: outcome 记录拦截", outcomes and outcomes[-1][2] == "auto_run_enabled=false")

    # ── 3. 补发 job 闸门（bug1 主因）─────────────────────────
    fired.clear()
    scheduler.load_config = lambda aid=None: make_cfg(auto=False, jitter=0)
    scheduler._retry_job(lambda: fired.append("retry"), "a1")
    check("R1 开关关闭: 补发不执行", fired == [])
    check("R2 开关关闭: 补发跳过写 outcome", outcomes and "补发跳过" in outcomes[-1][2])

    scheduler.list_accounts = A1_OFF
    scheduler.load_config = lambda aid=None: make_cfg(auto=True, jitter=0)
    scheduler._retry_job(lambda: fired.append("retry"), "a1")
    check("R3 账号停用: 补发不执行", fired == [])
    check("R4 账号停用: 补发跳过写 outcome", outcomes and "补发跳过" in outcomes[-1][2])

    scheduler.list_accounts = ALL_ON
    scheduler._retry_job(lambda: fired.append("retry"), "a1")
    check("R5 正常状态: 补发执行", fired == ["retry"])

    # ── 4. schedule_retry 注册的是带闸门的包装 job ────────────
    scheduler.load_config = lambda aid=None: make_cfg(auto=False, jitter=0)
    scheduler.configure(run_func=lambda account_id=None: fired.append("scheduled"))
    scheduler.schedule_retry(lambda: fired.append("retry2"), delay_minutes=45, account_id="a1")
    job = scheduler._scheduler.get_job("retry_a1")
    check("S1 retry job 已注册", job is not None)
    job.func(*job.args, **job.kwargs)
    check("S2 开关关闭时 retry job 触发后不补发", "retry2" not in fired)
    scheduler.shutdown()

    # ── 5. apply_schedule：注册 → 停用移除 → 边界回归 ─────────
    scheduler.list_accounts = ALL_ON
    scheduler.load_config = lambda aid=None: make_cfg(auto=True, harvest_day="mon")
    scheduler.configure(
        run_func=lambda account_id=None: None,
        harvest_func=lambda account_id=None: None,
    )
    sched = scheduler._scheduler
    check("A0 注册阶段: daily+harvest job 就位",
          sched.get_job("daily_send_a1") is not None and sched.get_job("weekly_harvest_a1") is not None)

    scheduler.list_accounts = A1_OFF
    scheduler.apply_schedule("a1")
    check("A1 停用账号: daily job 已移除", sched.get_job("daily_send_a1") is None)
    check("A2 停用账号: harvest job 已移除", sched.get_job("weekly_harvest_a1") is None)

    try:
        scheduler.apply_schedule("a1")
        check("A3 停用账号: 重复停用不抛异常", True)
    except Exception as e:
        check(f"A3 停用账号: 重复停用不抛异常 (异常: {e})", False)

    # harvest=off 且 job 从未注册时安全移除（原代码会抛 JobLookupError）
    scheduler.list_accounts = ALL_ON
    scheduler.load_config = lambda aid=None: make_cfg(auto=True, harvest_day="off")
    try:
        scheduler.apply_schedule("a1")
        check("A4 harvest=off 未注册 job: 安全移除不抛异常", sched.get_job("daily_send_a1") is not None)
    except Exception as e:
        check(f"A4 harvest=off 未注册 job: 安全移除不抛异常 (异常: {e})", False)

    # 停用账号时 pending retry 一并清除
    scheduler.schedule_retry(lambda: None, delay_minutes=30, account_id="a1")
    check("A5 停用前 retry job 存在", sched.get_job("retry_a1") is not None)
    scheduler.list_accounts = A1_OFF
    scheduler.apply_schedule("a1")
    check("A6 停用后 retry job 一并移除", sched.get_job("retry_a1") is None)
    scheduler.shutdown()

    # ── 6. 默认账号启停（bug2）────────────────────────────────
    acc = accounts.update_account("default", enabled=False)
    check("M1 default 可停用: update_account 返回非 None", acc is not None)
    check("M2 default 停用状态持久化", accounts.get_account("default")["enabled"] is False)
    listed = [a for a in accounts.list_accounts() if a["id"] == "default"]
    check("M3 list_accounts 反映 default 已停用", listed and listed[0]["enabled"] is False)

    acc = accounts.update_account("default", name="我的主号")
    check("M4 default 改名仍有效", acc and acc["name"] == "我的主号")
    check("M5 改名后停用状态保持", acc and acc["enabled"] is False)

    acc = accounts.update_account("default", enabled=True)
    check("M6 default 可重新启用", acc and acc["enabled"] is True)

    check("M7 default 仍不可删除", accounts.remove_account("default") is False)
    check("M8 不存在的账号返回 None", accounts.update_account("ghost", enabled=False) is None)

    # ── 7. _gates_open 直接验证 ───────────────────────────────
    scheduler.load_config = lambda aid=None: make_cfg(auto=True)
    scheduler.list_accounts = ALL_ON
    ok, reason = scheduler._gates_open("a1")
    check("G1 正常账号放行", ok is True and reason == "")
    ok, reason = scheduler._gates_open("a1")
    scheduler.list_accounts = DEFAULT_OFF
    ok, reason = scheduler._gates_open("default")
    check("G2 default 停用被拦截", ok is False and reason == "账号已停用")
    scheduler.list_accounts = ALL_ON
    scheduler.load_config = lambda aid=None: make_cfg(auto=False)
    ok, reason = scheduler._gates_open("a1")
    check("G3 开关关闭被拦截", ok is False and reason == "auto_run_enabled=false")

finally:
    scheduler.load_config = orig_load_config
    scheduler.list_accounts = orig_list_accounts
    scheduler.time.sleep = orig_sleep
    scheduler._on_outcome = None
    if scheduler._scheduler is not None:
        scheduler.shutdown()

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
