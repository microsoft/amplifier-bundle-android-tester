"""Host-global, cross-process AVD leases and port allocation.

The bug this exists to fix: an AVD directory holds mutable QCOW2 disk state
and cannot be opened by two `emulator` processes at once. Measured live:
three concurrent `start_emulator(avd="concur-probe")` calls with no port, on
one host, from three unrelated (mutually unaware) Amplifier sessions --
**all three failed**, emulator processes exiting almost immediately. Worse,
without an explicit port, two sessions starting at the same instant would
each adopt whichever adb serial appeared first -- a coin flip over whose
emulator is whose (Defect 2).

Design note, important: the honest fix for three unrelated projects is that
they should each have their OWN AVD, not share one -- this module does not
make AVD-sharing safe, it makes the collision *discoverable and loud*
(refuse immediately, name the owning pid, point at `create_avd`) instead of
silently corrupting both instances' disk state.

## Relationship to lock.py

`lock.py`'s `dump_lock` is a plain anonymous mutex: its file's *existence*
is all that matters, never its content, and it protects one specific
operation (`uiautomator dump`) for the duration of a single call. A lease
here is different: the file's *content* (who holds it, on what port/serial,
since when) is the actual data other processes need to make correctness
decisions, and a lease is logically held for an entire emulator's
lifetime -- far longer than any single tool call. Continuously holding an
OS-level `flock` across that whole lifetime would require keeping a file
descriptor open across every subsequent tool invocation in the session
(and would still not survive a crash any better than the approach below).
Instead: `fcntl.flock` here is used ONLY to guard the brief
read-current-state-then-decide-then-write critical section on each mutation
(acquire / update / release) -- exactly the same primitive as lock.py, just
scoped to a metadata read-modify-write instead of an entire operation.
Ownership across the gap between mutations is enforced by recording the
owning pid and checking its liveness (`os.kill(pid, 0)`) -- when a
recording process has died (crash, kill -9, `amplifier` session ended
uncleanly), the very next `acquire_avd_lease` call for that AVD detects the
dead pid and MAY reclaim the lease instead of deadlocking forever.

## Owner-pid-dead is NOT the same as stale (measured live)

A dead owner pid is only NECESSARY evidence of staleness, never SUFFICIENT.
The recorded "owner" is the *session/tool process* that called
`start_emulator` -- not the emulator subprocess itself, which is launched
detached (`start_new_session=True`, see emulator.py) specifically so it
survives that session ending. **An emulator routinely outlives the session
that started it.** Measured live: session S1 (pid P) started an emulator,
then exited normally; the emulator kept running. A later, unrelated call
treated the dead-pid lease as stale and reclaimed it -- one path let a
second `start_emulator` proceed to launch a second instance against the
same (still-open) AVD disk, caught only by QEMU's own file lock rather than
by this module; another let `stop_emulator` kill a live emulator out from
under nobody, with no refusal at all.

So staleness is judged from the emulator/device itself, not merely the
owner pid. Three states, not two:

| Owner pid | Emulator/device | State      | Meaning |
|-----------|------------------|------------|---------|
| alive     | (any)            | **Owned**    | A live session still holds this -- refuse. |
| dead      | alive            | **Orphaned** | No live session, but the emulator is still running -- refuse (it's someone's data, just unattended); `force=True` is the explicit, informed override. |
| dead      | dead             | **Stale**    | Nothing is actually using this -- reclaim freely. |

Callers that can determine device liveness (chiefly `emulator.py`, which
has `adb devices` / `adb -s <serial> get-state` available) pass a
`device_liveness_probe` callable so `acquire_avd_lease`/`stop_emulator` can
tell Orphaned apart from Stale. Callers that don't supply one (direct
`leases.py` use, most existing tests) get the conservative pre-existing
behaviour: dead pid alone reclaims/proceeds, exactly as before -- the probe
is strictly additive, never a required parameter.

The reclaim-under-lock sequence is what makes this race-safe: two callers
racing to acquire the SAME avd's lease at the same instant are serialised
by `flock`, and each one re-reads the just-updated state after acquiring
it -- never deciding based on a stale read taken before the lock.

## CRITICAL: the lease directory is HOST-GLOBAL

Unlike `run_dir`/`base_dir` (which are per-session, derived from `work_dir`),
`DEFAULT_LEASE_DIR` is a single fixed path. Three unrelated sessions with
three different `work_dir`s must all see the SAME lease directory, or the
whole point of cross-session collision detection is lost -- an AVD lease
scoped to `~/project-a/.sessions/leases/` would never see a lease held by
a process using `~/project-b/.sessions/leases/`. Callers may override the
directory via config (mainly for test isolation), but the default is never
derived from `work_dir`.

POSIX-only (`fcntl`), matching the rest of this tool's host assumptions.

Honest limit: this module protects processes that go through
`start_emulator`/`stop_emulator`. An emulator started by hand, by a
different tool, or by a version of this tool that predates leasing, has no
lease record at all -- `stop_emulator` treats that as "cannot verify
ownership" and refuses by default (same as a live foreign owner), rather
than assuming it's safe.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_LEASE_DIR",
    "PORT_ALLOC_LOCK_NAME",
    "AvdLease",
    "AvdLeaseError",
    "acquire_avd_lease",
    "describe_lease",
    "find_lease_by_serial",
    "is_pid_alive",
    "list_live_leases",
    "parse_port_from_serial",
    "pick_free_port",
    "read_lease",
    "record_lease_serial",
    "release_avd_lease",
    "sanitize_name_for_filename",
]


# Fixed, host-global -- NEVER derived from a per-session `work_dir`. See the
# module docstring's "CRITICAL" section above.
DEFAULT_LEASE_DIR = Path("~/.amplifier/android-sessions/leases").expanduser()

PORT_ALLOC_LOCK_NAME = "_port-allocation.lock"

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_name_for_filename(name: str) -> str:
    """Filesystem-safe filename fragment for an AVD name. Same conservative
    collapsing as `lock.sanitize_serial_for_filename` -- AVD names are
    generally safe already, but this guards against surprises (spaces,
    path separators) rather than assuming."""
    sanitized = _UNSAFE_CHARS_RE.sub("_", name.strip())
    return sanitized or "unknown-avd"


def is_pid_alive(pid: int) -> bool:
    """`True` iff `pid` currently identifies a running process, checked via
    `os.kill(pid, 0)` (sends no signal -- just probes existence/permission).
    A `PermissionError` still means the process exists (just isn't ours to
    signal), so only `ProcessLookupError`/`OSError` "no such process" counts
    as dead."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Any other OSError (e.g. PermissionError) means the pid DOES exist
        # -- we just aren't allowed to signal it. Treat as alive: we cannot
        # honestly claim a lease is stale when we can't even prove the pid
        # is gone.
        return True
    return True


