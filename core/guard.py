"""共享防护：限流关键词与安全验证浮层检测。"""

from __future__ import annotations

RATE_LIMIT_KEYWORDS = [
    "操作频繁",
    "操作太频繁",
    "发送过于频繁",
    "请稍后再试",
    "稍后再试",
    "请稍后",
    "安全验证",
    "滑动验证",
    "验证码",
    "验证中心",
    "人机验证",
    "网络异常",
    "请勿频繁",
]


def detect_rate_limit(page) -> str | None:
    """扫描页面上可见的限流/验证提示，命中返回关键词，未命中返回 None。"""
    for kw in RATE_LIMIT_KEYWORDS:
        try:
            loc = page.get_by_text(kw, exact=False)
            for i in range(loc.count()):
                box = loc.nth(i).bounding_box()
                if box:  # 可见才计，避免匹配到隐藏节点
                    return kw
        except Exception:
            continue
    return None
