"""好友台账：以会话显示名为键的本地持久化好友库（每账号独立 ledger.json）。

v1（P0）：仅 consumer 数据（显示名 + 火花天数 + 会话存在性 + 勾选状态 + 最后发送时间）。
P1 起扩展 short_id / nickname / user_id / 置信度，业务主键升级为 short_id（不可变），
display_name 变为可变字段。匹配铁律：display_name 相同即视为同一人（P0 阶段约束）。
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from .config import DEFAULT_ACCOUNT_ID, account_dir
from .avatar import fetch_and_save_avatar


def ledger_path(account_id: str | None = None) -> Path:
    return account_dir(account_id) / "ledger.json"


_lock = threading.RLock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_entry(display_name: str) -> dict:
    """P1 完整 schema：display_name 为 P0 主键，short_id 为业务主键（不可变）。"""
    return {
        "display_name": display_name,
        "nickname": "",
        "short_id": "",
        "user_id": "",
        "avatar": "",             # 本地缓存头像路径 /avatars/<aid>/<hash>.<ext>
        "custom_message": "",     # 好友专属自定义发送文案（为空时跟随全局文案库）
        "streak_days": 0,
        "has_conversation": False,
        "selected": False,
        "selected_order": None,  # 勾选顺序（越小越靠前，仅勾选时有效；持久化保证重启后置顶顺序不丢）
        "last_sent_at": None,
        "channel": "none",        # consumer | creator | none
        "join_confidence": "low",  # high: display_name 与 nickname 已对上；low: 未确认
        "source": {"creator": False, "consumer": False},
    }


def _parse_streak(text) -> int:
    """从火花文本（如 '🔥 81' / '81' / '81天'）提取天数，解析失败返回 0。"""
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m else 0


def _norm_ws(s) -> str:
    """空白归一化（仅用于 join 匹配，不改存储原样值）：
    consumer 页显示名可能含不间断空格（U+00A0），creator 接口返回普通空格。"""
    return str(s or "").replace("\u00a0", " ").strip()


def load_ledger(account_id: str | None = None) -> list[dict]:
    with _lock:
        entries: list[dict] = []
        lp = ledger_path(account_id)
        if lp.exists():
            try:
                data = json.loads(lp.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    entries = [dict(e) for e in data if isinstance(e, dict) and e.get("display_name")]
                    for e in entries:
                        # 派生标记（不持久化，读时计算保证一致）：
                        # no_consumer_conversation：consumer 私信无会话（自动识别，不限来源）
                        #   → 勾选也不会被通道 A 发送（默认 skipped 或降级），除非开启通道 B
                        # creator_only：creator 页有记录 且 consumer 无会话（creator 来源子集）
                        e["no_consumer_conversation"] = not e.get("has_conversation")
                        e["creator_only"] = e.get("channel") == "creator" and e["no_consumer_conversation"]
            except Exception:
                pass
        return entries


def _save(entries: list[dict], account_id: str | None = None) -> None:
    with _lock:
        d = account_dir(account_id)
        d.mkdir(parents=True, exist_ok=True)
        lp = ledger_path(account_id)
        temp_lp = lp.with_suffix('.tmp')
        temp_lp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_lp.replace(lp)


def _upsert(entries: list[dict], entry: dict) -> dict:
    """按 display_name upsert：更新新字段，但保留已有条目的 selected / last_sent_at。

    新增条目补齐默认字段，保证 schema 一致。返回命中/新增的条目。
    """
    for e in entries:
        if e.get("display_name") == entry["display_name"]:
            selected = e.get("selected", False)
            last_sent = e.get("last_sent_at")
            e.update(entry)
            for k, v in _default_entry(entry["display_name"]).items():
                e.setdefault(k, v)
            e["selected"] = selected
            e["last_sent_at"] = last_sent
            return e
    base = _default_entry(entry["display_name"])
    base.update(entry)
    entries.append(base)
    return base


def merge_consumer_contacts(contacts: list[dict], account_id: str | None = None) -> dict:
    """把 consumer 会话列表（fetch_chat_contacts 的 names 字段）upsert 进台账。

    只更新火花天数与会话存在性，不覆盖用户勾选与历史发送时间。
    返回 {"added", "updated", "total"} 统计。
    """
    # 步骤一：无锁读取快照用于头像比对
    with _lock:
        entries_snapshot = load_ledger(account_id)
    by_name_snapshot = {e.get("display_name"): e for e in entries_snapshot}
    contacts = contacts or []

    def _resolve_avatar(c: dict) -> str:
        """并发下载头像；无火花好友直接使用原链接不下载，大幅减轻并发封控压力。"""
        avatar_url = c.get("avatar") or ""
        if not avatar_url:
            return ""
            
        streak = _parse_streak(c.get("streak"))
        if streak == 0:
            old = by_name_snapshot.get(str(c.get("name", "")).strip())
            return (old.get("avatar") or avatar_url) if old else avatar_url

        new_avatar = fetch_and_save_avatar(avatar_url, account_id)
        if new_avatar:
            return new_avatar
        old = by_name_snapshot.get(str(c.get("name", "")).strip())
        return (old.get("avatar") or avatar_url) if old else avatar_url

    # 步骤二：耗时网络 IO（完全不阻塞 _lock，前端 api 可畅通无阻）
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        avatar_paths = list(pool.map(_resolve_avatar, contacts))

    # 步骤三：拿锁，重新加载最新台账进行原子级 upsert
    with _lock:
        entries = load_ledger(account_id)
        by_name = {e.get("display_name"): e for e in entries}
        added = 0
        updated = 0
        
        for c, avatar_path in zip(contacts, avatar_paths):
            name = str(c.get("name", "")).strip()
            if not name:
                continue
            exists = name in by_name
            old_entry = by_name.get(name) or {}
            old_avatar = old_entry.get("avatar") or ""
            new_streak = _parse_streak(c.get("streak"))
            # 防数据污染：接口兜底/DOM 提取失败时 streak 为 0，绝不能用 0 回写
            # 覆盖台账已有的精确火花；仅当新值在合理范围内才更新。
            # 上限用 4 位数（9999）：抖音续火花可超过 999 天，但 5 位及以上
            # 几乎必然是从消息文本误抠出的异常（金额/时间戳等），应予忽略。
            if 0 < new_streak <= 9999:
                eff_streak = new_streak
            else:
                eff_streak = int(old_entry.get("streak_days") or 0)
            e = _upsert(entries, {
                "display_name": name,
                "streak_days": eff_streak,
                "has_conversation": True,
                "channel": "consumer",
                "avatar": avatar_path or old_avatar,
            })
            e.setdefault("source", {})["consumer"] = True
            if e.get("source", {}).get("creator") and e.get("nickname") == name:
                e["join_confidence"] = "high"

            if not exists:
                added += 1
            else:
                updated += 1

        _save(entries, account_id)
        return {"added": added, "updated": updated, "total": len(entries)}


def import_config_friends(friends: list[str], account_id: str | None = None) -> dict:
    """兼容迁移：把 config.json 的 friends 同步进台账并默认勾选。

    - 已存在条目：置 selected=True（保持原发送目标集合不变）
    - 不存在条目：新增并 selected=True
    返回 {"added", "selected"}。
    """
    with _lock:
        entries = load_ledger(account_id)
        by_name = {e.get("display_name"): e for e in entries}
        added = 0
        selected = 0
        for name in friends or []:
            name = str(name).strip()
            if not name:
                continue
            if name in by_name:
                if not by_name[name].get("selected"):
                    by_name[name]["selected"] = True
                    selected += 1
            else:
                entries.append({
                    **_default_entry(name),
                    "has_conversation": False,
                    "selected": True,
                })
                by_name[name] = entries[-1]
                added += 1
        if added or selected:
            _save(entries, account_id)
        return {"added": added, "selected": selected}


def get_selected(account_id: str | None = None) -> list[dict]:
    return [e for e in load_ledger(account_id) if e.get("selected")]


def set_streak(name: str, days: int, account_id: str | None = None) -> bool:
    """发送校准：把从会话页读到的真实火花天数回填台账。

    仅当新值在 1~9999 合理区间才写入（0 表示提取失败，绝不覆盖既有精确值），
    与 import_contacts 的防污染规则保持一致。返回是否写入。
    """
    if not isinstance(days, int) or isinstance(days, bool) or not (0 < days <= 9999):
        return False
    with _lock:
        entries = load_ledger(account_id)
        for e in entries:
            if e.get("display_name") == name:
                e["streak_days"] = days
                _save(entries, account_id)
                return True
    return False


def merge_creator_map(mapping: dict, account_id: str | None = None) -> dict:
    """把 creator 采集的 {short_id: {nickname, user_id}} 合并进台账（预 join，乐观）。

    - display_name == nickname 或 display_name == short_id → 回填并 high（两侧确认同一人）
    - 否则新建条目：display_name=nickname（假定）、confidence=low、channel=creator
    返回 {"joined", "added", "updated", "total"}。
    """
    with _lock:
        entries = load_ledger(account_id)
        by_display = {e.get("display_name"): e for e in entries}
        # 归一化匹配：consumer 显示名可能含 NBSP（U+00A0），creator 昵称是普通空格。
        # 用「归一化名 → 条目列表」保留同名全部候选，避免 stub 覆盖真实条目。
        by_display_norm: dict[str, list[dict]] = {}
        for e in entries:
            by_display_norm.setdefault(_norm_ws(e.get("display_name")), []).append(e)

        def _pick_target(exact, by_sid, norm_list):
            """从候选条目里择优：优先有 consumer 数据（会话/勾选/历史发送）的真实条目，避免命中孤立 stub。"""
            seen: list[dict] = []
            for c in ([exact, by_sid] + (norm_list or [])):
                if c is not None and c not in seen:
                    seen.append(c)
            if not seen:
                return None
            for c in seen:
                if (
                    c.get("has_conversation")
                    or c.get("source", {}).get("consumer")
                    or c.get("last_sent_at")
                ):
                    return c
            return seen[0]

        joined = added = updated = 0
        for sid, info in (mapping or {}).items():
            sid = str(sid).strip()
            if not sid:
                continue
            nick = str(info.get("nickname", "")).strip()
            uid = str(info.get("user_id", ""))
            target = _pick_target(
                by_display.get(nick),
                by_display.get(sid),
                by_display_norm.get(nick),
            )
            if target is not None:
                changed = False
                if target.get("short_id") != sid:
                    target["short_id"] = sid
                    changed = True
                if target.get("nickname") != nick:
                    target["nickname"] = nick
                    changed = True
                if target.get("user_id") != uid:
                    target["user_id"] = uid
                    changed = True
                src = target.setdefault("source", {})
                # 旧 schema 数据恢复：有会话即视为 consumer 侧出现过
                if target.get("has_conversation") and not src.get("consumer"):
                    src["consumer"] = True
                if not src.get("creator"):
                    src["creator"] = True
                    changed = True
                # high 需两侧确认：display_name 已在 consumer 页出现 且 == nickname（或 == short_id）
                if target.get("join_confidence") != "high" and src.get("consumer"):
                    target["join_confidence"] = "high"
                    changed = True
                if target.get("has_conversation") and target.get("channel") != "consumer":
                    target["channel"] = "consumer"
                    changed = True
                if changed:
                    updated += 1
                joined += 1
                # 清理同 short_id 的孤立 stub（creator-only 无会话/未发送的条目，被真实 join 后删除）
                target_idx = entries.index(target)
                for i in range(len(entries) - 1, -1, -1):
                    e = entries[i]
                    if (
                        i != target_idx
                        and e.get("short_id") == sid
                        and not e.get("has_conversation")
                        and not e.get("last_sent_at")
                    ):
                        del entries[i]
            else:
                entries.append({
                    **_default_entry(nick),
                    "nickname": nick,
                    "short_id": sid,
                    "user_id": uid,
                    "source": {"creator": True, "consumer": False},
                    "join_confidence": "low",
                    "channel": "creator",
                })
                by_display[nick] = entries[-1]
                added += 1
        if added or updated:
            _save(entries, account_id)
        return {"joined": joined, "added": added, "updated": updated, "total": len(entries)}


def confirm_join(display_name: str, account_id: str | None = None) -> None:
    """发送时确认（P1 关键兜底）：该 display_name 的会话已在 consumer 页成功定位+标题校验通过。

    标记 source.consumer=true、channel=consumer；若 nickname == display_name（两侧名字一致）
    → join_confidence 升级为 high。
    """
    with _lock:
        entries = load_ledger(account_id)
        for e in entries:
            if e.get("display_name") == display_name:
                e.setdefault("source", {})["consumer"] = True
                e["channel"] = "consumer"
                if e.get("nickname") and e["nickname"] == display_name:
                    e["join_confidence"] = "high"
                break
        _save(entries, account_id)


def set_custom_message(display_name: str, message: str, account_id: str | None = None) -> bool:
    """设置指定好友的专属发送文案（传空字符串则表示清除专属文案，跟随全局文案库）。"""
    with _lock:
        entries = load_ledger(account_id)
        found = False
        for e in entries:
            if e.get("display_name") == display_name:
                e["custom_message"] = str(message or "").strip()
                found = True
                break
        if found:
            _save(entries, account_id)
        return found


def set_selected(entries_in: list[dict], account_id: str | None = None) -> dict:
    """批量更新勾选与勾选顺序：entries: [{display_name, selected, selected_order, custom_message}]。

    已存在条目更新勾选；不存在的条目新增并置勾选（支持手动添加好友）。
    selected_order 随勾选持久化（勾选组内排序，重启后置顶顺序不丢）；
    取消勾选时清空 selected_order。
    返回 {"updated", "added"}。
    """
    with _lock:
        entries = load_ledger(account_id)
        by_name = {e.get("display_name"): e for e in entries}
        updated = 0
        added = 0
        for it in entries_in:
            name = str(it.get("display_name", "")).strip()
            sel = bool(it.get("selected"))
            order = it.get("selected_order")
            if not name:
                continue
            if name in by_name:
                e = by_name[name]
                if bool(e.get("selected")) != sel:
                    e["selected"] = sel
                    updated += 1
                new_order = order if sel else None
                if e.get("selected_order") != new_order:
                    e["selected_order"] = new_order
                    updated += 1
                if "custom_message" in it:
                    c_msg = str(it.get("custom_message") or "").strip()
                    if e.get("custom_message") != c_msg:
                        e["custom_message"] = c_msg
                        updated += 1
            else:
                entries.append({
                    **_default_entry(name),
                    "has_conversation": False,
                    "selected": sel,
                    "selected_order": order if sel else None,
                    "custom_message": str(it.get("custom_message") or "").strip(),
                })
                by_name[name] = entries[-1]
                added += 1
        if updated or added:
            _save(entries, account_id)
        return {"updated": updated, "added": added}


def update_send_result(
    display_name: str,
    ok: bool,
    at: str | None = None,
    via_creator: bool = False,
    account_id: str | None = None,
    msg_text: str | None = None,
) -> None:
    """发送后回写台账：更新 last_sent_at；成功时标记会话存在。

    via_creator=True（通道 B 首条消息）：只记时间、不置 has_conversation——
    creator 侧发过首条消息 ≠ consumer 私信页有会话；否则 run_send 会误判
    走通道 A 去 consumer 页定位，而列表里根本没有该会话导致失败。
    msg_text：记录本次实际发送内容到 last_msg，供 build_message 去重，
    避免文案池较小时连续多天发完全相同的内容（提高风控暴露）。
    """
    entries = load_ledger(account_id)
    for e in entries:
        if e.get("display_name") == display_name:
            e["last_sent_at"] = at or _now()
            if ok and not via_creator:
                e["has_conversation"] = True
            if ok and msg_text:
                e["last_msg"] = msg_text
            break
    _save(entries, account_id)


def mark_no_consumer_conversation(display_name: str, account_id: str | None = None) -> None:
    """自愈：通道 A 在 consumer 页定位失败时调用（仅对 creator-only 条目）。

    把误标的 has_conversation 修正回 false，避免每轮都误走通道 A 失败；
    channel 保持 creator，之后走通道 B 或 skipped。
    """
    with _lock:
        entries = load_ledger(account_id)
        changed = False
        for e in entries:
            if e.get("display_name") == display_name and e.get("channel") == "creator":
                if e.get("has_conversation"):
                    e["has_conversation"] = False
                    changed = True
                break
        if changed:
            _save(entries, account_id)


def stats(account_id: str | None = None) -> dict:
    """台账自愈报表：分布统计、连续成功 Top、low 待确认、近 7 天发送。"""
    entries = load_ledger(account_id)
    now = datetime.now().astimezone()
    week_ago = (now - timedelta(days=7)).isoformat()
    high = sum(1 for e in entries if e.get("join_confidence") == "high")
    no_conv = [
        {"display_name": e["display_name"], "channel": e.get("channel")}
        for e in entries
        if not e.get("has_conversation")
    ]
    low_pending = [
        e["display_name"]
        for e in entries
        if e.get("join_confidence") == "low" and e.get("has_conversation")
    ]
    recent_sent = [
        e["display_name"]
        for e in entries
        if str(e.get("last_sent_at") or "") >= week_ago
    ]
    top_streak = sorted(entries, key=lambda e: -(e.get("streak_days") or 0))[:10]
    return {
        "total": len(entries),
        "selected": sum(1 for e in entries if e.get("selected")),
        "confidence": {"high": high, "low": len(entries) - high},
        "with_short_id": sum(1 for e in entries if e.get("short_id")),
        "no_conversation": no_conv,
        "low_pending": low_pending,
        "recent_sent_7d": recent_sent,
        "top_streak": [
            {"display_name": e["display_name"], "streak_days": e.get("streak_days")}
            for e in top_streak
        ],
    }


# 别名兼容
update_from_sync = merge_consumer_contacts
