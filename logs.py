"""Keeping the log files from growing without bound.

The poller runs 144 times a day, so anything it prints accumulates forever. But
rotating these logs is not the usual problem, because of who owns the file
descriptor.

**launchd opens `StandardOutPath` / `StandardErrorPath` itself and holds the fd
for the life of the process.** Rename or unlink that file and launchd carries on
writing to the now-unlinked inode: the visible log sits empty, the disk fills
anyway, and nothing indicates why. Classic logrotate-without-copytruncate, and
on a machine that runs this every ten minutes it would take a while to notice.

Two strategies, picked by who does the writing:

- **Python-owned logs** (`rotating_logger`) - the process writes them itself, so
  ordinary `RotatingFileHandler` renaming is safe. The plists point launchd at
  /dev/null so it never opens these at all.
- **launchd-owned logs** (`trim_in_place`) - the error streams, which must stay
  with launchd to capture failures that happen before Python starts, like a
  missing interpreter. These are truncated **in place**, preserving the inode,
  so launchd's O_APPEND descriptor keeps working against the shortened file.
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "data"

# Small: these are one line per run, and history beyond a few thousand runs has
# no value that the database doesn't already hold better.
MAX_BYTES = 512 * 1024
BACKUP_COUNT = 3


def rotating_logger(name: str, filename: str = None,
                    max_bytes: int = MAX_BYTES, backups: int = BACKUP_COUNT) -> logging.Logger:
    """A logger that rotates its own file. Safe because Python owns the fd."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / (filename or f"{name}.log"),
        maxBytes=max_bytes, backupCount=backups,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def trim_in_place(path: Path, max_bytes: int = MAX_BYTES, keep_fraction: float = 0.5) -> bool:
    """Shortens a file that another process is appending to, keeping the tail.

    Truncating rather than renaming is the whole point: launchd's descriptor
    stays valid and its O_APPEND writes resume against the shortened file. A
    rename would leave it writing to an inode with no name.

    Returns True if it trimmed.
    """
    path = Path(path)
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return False
        keep = int(max_bytes * keep_fraction)
        with path.open("rb") as f:
            f.seek(-keep, os.SEEK_END)
            tail = f.read()
        # Drop a partial first line so the file doesn't start mid-sentence.
        newline = tail.find(b"\n")
        if newline != -1:
            tail = tail[newline + 1:]
        with path.open("r+b") as f:
            f.truncate(0)
            f.write(b"[earlier entries trimmed]\n")
            f.write(tail)
        return True
    except OSError:
        # Log housekeeping must never be the thing that breaks a run.
        return False


def trim_launchd_logs() -> list[str]:
    """Trims the streams launchd owns. Called at the start of a run."""
    trimmed = []
    for name in ("fetch.err.log", "poll.err.log", "fetch.log"):
        if trim_in_place(LOG_DIR / name):
            trimmed.append(name)
    return trimmed
