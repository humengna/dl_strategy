# coding: utf-8
import io

from momentum.progress import Progress, format_duration


class TtyStream(io.StringIO):
    def isatty(self):
        return True


def test_format_duration():
    assert format_duration(0) == '00:00'
    assert format_duration(65) == '01:05'
    assert format_duration(3725) == '1:02:05'
    assert format_duration(-1) == '--:--'
    assert format_duration(float('inf')) == '--:--'


def test_non_tty_prints_by_percent_bucket():
    out = io.StringIO()
    bar = Progress(100, prefix='下载', stream=out, step_percent=25)
    for i in range(1, 101):
        bar.update(i)
    bar.close()

    lines = out.getvalue().strip().split('\n')
    # 0/25/50/75/100 五档，不会打 100 行
    assert len(lines) <= 6
    assert '100.0%' in lines[-1]
    assert lines[-1].startswith('下载')


def test_non_tty_does_not_duplicate_final_line():
    out = io.StringIO()
    bar = Progress(10, stream=out, step_percent=50)
    bar.update(10)
    bar.close()
    assert out.getvalue().count('100.0%') == 1


def test_tty_uses_carriage_return_and_ends_with_newline():
    out = TtyStream()
    bar = Progress(10, stream=out, min_interval=0)
    bar.update(5)
    bar.close()

    text = out.getvalue()
    assert text.startswith('\r')
    assert text.endswith('\n')


def test_tty_throttles_by_interval():
    out = TtyStream()
    bar = Progress(100, stream=out, min_interval=60)   # 60 秒内只渲染一次
    for i in range(1, 100):
        bar.update(i)
    renders = out.getvalue().count('\r')
    assert renders == 1


def test_bar_shows_percent_and_counts():
    out = io.StringIO()
    bar = Progress(200, prefix='[数据] 下载', stream=out, step_percent=50)
    bar.update(100, suffix='600000.SH')

    line = out.getvalue().strip()
    assert '[数据] 下载' in line
    assert ' 50.0%' in line
    assert '100/200' in line
    assert '600000.SH' in line
    assert '剩余' in line


def test_progress_disabled_writes_nothing():
    out = io.StringIO()
    bar = Progress(10, stream=out, enabled=False)
    bar.update(5)
    bar.close()
    assert out.getvalue() == ''


def test_zero_total_is_disabled():
    out = io.StringIO()
    bar = Progress(0, stream=out)
    bar.update(1)
    bar.close()
    assert out.getvalue() == ''


def test_overshoot_clamps_to_100_percent():
    out = io.StringIO()
    bar = Progress(10, stream=out)
    bar.update(15)
    assert '100.0%' in out.getvalue()


def test_advance_steps_forward():
    out = io.StringIO()
    bar = Progress(4, stream=out, step_percent=25)
    bar.advance()
    bar.advance()
    bar.close()
    assert '2/4' in out.getvalue() or '4/4' in out.getvalue()
