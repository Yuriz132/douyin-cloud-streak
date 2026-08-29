"""全局单工作线程执行器。

将耗时/易受平台限制的浏览器任务统一放到一个后台工作线程顺序执行，实现
「单线程顺序执行所有账号」：

- 浏览器实例可安全复用（sync Playwright 实例绑定创建它的线程，单线程内
  复用不存在跨线程 greenlet 崩溃问题）；
- 任务之间天然串行，避免多账号并发互踩与浏览器互相干扰；
- 对外提供 submit（异步）与 submit_and_wait（同步等待结果，供 CLI 使用）。

登录扫码（login_session）为长驻独立会话，不进入本执行器，避免阻塞其他任务。
"""

from __future__ import annotations

import logging
import queue
import threading
import time

logger = logging.getLogger("douyin-cloud-streak")


class _Executor:
    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="browser-worker")
        self._thread.start()

    def _run(self) -> None:
        while True:
            fn = self._q.get()
            if fn is None:  # 哨兵，停止
                break
            try:
                fn()
            except Exception:
                logger.exception("浏览器任务执行异常")

    def submit(self, fn) -> None:
        """异步排队执行（fire-and-forget），不等待结果。"""
        self._q.put(fn)

    def submit_and_wait(self, fn, timeout: float | None = None):
        """排队执行并阻塞等待结果。必须在非工作线程调用，否则会死锁。"""
        holder: dict = {}

        def _wrap() -> None:
            try:
                holder["result"] = fn()
            except Exception as e:  # noqa: BLE001
                holder["error"] = e

        self._q.put(_wrap)
        end = None if timeout is None else time.time() + timeout
        while "result" not in holder and "error" not in holder:
            if end is not None and time.time() > end:
                raise TimeoutError("任务执行超时")
            time.sleep(0.05)
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    def shutdown(self) -> None:
        self._q.put(None)


executor = _Executor()
