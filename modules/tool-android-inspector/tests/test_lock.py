"""Unit tests for lock.py -- the per-serial, cross-process dump lock.

Defect 1 (per-serial serialisation): proves the lock actually serialises
concurrent holders (via threads, since `fcntl.flock` locks are scoped to the
*open file description*, not the process -- two separate `open()` calls
within one process/thread pool contend on the lock exactly like two separate
processes would), and that different serials do not contend with each
other.
"""

from __future__ import annotations

import threading
import time

from amplifier_module_tool_android_inspector.lock import (
    dump_lock,
    sanitize_serial_for_filename,
)


def test_sanitize_serial_for_filename_replaces_unsafe_chars() -> None:
    assert sanitize_serial_for_filename("192.168.1.5:5555") == "192.168.1.5_5555"


def test_sanitize_serial_for_filename_empty_falls_back() -> None:
    assert sanitize_serial_for_filename("") == "unknown-serial"
    assert sanitize_serial_for_filename("   ") == "unknown-serial"


def test_sanitize_serial_for_filename_preserves_safe_chars() -> None:
    assert sanitize_serial_for_filename("emulator-5554") == "emulator-5554"


def test_dump_lock_creates_lock_file_under_given_dir(tmp_path) -> None:
    with dump_lock("emulator-5554", tmp_path):
        pass
    assert (tmp_path / "emulator-5554.dump.lock").exists()


def test_dump_lock_serialises_same_serial_across_threads(tmp_path) -> None:
    """The load-bearing property: two concurrent holders of the SAME serial's
    lock must never be inside the critical section at the same time."""
    events: list[str] = []
    barrier = threading.Barrier(2)

    def worker(label: str, hold_s: float) -> None:
        barrier.wait()
        with dump_lock("emulator-5554", tmp_path):
            events.append(f"{label}-enter")
            time.sleep(hold_s)
            events.append(f"{label}-exit")

    t1 = threading.Thread(target=worker, args=("A", 0.2))
    t2 = threading.Thread(target=worker, args=("B", 0.0))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    # Whichever thread got there first, its enter/exit pair must be
    # contiguous -- the other thread's "enter" must not appear between them.
    assert events[0].endswith("enter")
    assert events[1].endswith("exit")
    assert events[0].split("-")[0] == events[1].split("-")[0]
    assert events[2].endswith("enter")
    assert events[3].endswith("exit")


def test_dump_lock_different_serials_do_not_contend(tmp_path) -> None:
    """Two different serials must be independently lockable -- one serial's
    lock being held must never block a dump against a different serial."""
    order: list[str] = []
    release_a = threading.Event()

    def hold_a() -> None:
        with dump_lock("serial-A", tmp_path):
            order.append("a-enter")
            release_a.wait(timeout=5)
            order.append("a-exit")

    t = threading.Thread(target=hold_a)
    t.start()
    # Give thread A a moment to acquire its lock first.
    time.sleep(0.1)

    # Serial B must acquire immediately, without waiting on A's lock.
    start = time.monotonic()
    with dump_lock("serial-B", tmp_path):
        order.append("b-enter")
        order.append("b-exit")
    elapsed = time.monotonic() - start

    release_a.set()
    t.join(timeout=5)

    assert elapsed < 1.0, (
        "serial-B waited on serial-A's lock -- locks are not per-serial"
    )
    assert order[0] == "a-enter"
    assert "b-enter" in order
    assert "b-exit" in order


def test_dump_lock_releases_on_exception(tmp_path) -> None:
    """A holder that raises inside the `with` block must still release the
    lock -- otherwise one failed dump would permanently wedge the serial."""

    class _Boom(Exception):
        pass

    try:
        with dump_lock("emulator-5554", tmp_path):
            raise _Boom("simulated failure mid-dump")
    except _Boom:
        pass

    # If the lock leaked, this would hang; give it a bounded window via a
    # thread + join so a real regression fails fast instead of hanging the
    # whole suite.
    acquired = threading.Event()

    def try_acquire() -> None:
        with dump_lock("emulator-5554", tmp_path):
            acquired.set()

    t = threading.Thread(target=try_acquire)
    t.start()
    t.join(timeout=2)
    assert acquired.is_set(), "lock was not released after an exception"
