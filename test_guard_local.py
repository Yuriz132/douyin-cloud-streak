# -*- coding: utf-8 -*-
"""离线单测：compute_reply_decision 守卫逻辑（无需浏览器）。
核心诉求：好友没有发新消息、也没分享视频时 -> 不回复、不调 AI。
重点验证“我发的消息被角色误判成对方(user)”这种最易踩坑的情况也被拦住。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_reply as A


def M(role, raw, is_share=False):
    return {"role": role, "type": "text", "content": raw, "raw": raw,
            "is_share_caption": is_share}


def check(name, cond, dec=None):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  -> {dec}" if dec is not None else ""))
    if not cond:
        check.failed += 1
check.failed = 0


# 1) 正常：我最后发的，对方没回 -> 跳过（角色分类正确）
msgs = [M("user", "在吗吗吗"), M("assistant", "我马上就来")]
dec = A.compute_reply_decision(msgs, "我马上就来")
check("1 正常-我最后发/对方没回 -> 跳过", dec["skip"] and dec["reason"] == "last_is_mine(no_reply_from_friend)", dec)

# 2) 角色误判：我最后发的被当成 user，但内容与本地最后发出精确吻合 -> 仍跳过
msgs = [M("user", "在吗吗吗"), M("user", "我马上就来")]
dec = A.compute_reply_decision(msgs, "我马上就来")
check("2 误判-我消息被当user/内容吻合 -> 跳过", dec["skip"], dec)

# 3) 尾缀 emoji 也吻合：历史存了带 emoji，DOM 也带 -> 精确相等命中
msgs = [M("user", "看这个"), M("assistant", "好嘞收到哈哈")]
dec = A.compute_reply_decision(msgs, "好嘞收到哈哈")
check("3 尾缀emoji-精确相等 -> 跳过", dec["skip"], dec)

# 4) 对方在我回复后发了新消息 -> 不跳过，incoming 命中
msgs = [M("user", "在吗吗吗"), M("assistant", "我马上就来"), M("user", "晚上吃啥呀")]
dec = A.compute_reply_decision(msgs, "我马上就来")
check("4 对方新消息 -> 不跳过", (not dec["skip"]) and len(dec["incoming"]) == 1
      and dec["incoming"][0]["raw"] == "晚上吃啥呀", dec)

# 5) 对方在我回复后分享了视频 -> 不跳过，incoming 含分享卡
msgs = [M("user", "看这个"), M("assistant", "好嘞收到"), M("user", "柏林.🐿️的vlog", is_share=True)]
dec = A.compute_reply_decision(msgs, "好嘞收到")
check("5 对方分享视频 -> 不跳过且含分享", (not dec["skip"])
      and any(m.get("is_share_caption") for m in dec["incoming"]), dec)

# 6) 从未回复过（lr 空），对方首条消息 -> 不跳过
msgs = [M("user", "你好呀在吗")]
dec = A.compute_reply_decision(msgs, "")
check("6 首条/未回复 -> 不跳过", not dec["skip"] and len(dec["incoming"]) == 1, dec)

# 7) 误判 + 内容不一致（DOM 与历史不完全相等）：兜底仍靠角色=assistant 失败，
#    但 last_is_mine 精确相等兜底 -> 我最后发的字字相同也拦截（最贴近用户原话的场景）
msgs = [M("user", "对方旧消息"), M("user", "我刚发的这句很长的话")]
dec = A.compute_reply_decision(msgs, "我刚发的这句很长的话")
check("7 误判+长句精确相等 -> 跳过", dec["skip"], dec)

# 8) 好友发的消息恰好与我上条回复完全相同 -> 安全跳过（优先"不误回"）。
#    说明：用户核心诉求是"好友没发新消息就别回"，对于末尾消息精确等于我最后发出的内容，
#    宁可跳过（顶多少回一条回声），也绝不给自己的消息误回复。这是符合优先级的安全行为。
msgs = [M("user", "在吗吗吗"), M("assistant", "收到收到"), M("user", "收到收到")]
dec = A.compute_reply_decision(msgs, "收到收到")
check("8 好友回声=我上条 -> 安全跳过(优先不误回)", dec["skip"]
      and dec["reason"] == "last_is_mine(no_reply_from_friend)", dec)

# 9) 空消息列表 -> 跳过
dec = A.compute_reply_decision([], "任意")
check("9 空列表 -> 跳过", dec["skip"] and dec["reason"] == "empty_chat", dec)

# 10) 对方发了新消息，但也夹着我刚发的回声（误判为user）-> 只回真实新消息
msgs = [M("user", "老地方见？"), M("assistant", "好嘞就这"), M("user", "好嘞就这"), M("user", "记得带伞")]
dec = A.compute_reply_decision(msgs, "好嘞就这")
check("10 混合：剔除我回声/保留真实新消息", (not dec["skip"])
      and [m["raw"] for m in dec["incoming"]] == ["记得带伞"], dec)


print(f"\n{'='*40}\n失败用例数: {check.failed}")
sys.exit(1 if check.failed else 0)
