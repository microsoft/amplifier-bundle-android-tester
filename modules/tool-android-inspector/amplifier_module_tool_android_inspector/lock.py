"""Cross-process, per-serial file lock for serialising `uiautomator dump`.

The bug this exists to fix: `tap`, `type_text`, `find`, and `wait_for` all
dump internally, and `wait_for`'s poll loop makes overlapping dumps against
the *same device* likely the moment two tool invocations (two agent
processes, two sessions) ever run concurrently against it. Two
`uiautomator dump` invocations racing on one device collide with:

    java.lang.IllegalStateException: UiAutomationService ... already registered!

and the adb shell typically reports exit 137. Observed 4 times in one field
run; it is what blocked Chat text-entry verification from ever completing.

An in-process lock (e.g. a plain `threading.Lock`) is not enough: sessions
can span processes (separate `amplifier` invocations, separate agent
processes), and the screenshot-clobbering defect earlier in this project
came from exactly that wrong assumption -- an in-process counter that reset
to zero every new process. The fix here is the same lesson applied to
locking: an OS-level file lock (`fcntl.flock`), which holds across
processes, not just across threads/tasks within one.

Honest limit: this lock only serialises *this tool's* own dumps against each
other. A different process on the host dumping the same device concurrently
(a sibling automation harness, a manual `adb shell uiautomator dump`, a
human at a terminal) is not covered by this lock and can still collide --
that is exactly the scenario `dump_ui_xml`'s retry-exhausted error names
explicitly rather than silently implying a complete fix.

POSIX-only (`fcntl`), matching the rest of this tool's host assumptions --
`avd.py`/`emulator.py` already assume a Linux host for KVM/ptrace_scope
checks; there is no existing Windows-support path in this codebase.
"""

from __future__ import annotations

import contextlib
import fcntl
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path

__all__ = ["DEFAULT_LOCK_DIR", "dump_lock", "sanitize_serial_for_filename"]

# Used only when a caller doesn't have a work directory to hand (e.g. a bare
# unit test constructing ui.py functions directly). Real production use
# threads an explicit `lock_dir` (the tool's per-session `run_dir`) down from
# `__init__.py`, so independently-configured `work_dir`s don't matter -- all
# of *this* process's dumps for a serial always contend on the same path.
DEFAULT_LOCK_DIR = Path(tempfile.gettempdir()) / "amplifier-android-inspector-locks"

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_serial_for_filename(serial: str) -> str:
    """Filesystem-safe filename fragment for a device serial.

    Serials can contain ':' (network-attached devices, e.g.
    '192.168.1.5:5555'), which is unsafe in filenames on some filesystems --
    collapse anything outside a conservative safe set to '_'.
    """
    sanitized = _UNSAFE_CHARS_RE.sub("_", serial.strip())
    return sanitized or "unknown-serial"


def _lock_path(serial: str, lock_dir: Path) -> Path:
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"{sanitize_serial_for_filename(serial)}.dump.lock"


@contextlib.contextmanager
def dump_lock(serial: str, lock_dir: Path | None = None) -> Iterator[None]:
    """Acquire an OS-level, cross-process exclusive lock scoped to `serial`.

    Blocks until acquired (`fcntl.flock(LOCK_EX)`) -- there is no timeout
    here, because lock ACQUISITION is not the failure mode this fixes (this
    tool's own dumps always eventually release); the specific
    "already registered" collision is handled by `dump_ui_xml`'s bounded
    retry, which runs *inside* this lock so a competing dump from a
    different process is what its retry is actually guarding against.

    The lock file itself is never deleted (only unlocked) -- deleting it
    would reopen exactly the race this exists to close, if another process
    is mid-open on the same path.
    """
    effective_dir = lock_dir or DEFAULT_LOCK_DIR
    path = _lock_path(serial, effective_dir)
    with open(path, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
