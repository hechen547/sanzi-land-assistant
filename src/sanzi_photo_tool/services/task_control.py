from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TaskCancelled(RuntimeError):
    """用户主动取消后台任务。"""


class TaskControl:
    """线程安全的任务进度与取消控制器。"""

    def __init__(
        self,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> None:
        self._cancel_event = threading.Event()
        self._progress_callback = progress_callback

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def cancel(self) -> None:
        self._cancel_event.set()

    def set_progress_callback(
        self,
        callback: Callable[[int, str], None] | None,
    ) -> None:
        self._progress_callback = callback

    def checkpoint(self) -> None:
        if self.cancelled:
            raise TaskCancelled("操作已由用户取消")

    def wait(self, seconds: float) -> None:
        """可取消的短暂等待，用于请求重试和上传间隔。"""
        if seconds <= 0:
            self.checkpoint()
            return
        if self._cancel_event.wait(seconds):
            raise TaskCancelled("操作已由用户取消")

    def report(self, percent: float, message: str) -> None:
        self.checkpoint()
        if self._progress_callback:
            self._progress_callback(
                max(0, min(100, int(round(percent)))),
                message,
            )
        # 纯 Python 坐标计算会长时间持有 GIL；短暂让出执行权可保持界面响应。
        time.sleep(0)

    def report_range(
        self,
        start: float,
        end: float,
        current: int,
        total: int,
        message: str,
    ) -> None:
        ratio = current / max(1, total)
        self.report(start + (end - start) * ratio, message)
