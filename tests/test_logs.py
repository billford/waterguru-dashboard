"""Log rotation, with the constraint that makes it non-obvious.

launchd opens StandardOutPath/StandardErrorPath itself and holds the descriptor
for the life of the process. Rename or unlink that file and launchd carries on
writing to the unlinked inode: the visible log stays empty while the disk fills,
with nothing to indicate why. On a poller running 144 times a day that would
take a long time to notice.
"""
import os

import pytest

import logs


def _fill(path, lines=20000):
    path.write_text("".join(f"line {i}\n" for i in range(lines)))
    return path


def test_a_file_under_the_limit_is_left_alone(tmp_path):
    path = tmp_path / "small.log"
    path.write_text("one line\n")
    assert logs.trim_in_place(path, max_bytes=10_000) is False
    assert path.read_text() == "one line\n"


def test_an_oversized_file_is_shortened(tmp_path):
    path = _fill(tmp_path / "big.log")
    before = path.stat().st_size
    assert logs.trim_in_place(path, max_bytes=8_000) is True
    assert path.stat().st_size < before


def test_the_inode_survives_so_an_open_descriptor_stays_valid(tmp_path):
    """The whole reason this truncates instead of renaming."""
    path = _fill(tmp_path / "held.log")
    inode = path.stat().st_ino
    logs.trim_in_place(path, max_bytes=8_000)
    assert path.stat().st_ino == inode


def test_a_holder_appending_afterwards_still_writes_to_the_visible_file(tmp_path):
    """Simulates launchd: an O_APPEND fd held across the rotation."""
    path = _fill(tmp_path / "held.log")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        logs.trim_in_place(path, max_bytes=8_000)
        os.write(fd, b"written after trimming\n")
    finally:
        os.close(fd)
    assert "written after trimming" in path.read_text()


def test_the_most_recent_entries_are_what_survive(tmp_path):
    path = _fill(tmp_path / "big.log")
    logs.trim_in_place(path, max_bytes=8_000)
    text = path.read_text()
    assert "line 19999" in text
    assert "line 0\n" not in text


def test_the_trimmed_file_does_not_start_mid_line(tmp_path):
    path = _fill(tmp_path / "big.log")
    logs.trim_in_place(path, max_bytes=8_000)
    lines = path.read_text().splitlines()
    assert lines[0] == "[earlier entries trimmed]"
    assert lines[1].startswith("line ")


def test_a_missing_file_is_not_an_error(tmp_path):
    assert logs.trim_in_place(tmp_path / "nope.log") is False


def test_housekeeping_never_breaks_a_run(tmp_path, monkeypatch):
    """A failure here must not take down the fetch it runs at the start of."""
    path = _fill(tmp_path / "big.log")
    monkeypatch.setattr(logs.Path, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert logs.trim_in_place(path, max_bytes=8_000) is False


# ---- Python-owned logs rotate normally, because Python owns the fd ----

def test_a_python_owned_log_rotates_and_stays_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "LOG_DIR", tmp_path)
    log = logs.rotating_logger("test_rotation", "r.log", max_bytes=2_000, backups=2)
    for i in range(400):
        log.info("sample %d with some padding to take up room", i)

    files = sorted(p.name for p in tmp_path.glob("r.log*"))
    assert files == ["r.log", "r.log.1", "r.log.2"]
    assert sum((tmp_path / f).stat().st_size for f in files) < 10_000


def test_the_same_logger_is_not_double_handled(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "LOG_DIR", tmp_path)
    first = logs.rotating_logger("test_once", "o.log")
    second = logs.rotating_logger("test_once", "o.log")
    assert first is second
    assert len(first.handlers) == 1
