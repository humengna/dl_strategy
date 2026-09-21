# coding: utf-8
"""
终端进度条。

终端（tty）下用 \\r 原地刷新，重定向到文件时按百分比档位换行打印，
这样日志文件里不会出现成千上万行刷屏。
"""

import sys
import time
from typing import Optional, TextIO


def format_duration(seconds: float) -> str:
    """把秒数格式化成 mm:ss 或 h:mm:ss"""
    if seconds is None or seconds != seconds or seconds in (float('inf'), float('-inf')) or seconds < 0:
        return '--:--'
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes:02d}:{secs:02d}'


class Progress(object):
    """
    进度条。

        bar = Progress(total=5224, prefix='下载日线')
        for i, item in enumerate(items, 1):
            ...
            bar.update(i, suffix=item)
        bar.close()
    """

    BAR_WIDTH = 24

    def __init__(self, total: int, prefix: str = '', enabled: bool = True,
                 stream: Optional[TextIO] = None, min_interval: float = 0.2,
                 step_percent: int = 10):
        self.total = max(int(total or 0), 0)
        self.prefix = prefix
        self.stream = stream if stream is not None else sys.stdout
        self.enabled = bool(enabled) and self.total > 0
        self.min_interval = min_interval          # tty 下两次刷新的最小间隔
        self.step_percent = max(int(step_percent), 1)   # 非 tty 下每多少百分点打一行

        self.start_time = time.time()
        self.tty = bool(getattr(self.stream, 'isatty', lambda: False)())
        self._done = 0
        self._last_render = 0.0
        self._last_bucket = -1
        self._max_len = 0
        self._rendered_done = -1
        self._closed = False

    # ---------- 对外 ----------

    def update(self, done: int, suffix: str = '', force: bool = False) -> None:
        self._done = max(int(done), 0)
        if not self.enabled or self._closed:
            return
        if not force and not self._should_render():
            return
        self._write(self._render(suffix))

    def advance(self, step: int = 1, suffix: str = '') -> None:
        self.update(self._done + step, suffix)

    def close(self, suffix: str = '') -> None:
        if self._closed:
            return
        # 非 tty 下最后一行已经打过就不再重复
        if self.enabled and (self.tty or self._rendered_done != self._done):
            self._write(self._render(suffix), final=True)
        self._closed = True

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    # ---------- 内部 ----------

    def _should_render(self) -> bool:
        now = time.time()
        if self._done >= self.total:
            return True
        if self.tty:
            if now - self._last_render < self.min_interval:
                return False
            self._last_render = now
            return True

        bucket = int(self._percent() // self.step_percent)
        if bucket <= self._last_bucket:
            return False
        self._last_bucket = bucket
        self._last_render = now
        return True

    def _percent(self) -> float:
        if not self.total:
            return 100.0
        return min(100.0 * self._done / self.total, 100.0)

    def _eta(self) -> float:
        if self._done <= 0 or self._done >= self.total:
            return 0.0
        return self.elapsed / self._done * (self.total - self._done)

    def _render(self, suffix: str) -> str:
        pct = self._percent()
        filled = int(self.BAR_WIDTH * pct / 100)
        bar = '#' * filled + '-' * (self.BAR_WIDTH - filled)

        parts = []
        if self.prefix:
            parts.append(self.prefix)
        parts.append(f'[{bar}]')
        parts.append(f'{pct:5.1f}%')
        parts.append(f'{self._done}/{self.total}')
        parts.append(f'已用 {format_duration(self.elapsed)}')
        if self._done < self.total:
            parts.append(f'剩余 {format_duration(self._eta())}')
        if suffix:
            parts.append(str(suffix))
        return ' '.join(parts)

    def _write(self, line: str, final: bool = False) -> None:
        self._rendered_done = self._done
        self._max_len = max(self._max_len, len(line))
        if self.tty:
            self.stream.write('\r' + line.ljust(self._max_len))
            if final:
                self.stream.write('\n')
        else:
            self.stream.write(line + '\n')
        try:
            self.stream.flush()
        except Exception:
            pass
