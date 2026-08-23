"""消息文案构建与防风控轮换引擎。

支持：
1. 静态文案池随机抽取；
2. 防连续重复算法（同一好友不连续两天发送相同文案）；
3. 节日与时间变量自动填充（如 [日期]、[时间]）。
"""

from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger("douyin-cloud-streak")


def render_template(template: str) -> str:
    """渲染消息模板，替换变量。"""
    msg = template.strip()
    now = datetime.now()

    if "[日期]" in msg or "[date]" in msg.lower():
        date_str = now.strftime("%m月%d日")
        msg = msg.replace("[日期]", date_str).replace("[date]", date_str)

    if "[时间]" in msg or "[time]" in msg.lower():
        time_str = now.strftime("%H:%M")
        msg = msg.replace("[时间]", time_str).replace("[time]", time_str)

    return msg.strip()


def build_message(templates: List[str], last_sent_msg: str = "") -> str:
    """从模板池中选择一条消息，并尽量避免与上次发送的内容相同。"""
    if not templates:
        templates = ["🔥 续火花", "今天也要开心哦 🔥", "晚上好 🔥"]

    # 预渲染所有候选
    candidates = []
    for t in templates:
        rendered = render_template(t)
        if rendered:
            candidates.append(rendered)

    if not candidates:
        return "🔥 续火花"

    # 过滤掉与上次相同的消息
    diff_candidates = [m for m in candidates if m != last_sent_msg]
    if diff_candidates:
        return random.choice(diff_candidates)

    return random.choice(candidates)
