"""The diagnostics bundle keeps a long log window; httpx request lines stay out of the log (#105).

A 0.7.1 bundle kept the last 2000 lines; per-request httpx lines were 805 of them and pushed a
user's import out. Now the whole log is kept up to 20,000 lines or 5 MB (the newest if longer),
plus every warning and error from anything older, in time order.
"""

from __future__ import annotations

import logging

import pytest
import structlog

from kestrel import logging_setup, paths
from kestrel.services import diagnostics

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_EMPLOYER = "Northwind Freight"


def _line(i: int, level: str = "INFO", text: str | None = None) -> str:
    return f"2026-11-01 09:{i // 60 % 60:02d}:{i % 60:02d},000 {level} MainThread " + (
        text or f'{{"event": "line {i}"}}'
    )


def _write(lines: list[str], name: str = "kestrel.log") -> None:
    paths.ensure_dirs()
    (paths.logs_dir() / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tails(scrub=None) -> dict[str, str]:
    return dict(diagnostics.log_tails(scrub or diagnostics.make_scrubber(None)))


def test_a_5000_line_log_is_kept_whole_including_early_warnings():
    lines = [_line(i) for i in range(5000)]
    lines[3] = _line(3, "WARNING", '{"event": "import_failed_early"}')
    _write(lines)
    text = _tails()["logs/kestrel.log"]
    assert text.startswith("# kestrel.log: all 5000 lines\n")
    assert text.splitlines()[1:] == lines


def test_a_30000_line_log_keeps_the_newest_20000_and_the_earlier_warnings_in_order():
    lines = [_line(i) for i in range(30_000)]
    early = {5: "WARNING", 100: "ERROR", 9_999: "WARNING"}
    for i, level in early.items():
        lines[i] = _line(i, level, f'{{"event": "problem {i}"}}')
    # A traceback under an early error is part of that error.
    lines.insert(101, "Traceback (most recent call last):")
    lines.insert(102, '  File "x.py", line 1')
    _write(lines)
    out = _tails()["logs/kestrel.log"].splitlines()
    assert out[0] == "# kestrel.log: newest 20000 of 30002 lines plus 5 earlier warnings and errors"
    body = out[1:]
    assert body[:5] == [
        lines[5], lines[100], "Traceback (most recent call last):", '  File "x.py", line 1',
        lines[9_999 + 2],
    ]
    assert body[5:] == lines[-20_000:]
    assert len(body) == 20_005
    assert len(set(body)) == len(body)  # no line twice


def test_an_oversize_log_is_cut_to_five_megabytes_keeping_the_newest():
    pad = "x" * 900
    lines = [_line(i, text=f"{i} {pad}") for i in range(8000)]  # about 7 MB, under 20,000 lines
    _write(lines)
    out = _tails()["logs/kestrel.log"].splitlines()
    kept = out[1:]
    assert kept[-1] == lines[-1]
    assert sum(len(x.encode()) + 1 for x in kept) <= diagnostics.LOG_WINDOW_BYTES
    assert "newest" in out[0] and "of 8000 lines" in out[0]


def test_rotated_files_are_folded_in_before_the_current_one():
    _write([_line(1, "ERROR", '{"event": "old_rotation_error"}'), _line(2)], "kestrel.log.2")
    _write([_line(3)], "kestrel.log.1")
    _write([_line(4)])
    tails = _tails()
    assert list(tails) == ["logs/kestrel.log"]
    assert tails["logs/kestrel.log"].splitlines() == [
        "# kestrel.log: all 4 lines", _line(1, "ERROR", '{"event": "old_rotation_error"}'),
        _line(2), _line(3), _line(4),
    ]


def test_redaction_still_masks_an_early_warning_line_outside_the_window():
    token = "sk-or-v1-0123456789abcdefghijklmnopqrstuvwxyz"
    lines = [_line(i) for i in range(25_000)]
    lines[2] = _line(2, "WARNING", f'{{"event": "auth", "org": "{_EMPLOYER}", "key": "{token}"}}')
    lines[3] = _line(3, "ERROR", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.leaked-value")
    _write(lines)
    scrub = diagnostics.make_scrubber({_EMPLOYER: "[Prior employer]"})
    text = _tails(scrub)["logs/kestrel.log"]
    assert "plus 2 earlier warnings and errors" in text.splitlines()[0]
    assert "[Prior employer]" in text and _EMPLOYER not in text
    assert token not in text and "leaked-value" not in text


@pytest.fixture
def _clean_logging():
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    watched = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    logging_setup._configured = False
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)
    for n, lv in watched.items():
        logging.getLogger(n).setLevel(lv)
    logging_setup._configured = False
    structlog.reset_defaults()


@pytest.mark.usefixtures("_clean_logging")
def test_httpx_request_lines_stay_out_of_the_log_but_its_warnings_do_not():
    logging_setup.configure_logging("INFO")
    logging.getLogger("httpx").info('HTTP Request: POST http://x/v1/chat/completions "200 OK"')
    logging.getLogger("httpcore.http11").debug("send_request_headers")
    logging.getLogger("httpx").warning("httpx_real_warning")
    logging.getLogger("kestrel.test.probe").info("app_info_line")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (paths.logs_dir() / "kestrel.log").read_text(encoding="utf-8")
    assert "HTTP Request" not in text and "send_request_headers" not in text
    assert "httpx_real_warning" in text and "app_info_line" in text


def test_earlier_warnings_are_capped_at_two_megabytes_keeping_the_newest():
    pad = "y" * 900
    lines = [_line(i, "WARNING", f"w{i} {pad}") for i in range(4000)]  # about 3.7 MB of warnings
    lines += [_line(i) for i in range(4000, 4000 + diagnostics.LOG_WINDOW_LINES)]
    _write(lines)
    out = _tails()["logs/kestrel.log"].splitlines()
    assert "(oldest dropped above 2 MB)" in out[0]
    body = out[1 : -diagnostics.LOG_WINDOW_LINES]
    assert sum(len(x.encode()) + 1 for x in body) <= diagnostics.LOG_EARLIER_BYTES
    assert body[-1] == lines[3999] and "w0 " not in body[0]
    assert body[0].split(" ")[2] == "WARNING"  # starts on an entry, not mid-traceback
    assert out[-1] == lines[-1]


def test_a_small_set_of_earlier_warnings_carries_no_cap_note():
    lines = [_line(i) for i in range(21_000)]
    lines[1] = _line(1, "ERROR", "boom")
    _write(lines)
    assert "dropped" not in _tails()["logs/kestrel.log"].splitlines()[0]


def test_an_unreadable_rotated_file_is_always_reported(monkeypatch):
    _write([_line(1)], "kestrel.log.1")
    _write([_line(i) for i in range(25_000)])
    real = type(paths.logs_dir()).read_text

    def _read(self, *a, **k):
        if self.name == "kestrel.log.1":
            raise PermissionError("locked by another program")
        return real(self, *a, **k)

    monkeypatch.setattr(type(paths.logs_dir()), "read_text", _read)
    out = _tails()["logs/kestrel.log"].splitlines()
    assert any("kestrel.log.1 unreadable" in x and "locked" in x for x in out[:3])