def parse_port_from_serial(serial: str) -> int | None:
    """`'emulator-5554'` -> `5554`. `None` for anything else (a physical
    device serial, a malformed string, ...) -- never raises."""
    prefix = "emulator-"
    if not serial.startswith(prefix):
        return None
    tail = serial[len(prefix) :]
    return int(tail) if tail.isdigit() else None


class AvdLeaseError(RuntimeError):
    """Raised when an AVD lease cannot be acquired (held by another live
    process) or a stop is refused (serial not verifiably owned by the
    caller). Carries `.extra` -- structured fields (owner pid, avd, port,
    held duration, ...) that must reach the caller intact, mirroring
    `AvdError`/`LaunchError` elsewhere in this package."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra: dict[str, Any] = extra


@dataclass
class AvdLease:
    """The full content of one AVD's lease file."""

    avd: str
    pid: int
    port: int | None
    serial: str | None
    started_at: float
    # Only meaningful at the moment `acquire_avd_lease` returns -- True iff
    # port auto-allocation was requested (no explicit port) and every port
    # in the requested range was already in use/leased. Persisted along
    # with everything else for simplicity; not load-bearing once read back.
    port_allocation_exhausted: bool = field(default=False)

    def held_for_s(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def describe_lease(lease: AvdLease) -> str:
    """Human-readable one-liner for error/warning messages."""
    port_part = f"port {lease.port}" if lease.port is not None else "no port recorded"
    serial_part = f", serial {lease.serial}" if lease.serial else ""
    return (
        f"pid {lease.pid}, {port_part}{serial_part}, held for {lease.held_for_s():.1f}s"
    )


def _lease_path(avd: str, lease_dir: Path) -> Path:
    lease_dir.mkdir(parents=True, exist_ok=True)
    return lease_dir / f"{sanitize_name_for_filename(avd)}.lease"


def _parse_lease_json(raw: str) -> AvdLease | None:
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
        return AvdLease(
            avd=data["avd"],
            pid=int(data["pid"]),
            port=data.get("port"),
            serial=data.get("serial"),
            started_at=float(data["started_at"]),
            port_allocation_exhausted=bool(
                data.get("port_allocation_exhausted", False)
            ),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Malformed/partial content (e.g. a read racing a concurrent write
        # on a filesystem without atomic small-write guarantees) -- treated
        # as "no usable lease" rather than propagating a parse error up
        # through what is meant to be a best-effort diagnostic read.
        return None


def _read_lease_path(path: Path) -> AvdLease | None:
    """Lock-free read straight from disk -- for best-effort scans/reporting
    only (`read_lease`, `find_lease_by_serial`, `list_live_leases`). Never
    used to decide acquire/release outcomes -- those re-read under the
    file's own flock (see `acquire_avd_lease`) to stay race-safe."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_lease_json(raw)


def _read_locked(fh: Any) -> AvdLease | None:
    fh.seek(0)
    return _parse_lease_json(fh.read())


def _write_locked(fh: Any, lease: AvdLease) -> None:
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps(lease.to_dict()))
    fh.flush()


def read_lease(avd: str, lease_dir: Path | None = None) -> AvdLease | None:
    """Best-effort read of `avd`'s lease, or `None` if absent/unparseable.
    No locking -- for diagnostics/reporting only."""
    effective_dir = lease_dir or DEFAULT_LEASE_DIR
    return _read_lease_path(_lease_path(avd, effective_dir))


def find_lease_by_serial(serial: str, lease_dir: Path | None = None) -> AvdLease | None:
    """Reverse lookup: which AVD's lease (if any) currently records
    `serial`? Used by `stop_emulator` -- callers only have a serial, not
    the AVD name, and the owning process may be a completely different one
    than whichever started this tool instance."""
    effective_dir = lease_dir or DEFAULT_LEASE_DIR
    if not effective_dir.exists():
        return None
    for path in effective_dir.glob("*.lease"):
        lease = _read_lease_path(path)
        if lease is not None and lease.serial == serial:
            return lease
    return None


def list_live_leases(
    lease_dir: Path | None = None, *, skip_avd: str | None = None
) -> list[AvdLease]:
    """Every lease currently recorded under `lease_dir` whose owner pid is
    still alive -- i.e. leases that actually mean something right now, as
    opposed to abandoned/stale ones. `skip_avd`, if given, excludes that
    AVD's own lease file from the results (used by port allocation, which
    is scanning for OTHER avds' claimed ports while already holding --
    and about to overwrite -- its own)."""
    effective_dir = lease_dir or DEFAULT_LEASE_DIR
    if not effective_dir.exists():
        return []
    skip_name = sanitize_name_for_filename(skip_avd) if skip_avd is not None else None
    live: list[AvdLease] = []
    for path in effective_dir.glob("*.lease"):
        if skip_name is not None and path.stem == skip_name:
            continue
        lease = _read_lease_path(path)
        if lease is not None and is_pid_alive(lease.pid):
            live.append(lease)
    return live


def pick_free_port(*, port_min: int, port_max: int, in_use: set[int]) -> int | None:
    """The lowest even port in `[port_min, port_max]` not in `in_use`, or
    `None` if every port in range is taken. Pure function -- no I/O, no
    locking -- so it's trivially testable independent of the filesystem
    scanning that builds `in_use`."""
    for candidate in range(port_min, port_max + 1, 2):
        if candidate not in in_use:
            return candidate
    return None


def acquire_avd_lease(
    avd: str,
    *,
    port: int | None = None,
    serial: str | None = None,
    lease_dir: Path | None = None,
    pid: int | None = None,
    allocate_port_range: tuple[int, int] | None = None,
    attached_ports: frozenset[int] = frozenset(),
    device_liveness_probe: Callable[[str], bool] | None = None,
) -> AvdLease:
    """Acquire an exclusive, cross-process lease on `avd`.

    If `port` is given, it is simply recorded (no allocation). If `port` is
    `None` and `allocate_port_range=(port_min, port_max)` is given, a free
    port is chosen and recorded atomically as part of THIS SAME acquire
    call -- picked while holding a dedicated, host-global port-allocation
    lock (serialising against every other AVD's concurrent auto-allocation,
    not just this one's), and written into this avd's own lease file before
    that port-allocation lock is released, so no other concurrent
    allocation can ever observe the port as free and pick it too. If every
    port in range is taken, `port` is left `None` and
    `AvdLease.port_allocation_exhausted` is `True` -- the caller is
    expected to fall back to a non-deterministic "any new serial" wait in
    that case (see `emulator.start_emulator`).

    `device_liveness_probe`, if given, is called with a serial and must
    return `True` iff that serial currently identifies a live/attached
    device. It is consulted ONLY when the existing lease's owner pid is
    dead AND the existing lease has a recorded serial -- to distinguish
    **Orphaned** (owner dead, emulator still running -- refuse) from
    **Stale** (owner dead, emulator also gone -- reclaim). See the module
    docstring's "Owner-pid-dead is NOT the same as stale" section. Without
    a probe (the default), a dead owner pid alone is treated as stale, same
    as before this distinction existed -- this parameter is purely
    additive and never required.

    Raises:
        AvdLeaseError: `avd` is already leased by a DIFFERENT, currently
            live process (Owned), or by a dead process whose emulator is
            still running (Orphaned -- only detected when
            `device_liveness_probe` is given). `.extra` carries `avd`,
            `owner_pid`, `port`, `serial`, `held_for_s`, and (for the
            Orphaned case) `orphaned=True` -- naming exactly who/what holds
            it and for how long, so the caller can report it (and suggest
            using the existing serial directly, forcing a stop, or
            `create_avd`) rather than a bare refusal.

    A lease recorded by a now-dead pid whose emulator is ALSO gone (Stale)
    is reclaimed automatically (no error) -- the read-decide-write sequence
    happens entirely inside this avd's own flock, so two callers racing to
    acquire the SAME avd at the same instant can never both believe they
    won.
    """
    effective_dir = lease_dir or DEFAULT_LEASE_DIR
    effective_pid = pid if pid is not None else os.getpid()
    avd_lease_path = _lease_path(avd, effective_dir)

    alloc_fh = None
    if port is None and allocate_port_range is not None:
        effective_dir.mkdir(parents=True, exist_ok=True)
        alloc_fh = open(effective_dir / PORT_ALLOC_LOCK_NAME, "a+b")  # noqa: SIM115
        fcntl.flock(alloc_fh.fileno(), fcntl.LOCK_EX)

    try:
        with open(avd_lease_path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                existing = _read_locked(fh)
                if existing is not None and existing.pid != effective_pid:
                    if is_pid_alive(existing.pid):
                        raise AvdLeaseError(
                            f"AVD {avd!r} is already leased by pid {existing.pid} "
                            f"({describe_lease(existing)}). Its QCOW2 disk state "
                            "cannot be opened by a second emulator process without "
                            "corrupting both -- refusing rather than silently "
                            "colliding. Use a different AVD, or run 'create_avd' "
                            f"to provision your own (e.g. name={avd}-<your-project>).",
                            avd=avd,
                            owner_pid=existing.pid,
                            port=existing.port,
                            serial=existing.serial,
                            held_for_s=existing.held_for_s(),
                            suggested_operation="create_avd",
                        )
                    # Owner pid is dead -- but that alone does NOT mean the
                    # AVD is free. An emulator commonly outlives the session
                    # that started it (see module docstring). Only when we
                    # can positively confirm the emulator itself is ALSO
                    # gone is this genuinely stale; otherwise it's orphaned
                    # and must be refused just as loudly as a live owner.
                    if (
                        existing.serial is not None
                        and device_liveness_probe is not None
                        and device_liveness_probe(existing.serial)
                    ):
                        raise AvdLeaseError(
                            f"AVD {avd!r} is already running as "
                            f"{existing.serial!r} -- orphaned: its owning "
                            f"session (pid {existing.pid}) is gone, but the "
                            f"emulator itself is still running "
                            f"({describe_lease(existing)}). An emulator "
                            "commonly outlives the session that started it, "
                            "so a dead owner process does not mean this AVD "
                            "is free. Use "
                            f"{existing.serial!r} directly, call "
                            f"stop_emulator(serial={existing.serial!r}, "
                            "force=true) if you want to replace it, or run "
                            "'create_avd' to provision your own AVD instead "
                            "of sharing this one.",
                            avd=avd,
                            owner_pid=existing.pid,
                            port=existing.port,
                            serial=existing.serial,
                            held_for_s=existing.held_for_s(),
                            orphaned=True,
                            suggested_operation="create_avd",
                        )
                    # else: genuinely stale (owner dead, emulator also gone,
                    # or its liveness could not be determined at all) --
                    # fall through and reclaim below.

                chosen_port = port
                exhausted = False
                if chosen_port is None and allocate_port_range is not None:
                    port_min, port_max = allocate_port_range
                    in_use = set(attached_ports) | {
                        lease.port
                        for lease in list_live_leases(effective_dir, skip_avd=avd)
                        if lease.port is not None
                    }
                    chosen_port = pick_free_port(
                        port_min=port_min, port_max=port_max, in_use=in_use
                    )
                    exhausted = chosen_port is None

                new_lease = AvdLease(
                    avd=avd,
                    pid=effective_pid,
                    port=chosen_port,
                    serial=serial,
                    started_at=time.time(),
                    port_allocation_exhausted=exhausted,
                )
                _write_locked(fh, new_lease)
                return new_lease
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        if alloc_fh is not None:
            fcntl.flock(alloc_fh.fileno(), fcntl.LOCK_UN)
            alloc_fh.close()


def record_lease_serial(avd: str, serial: str, lease_dir: Path | None = None) -> None:
    """Update the serial recorded on `avd`'s lease once it's actually known
    (after the emulator attaches to adb) -- called by whichever process
    already holds the lease. A no-op if the lease has since disappeared
    (e.g. released by a concurrent failure-cleanup)."""
    effective_dir = lease_dir or DEFAULT_LEASE_DIR
    path = _lease_path(avd, effective_dir)
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            existing = _read_locked(fh)
            if existing is None:
                return
            existing.serial = serial
            _write_locked(fh, existing)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def release_avd_lease(
    avd: str, *, expected_pid: int | None = None, lease_dir: Path | None = None
) -> bool:
    """Release `avd`'s lease (clear the file's content).

    If `expected_pid` is given and a DIFFERENT, currently live pid holds
    the lease, the release is refused (returns `False`, file untouched) --
    a caller should only ever pass its own pid here, so this is a safety
    net against releasing someone else's lease by mistake, not a normal
    code path.

    Returns `True` if the lease is now clear (already absent counts as
    success -- release is idempotent).
    """
    effective_dir = lease_dir or DEFAULT_LEASE_DIR
    path = _lease_path(avd, effective_dir)
    if not path.exists():
        return True
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            existing = _read_locked(fh)
            if existing is None:
                return True
            if (
                expected_pid is not None
                and existing.pid != expected_pid
                and is_pid_alive(existing.pid)
            ):
                return False
            fh.seek(0)
            fh.truncate()
            fh.flush()
            return True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
