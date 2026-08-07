"""Host-workaround-aware emulator lifecycle.

Encodes the aarch64/KVM host quirks that cost ~57 bash calls of yak-shaving to
discover the first time (see docs/TROUBLESHOOTING.md in the bundle):

- `kernel.yama.ptrace_scope != 0` kills the emulator's own crash handler (it
  self-ptraces at startup). Detected and worked around by launching under
  `gdb -batch` with `handle all nostop noprint pass` — "handle all" is
  load-bearing: QEMU uses SIGUSR1/SIGUSR2/SIGCONT internally, and silencing
  only SIGSEGV lets gdb stop-and-kill a perfectly healthy booting emulator.
- `-no-window` is a crash workaround, not an optimisation (the windowed qemu
  binary needs libpcre2-16.so.0 on this class of host).
- Backgrounded processes must survive the tool call returning: launched via
  `start_new_session=True` (the Python equivalent of `setsid`) with
  stdin=DEVNULL, not a bare fire-and-forget subprocess.
- Readiness is two-stage: `wait-for-device` THEN poll `sys.boot_completed`,
  THEN `input keyevent 82` to clear the keyguard. Never one without the
  others.
- Serial discovery snapshots `adb devices` BEFORE launch and only accepts a
  genuinely NEW serial as ours — never "the first emulator- entry", which
  could belong to someone else's already-running instance.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adb import AdbClient, AdbClientLike, AdbError, list_raw_devices
from .evidence import unique_evidence_path
from .leases import (
    AvdLeaseError,
    acquire_avd_lease,
    describe_lease,
    find_lease_by_serial,
    is_pid_alive,
    parse_port_from_serial,
    record_lease_serial,
    release_avd_lease,
)

__all__ = [
    "DEFAULT_BOOT_TIMEOUT_S",
    "DEFAULT_DEVICE_APPEAR_TIMEOUT_S",
    "DEFAULT_EMULATOR_ARGS",
    "KEYCODE_MENU",
    "PORT_RANGE_MAX",
    "PORT_RANGE_MIN",
    "EmulatorError",
    "LaunchedEmulatorProcess",
    "build_gdb_launch_script",
    "dismiss_keyguard",
    "launch_emulator_process",
    "ptrace_scope",
    "resolve_emulator_binary",
    "snapshot_serials",
    "start_emulator",
    "stop_emulator",
    "validate_emulator_port",
    "wait_boot_completed",
    "wait_for_new_serial",
    "wait_for_specific_serial",
]


class EmulatorError(RuntimeError):
    """Raised for emulator lifecycle failures (no silent fallback/degradation)."""


DEFAULT_EMULATOR_ARGS: list[str] = [
    "-no-window",
    "-no-snapshot",
    "-no-boot-anim",
    "-gpu",
    "swiftshader_indirect",
]

DEFAULT_BOOT_TIMEOUT_S = 240.0
DEFAULT_DEVICE_APPEAR_TIMEOUT_S = 60.0
BOOT_POLL_INTERVAL_S = 2.0
DEVICE_POLL_INTERVAL_S = 1.0

KEYCODE_MENU = 82  # unlocks a non-secure keyguard on a cold-booted AVD

# adb only scans this range of console ports looking for emulators; the
# emulator itself uses `port` for its console and `port + 1` for adb, so an
# explicit port must be even and inside this range or adb will simply never
# find the resulting adb port, regardless of what the emulator does with it.
PORT_RANGE_MIN = 5554
PORT_RANGE_MAX = 5682

# Short per-attempt timeout for polling device readiness (see
# `_wait_device_ready`). `adb wait-for-device` blocks uninterruptibly for
# whatever timeout it's given, so a single long call could outlive a process
# that died moments after launch. Bounded attempts let us recheck process
# liveness between them instead.
_WAIT_DEVICE_ATTEMPT_TIMEOUT_S = 2.0

# Lines containing any of these (case-insensitive) are treated as the most
# diagnostic content in an emulator log when a launched process has died --
# preferred over a plain tail so the caller sees the cause, not just
# whatever happened to be printed last.
_LOG_DIAGNOSTIC_MARKERS = (
    "sigsegv",
    "segmentation fault",
    "error",
    "cannot open",
    "failed to",
    "fatal",
    "no longer exists",
)

_PTRACE_SCOPE_PATH = Path("/proc/sys/kernel/yama/ptrace_scope")

_AUTO = object()


# ---------------------------------------------------------------------------
# Host workaround detection
# ---------------------------------------------------------------------------


def ptrace_scope(path: Path = _PTRACE_SCOPE_PATH) -> int | None:
    """Read kernel.yama.ptrace_scope. None if absent (non-Linux, or no YAMA)."""
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def build_gdb_launch_script(avd: str, extra_emulator_args: list[str]) -> str:
    """The gdb batch script that keeps a permanent, all-signals-passthrough
    tracer attached so the emulator's self-ptrace startup check succeeds.

    `handle all nostop noprint pass` is load-bearing: QEMU's threads use
    SIGUSR1/SIGUSR2/SIGCONT internally for ordinary event-loop wakeups. If
    only SIGSEGV were silenced, the first internal SIGUSR1 would stop the
    inferior; gdb's batch script would then hit end-of-script with the
    program merely "stopped, not exited", and `set confirm off` auto-answers
    yes to gdb's own "kill the program on exit?" prompt — silently killing a
    perfectly healthy booting emulator.
    """
    args = " ".join(["-avd", avd, *extra_emulator_args])
    return (
        "set pagination off\n"
        "set confirm off\n"
        "handle all nostop noprint pass\n"
        f"run {args}\n"
    )


def validate_emulator_port(port: int) -> None:
    """Validate an explicit emulator console port BEFORE anything is
    launched. The emulator uses `port` for its own console and `port + 1`
    for adb, so `port` must be even; adb additionally only scans
    `PORT_RANGE_MIN`-`PORT_RANGE_MAX` for emulator consoles, so a port
    outside that range would never be discoverable by adb regardless of
    what the emulator does with it.

    Raises:
        EmulatorError: naming the exact constraint violated. Never silently
            adjusted to a nearby valid value.
    """
    if port % 2 != 0:
        raise EmulatorError(
            f"Invalid emulator port {port}: must be even (the emulator uses "
            "'port' for its own console and 'port + 1' for adb -- an odd "
            "port leaves the adb port undefined)."
        )
    if not (PORT_RANGE_MIN <= port <= PORT_RANGE_MAX):
        raise EmulatorError(
            f"Invalid emulator port {port}: must be in the "
            f"{PORT_RANGE_MIN}-{PORT_RANGE_MAX} range that adb scans for "
            "emulator consoles."
        )


def resolve_emulator_binary(config: dict[str, Any] | None = None) -> str:
    config = config or {}
    override = config.get("emulator_path")
    if override:
        path = Path(str(override)).expanduser()
        if not path.exists():
            raise EmulatorError(f"Configured emulator_path does not exist: {path}")
        return str(path)

    android_home = (
        config.get("android_home")
        or os.environ.get("ANDROID_HOME")
        or os.environ.get("ANDROID_SDK_ROOT")
    )
    if not android_home:
        raise EmulatorError(
            "ANDROID_HOME/ANDROID_SDK_ROOT not set and no 'emulator_path' configured; "
            "cannot locate the emulator binary."
        )
    binary = Path(str(android_home)).expanduser() / "emulator" / "emulator"
    if not binary.exists():
        raise EmulatorError(f"emulator binary not found at {binary}.")
    return str(binary)


# ---------------------------------------------------------------------------
# Process liveness diagnostics
#
# Every wait loop below (serial appearance, device-ready, boot-completed)
# accepts an optional `process` (a Popen-like handle) and `log_path`. When
# given, the loop polls the process for liveness on every iteration and
# stops the MOMENT it has exited — never waiting out the rest of the
# timeout budget on a process that can never satisfy what's being waited
# for. Measured live: a segfaulted emulator process left `start_emulator`
# sitting in `adb wait-for-device` for the full 240s boot timeout before
# reporting a symptom ("adb timed out") instead of the cause (the process
# died), even though the emulator's own log named the cause the whole time.
# ---------------------------------------------------------------------------


def _describe_exit(returncode: int) -> str:
    """Human-readable description of a Popen returncode: signal name for a
    negative (signal-terminated) code, else a plain exit-code message."""
    if returncode < 0:
        signum = -returncode
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = f"signal {signum}"
        return f"was terminated by {name} ({signum})"
    return f"exited with code {returncode}"


def _log_excerpt(log_path: Path | None, *, max_lines: int = 40) -> list[str]:
    """The most useful lines from an emulator log for diagnosing a crash:
    lines matching a known crash/error marker if any are present, else the
    last `max_lines` lines. Never raises — a missing/unreadable/absent log
    yields an empty list rather than masking the real error with a new one.
    """
    if log_path is None:
        return []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    diagnostic = [
        ln for ln in lines if any(m in ln.lower() for m in _LOG_DIAGNOSTIC_MARKERS)
    ]
    if diagnostic:
        return diagnostic[-max_lines:]
    return lines[-max_lines:]


def _process_death_error(
    process: Any, returncode: int, log_path: Path | None
) -> EmulatorError:
    """Build the structured error for a process that died while something
    was waiting on it: the exit code/signal, the log path, a diagnostic
    excerpt from the log (so the caller sees the cause without a second
    round trip), and — when the death looks like a signal and
    `ptrace_scope != 0` — a pointer to the documented workaround."""
    cause = _describe_exit(returncode)
    excerpt = _log_excerpt(log_path)
    excerpt_text = "\n".join(excerpt) if excerpt else "(log empty or unavailable)"

    hint = ""
    if returncode < 0:
        scope = ptrace_scope()
        if scope is not None and scope != 0:
            hint = (
                " kernel.yama.ptrace_scope != 0 on this host — see "
                "docs/TROUBLESHOOTING.md ('The ptrace_scope crash') for the "
                "gdb workaround this tool applies; if this still happens, "
                "the workaround itself needs attention."
            )

    log_note = f"See emulator log: {log_path}." if log_path is not None else ""
    pid = getattr(process, "pid", "?")
    return EmulatorError(
        f"Emulator process (pid {pid}) {cause} while waiting for it to become "
        f"ready — it will never respond, so waiting further would be "
        f"pointless. {log_note}\n"
        f"Last diagnostic lines from the log:\n{excerpt_text}{hint}"
    )


def _check_process_alive(process: Any, log_path: Path | None) -> None:
    """Raise immediately if `process` has already exited. `process=None`
    (no process to check — e.g. no explicit port, waiting on a device that
    was already attached) is a silent no-op."""
    if process is None:
        return
    returncode = process.poll()
    if returncode is not None:
        raise _process_death_error(process, returncode, log_path)


# ---------------------------------------------------------------------------
# Serial discovery
# ---------------------------------------------------------------------------


def snapshot_serials(adb_path: str) -> set[str]:
    """The set of currently-attached device serials, for before/after diffing."""
    return {d.serial for d in list_raw_devices(adb_path)}


def wait_for_new_serial(
    before: set[str],
    *,
    list_serials_fn: Callable[[], set[str]],
    timeout_s: float = DEFAULT_DEVICE_APPEAR_TIMEOUT_S,
    poll_s: float = DEVICE_POLL_INTERVAL_S,
    process: Any = None,
    log_path: Path | None = None,
) -> str:
    """Poll until a serial appears that was NOT in `before`. Only a genuinely
    new serial is accepted as ours — never "the first emulator- entry",
    which could belong to an already-running instance.

    If `process` (the emulator's Popen-like handle) is given, it is polled
    for liveness every iteration — see the "Process liveness diagnostics"
    section above.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        _check_process_alive(process, log_path)
        current = list_serials_fn()
        new = current - before
        if new:
            return min(new)
        if time.monotonic() >= deadline:
            raise EmulatorError(
                f"No new adb device serial appeared within {timeout_s}s."
            )
        time.sleep(poll_s)


def wait_for_specific_serial(
    serial: str,
    *,
    list_serials_fn: Callable[[], set[str]],
    timeout_s: float = DEFAULT_DEVICE_APPEAR_TIMEOUT_S,
    poll_s: float = DEVICE_POLL_INTERVAL_S,
    process: Any = None,
    log_path: Path | None = None,
) -> str:
    """Poll until `serial` specifically appears in `list_serials_fn()`.

    Used when an explicit `port` was given to `start_emulator`: the expected
    serial is then deterministic (`emulator-<port>`), so waiting for that
    exact serial is preferred over the "any new serial" heuristic
    `wait_for_new_serial` uses when no port is pinned. Liveness checking
    against `process`/`log_path` behaves identically to `wait_for_new_serial`
    — see the "Process liveness diagnostics" section above.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        _check_process_alive(process, log_path)
        if serial in list_serials_fn():
            return serial
        if time.monotonic() >= deadline:
            raise EmulatorError(
                f"Expected serial {serial!r} did not appear within {timeout_s}s."
            )
        time.sleep(poll_s)


# ---------------------------------------------------------------------------
# Process launch
# ---------------------------------------------------------------------------


@dataclass
class LaunchedEmulatorProcess:
    """What `launch_emulator_process` hands back: the pid and log path
    callers already relied on, plus the Popen-like `process` handle itself
    so readiness waiters can poll it for liveness (see "Process liveness
    diagnostics" above) instead of blindly waiting out a timeout on a
    process that has already died."""

    pid: int
    log_path: Path
    process: Any  # Popen-like; `Any` so a test double satisfies it structurally


def launch_emulator_process(
    avd: str,
    emulator_binary: str,
    config: dict[str, Any],
    run_dir: Path,
    *,
    port: int | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    ptrace_scope_override: int | None | object = _AUTO,
) -> LaunchedEmulatorProcess:
    """Launch the emulator detached (survives the tool call returning), applying
    the gdb ptrace_scope workaround if the host needs it.

    `port`, when given, is appended as `-port <port>` to the emulator's argv
    -- in both the direct-launch and gdb-wrapped branches, since the gdb
    branch's `run` line is built from the same `extra_args`. Callers must
    validate `port` (see `validate_emulator_port`) before calling this --
    this function does not re-validate it.

    Returns a `LaunchedEmulatorProcess` (pid, log_path, and the process
    handle itself).
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    # Collision-proof: a bare "emulator-{avd}.log" is silently truncated
    # (mode "wb") by every subsequent start_emulator call for the same AVD
    # name, destroying the previous boot's log. unique_evidence_path names
    # each boot's log distinctly instead.
    log_path = unique_evidence_path(run_dir, f"emulator-{avd}", "log")
    extra_args = list(config.get("emulator_args") or DEFAULT_EMULATOR_ARGS)
    if port is not None:
        extra_args = [*extra_args, "-port", str(port)]

    scope = ptrace_scope() if ptrace_scope_override is _AUTO else ptrace_scope_override
    needs_gdb = scope is not None and scope != 0

    if needs_gdb:
        gdb_path = config.get("gdb_path") or _which("gdb")
        if not gdb_path:
            raise EmulatorError(
                "kernel.yama.ptrace_scope != 0 on this host, which requires "
                "launching the emulator under gdb (its own crash handler "
                "self-ptraces at startup and YAMA blocks that) — but no 'gdb' "
                "binary was found on PATH. Install gdb, or configure 'gdb_path'."
            )
        script_path = run_dir / f"gdb-launch-{avd}.txt"
        script_path.write_text(
            build_gdb_launch_script(avd, extra_args), encoding="utf-8"
        )
        argv = [
            gdb_path,
            "-batch",
            "-x",
            str(script_path),
            "./" + Path(emulator_binary).name,
        ]
        cwd = str(Path(emulator_binary).parent)
    else:
        argv = [emulator_binary, "-avd", avd, *extra_args]
        cwd = str(Path(emulator_binary).parent)

    with open(log_path, "wb") as log_file:
        proc = popen_factory(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # setsid-equivalent: survives this call returning
            close_fds=True,
        )

    return LaunchedEmulatorProcess(pid=proc.pid, log_path=log_path, process=proc)


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


# ---------------------------------------------------------------------------
# Readiness (two-stage: wait-for-device THEN boot_completed THEN keyguard)
# ---------------------------------------------------------------------------


def _wait_device_ready(
    client: AdbClientLike,
    *,
    timeout_s: float,
    poll_s: float,
    process: Any,
    log_path: Path | None,
) -> None:
    """Wait for the device to report ready state, in short bounded attempts
    rather than one long blocking `adb wait-for-device` call.

    `adb wait-for-device` blocks uninterruptibly for whatever timeout it's
    given -- a single call using the full `timeout_s` could outlive an
    emulator process that died moments after launch, with no way to notice
    in the meantime. Polling in `_WAIT_DEVICE_ATTEMPT_TIMEOUT_S`-bounded
    attempts lets process liveness be rechecked between them instead.
    """
    deadline = time.monotonic() + timeout_s
    attempt_timeout = min(_WAIT_DEVICE_ATTEMPT_TIMEOUT_S, timeout_s)
    while True:
        _check_process_alive(process, log_path)
        try:
            client.wait_for_device(timeout=attempt_timeout)
            return
        except AdbError:
            pass  # per-attempt timeout, or a transient failure -- keep polling
        if time.monotonic() >= deadline:
            raise EmulatorError(
                f"Device did not report ready (wait-for-device) within {timeout_s}s."
            )
        time.sleep(poll_s)


def wait_boot_completed(
    client: AdbClientLike,
    *,
    timeout_s: float = DEFAULT_BOOT_TIMEOUT_S,
    poll_s: float = BOOT_POLL_INTERVAL_S,
    process: Any = None,
    log_path: Path | None = None,
) -> None:
    """Two-stage readiness: device-ready THEN `sys.boot_completed`.

    If `process` (the emulator's Popen-like handle) is given, BOTH stages
    check its liveness between poll attempts -- a process that has already
    exited is detected immediately rather than waited on for the full
    timeout. See `_wait_device_ready` for why the device-ready stage in
    particular can no longer be a single blocking `wait-for-device` call.
    """
    _wait_device_ready(
        client, timeout_s=timeout_s, poll_s=poll_s, process=process, log_path=log_path
    )
    deadline = time.monotonic() + timeout_s
    while True:
        _check_process_alive(process, log_path)
        result = client.shell("getprop sys.boot_completed")
        if result.stdout.strip() == "1":
            return
        if time.monotonic() >= deadline:
            raise EmulatorError(f"Android boot did not complete within {timeout_s}s.")
        time.sleep(poll_s)


def dismiss_keyguard(client: AdbClientLike) -> None:
    """Clear the non-secure keyguard a cold-booted AVD otherwise sits on,
    which would swallow the first launch intent."""
    client.run("shell", "input", "keyevent", str(KEYCODE_MENU), check_output=False)


# ---------------------------------------------------------------------------
# Full orchestration
# ---------------------------------------------------------------------------


def start_emulator(
    *,
    avd: str,
    adb_path: str,
    config: dict[str, Any],
    run_dir: Path,
    port: int | None = None,
    lease_dir: Path | None = None,
) -> dict[str, Any]:
    """Boot `avd`, applying host workarounds, and return once ready.

    NOTE: the AVD-existence fast-fail check (avd.py's `require_avd_exists`)
    runs upstream of this function, in
    `AndroidInspectorTool._start_emulator()`, BEFORE this is ever called --
    not here. That keeps avd.py's dependency on this module one-directional
    (avd.py imports from here; this module does not import from avd.py) and
    means a missing AVD never reaches the ~60s device-appear timeout below.

    Cross-process AVD lease (see leases.py): before anything is launched,
    this acquires an exclusive lease on `avd` -- an AVD's QCOW2 disk state
    cannot be opened by two emulator processes at once, so a second
    `start_emulator` call for the same AVD, from a different (unaware)
    session, fails immediately and loudly, naming the owning pid, its port,
    and how long it's been held, rather than both instances silently
    corrupting each other. A lease held by a since-dead pid is stale and is
    reclaimed automatically. The lease is released again (so a retry can
    succeed) if ANYTHING below fails -- it is only left held once this
    function returns successfully.

    `port`, when given, pins the emulator's console port (`-port <port>`;
    adb derives the adb port as `port + 1`). It is validated (even, within
    adb's scan range -- see `validate_emulator_port`) BEFORE anything is
    launched, so an invalid port is never silently discarded or adjusted.
    The expected serial is then deterministic (`emulator-<port>`): if a
    device already answers to that serial before launch, this call refuses
    to proceed rather than risk adopting someone else's already-running
    instance, and waits on that exact serial afterward instead of the
    "any new serial" heuristic used when no port is given.

    When `port` is omitted, a free port is allocated atomically (as part of
    the same lease acquisition) from the lowest free even port in
    `PORT_RANGE_MIN`-`PORT_RANGE_MAX`, skipping ports already attached in
    `adb devices` and ports recorded on any OTHER currently-live lease --
    this closes the "two sessions each adopt whichever serial appears
    first" race. Only if every port in range is already taken does this
    fall back to the old "any new serial" heuristic; the result's
    `port_allocation_fallback` says so explicitly rather than silently
    reverting to non-deterministic behaviour.
    """
    if not avd:
        raise EmulatorError("start_emulator requires 'avd'.")

    emulator_binary = resolve_emulator_binary(config)
    before = snapshot_serials(adb_path)
    scope = ptrace_scope()

    caller_supplied_port = port
    attached_ports = {p for s in before if (p := parse_port_from_serial(s)) is not None}
    # If this raises AvdLeaseError, nothing has been acquired -- nothing to
    # release. Let it propagate as-is (owner pid/port/duration intact).
    #
    # `device_liveness_probe` lets acquire_avd_lease distinguish Orphaned
    # (owner pid dead, emulator still running) from Stale (owner pid dead,
    # emulator also gone) -- see leases.py module docstring. `before` is
    # the single point-in-time `adb devices` snapshot already taken above
    # (same one `attached_ports` derives from), so this costs no extra adb
    # call and stays consistent with the attached-ports check right below.
    lease = acquire_avd_lease(
        avd,
        port=caller_supplied_port,
        lease_dir=lease_dir,
        allocate_port_range=(PORT_RANGE_MIN, PORT_RANGE_MAX)
        if caller_supplied_port is None
        else None,
        attached_ports=frozenset(attached_ports),
        device_liveness_probe=lambda serial: serial in before,
    )

    port_allocation_fallback = (
        caller_supplied_port is None and lease.port_allocation_exhausted
    )
    port = caller_supplied_port if caller_supplied_port is not None else lease.port

    try:
        expected_serial: str | None = None
        if port is not None:
            validate_emulator_port(port)
            expected_serial = f"emulator-{port}"
            if expected_serial in before:
                raise EmulatorError(
                    f"Refusing to launch on port {port}: serial {expected_serial!r} "
                    "is already attached to another device/emulator. Adopting it "
                    "would mean operating on an instance this call did not start "
                    "-- choose a different port, or stop whatever is already "
                    "using this one. See docs/TROUBLESHOOTING.md, 'Wrong device "
                    "targeted'."
                )

        launched = launch_emulator_process(
            avd, emulator_binary, config, run_dir, port=port
        )

        try:
            if expected_serial is not None:
                serial = wait_for_specific_serial(
                    expected_serial,
                    list_serials_fn=lambda: snapshot_serials(adb_path),
                    timeout_s=float(
                        config.get(
                            "device_appear_timeout_s", DEFAULT_DEVICE_APPEAR_TIMEOUT_S
                        )
                    ),
                    process=launched.process,
                    log_path=launched.log_path,
                )
            else:
                serial = wait_for_new_serial(
                    before,
                    list_serials_fn=lambda: snapshot_serials(adb_path),
                    timeout_s=float(
                        config.get(
                            "device_appear_timeout_s", DEFAULT_DEVICE_APPEAR_TIMEOUT_S
                        )
                    ),
                    process=launched.process,
                    log_path=launched.log_path,
                )
            client = AdbClient(serial=serial, adb_path=adb_path)
            wait_boot_completed(
                client,
                timeout_s=float(config.get("boot_timeout_s", DEFAULT_BOOT_TIMEOUT_S)),
                process=launched.process,
                log_path=launched.log_path,
            )
            dismiss_keyguard(client)
        except EmulatorError as exc:
            # Process-death errors already name `launched.log_path` themselves
            # (see `_process_death_error`) -- only append the pointer for errors
            # that don't (plain timeouts), to avoid a redundant double mention.
            message = str(exc)
            if str(launched.log_path) not in message:
                message = f"{message} See emulator log: {launched.log_path}"
            raise EmulatorError(message) from exc

        record_lease_serial(avd, serial, lease_dir=lease_dir)
    except Exception:
        # Anything failed after the lease was acquired -- nothing is
        # actually running, so release it rather than leaving the AVD
        # falsely "leased" until this (still-alive) process exits.
        release_avd_lease(avd, expected_pid=os.getpid(), lease_dir=lease_dir)
        raise

    return {
        "serial": serial,
        "avd": avd,
        "pid": launched.pid,
        "port": port,
        "log_path": str(launched.log_path),
        "used_gdb_workaround": scope is not None and scope != 0,
        "port_allocation_fallback": port_allocation_fallback,
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _default_device_liveness_probe(client: AdbClientLike) -> Callable[[], bool]:
    """Build the default liveness probe for `stop_emulator`: `adb -s
    <serial> get-state`. Returns `True` iff the device still answers at
    all (any state -- 'device', 'offline', ...) -- exit code alone (a
    nonzero "device not found") is what actually distinguishes a gone
    emulator from a merely-not-yet-ready one. No new adb_path parameter is
    needed: `client` is already scoped to the exact serial being stopped.
    """

    def probe() -> bool:
        return client.run("get-state", check_output=False).ok

    return probe


def stop_emulator(
    client: AdbClientLike,
    *,
    pid: int | None = None,
    force: bool = False,
    lease_dir: Path | None = None,
    device_liveness_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Stop the emulator at `client.serial`, refusing unless a live lease
    for that serial is owned by the calling process (see leases.py).

    Ownership is resolved by serial -- not by any in-memory registry --
    because the whole point is protecting against a DIFFERENT process
    (which never shares this one's in-memory state) calling `stop_emulator`
    on a serial it never started. Four outcomes:

    1. The lease is ours (or none exists but the emulator was started by
       THIS pid some other way), OR the lease's owner pid is dead AND the
       emulator itself is also confirmed gone (Stale) -- proceeds normally,
       no force required. Killing something already gone is harmless.
    2. A DIFFERENT, currently live process holds the lease (Owned) --
       refuses with `AvdLeaseError` naming the owning pid, unless
       `force=True`, in which case it proceeds but the result carries a
       prominent `warning` naming whose emulator was just killed. Never
       silent either way.
    3. The lease's owner pid is dead, but the emulator itself is still
       running (Orphaned -- measured live: a session ends normally, its
       emulator keeps running, and a dead owner pid alone must never be
       read as "safe to kill"). Refuses just as loudly as case 2, naming
       the dead owner pid explicitly so `force=True` is an obviously safe,
       informed call -- there is no live session to disturb, only an
       unattended emulator.
    4. No lease record exists at all (predates lease tracking, started
       outside this tool, or physical device) -- ownership cannot be
       verified in EITHER direction, so this is treated the same as case 2:
       refuses unless `force=True` (with the same warning-on-force
       behaviour). This is the stricter, safer reading -- "no record" is
       not proof of "safe to kill".

    `device_liveness_probe`, if given, is a zero-arg callable returning
    `True` iff `client.serial` is currently alive -- used ONLY to
    distinguish case 1 (Stale) from case 3 (Orphaned) when the owner pid is
    dead. Defaults to `adb -s <serial> get-state` via `client` itself (see
    `_default_device_liveness_probe`) -- no extra adb_path plumbing needed.

    Once the kill actually proceeds (case 1, or 2/3 with `force=True`), the
    lease (if any) is released so the AVD becomes available again.
    """
    serial = client.serial
    lease = find_lease_by_serial(serial, lease_dir=lease_dir)
    our_pid = os.getpid()
    probe = device_liveness_probe or _default_device_liveness_probe(client)

    live_other_owner = False
    orphaned = False
    if lease is not None and lease.pid != our_pid:
        if is_pid_alive(lease.pid):
            live_other_owner = True
        else:
            # Owner pid is dead -- but an emulator commonly outlives the
            # session that started it (see leases.py module docstring).
            # Only a positive "the device is gone too" confirms Stale;
            # anything else (device still alive) is Orphaned, not free.
            orphaned = probe()
    unverifiable = lease is None

    warning: str | None = None
    if live_other_owner or orphaned or unverifiable:
        if not force:
            if live_other_owner:
                assert lease is not None  # narrows for the type checker
                raise AvdLeaseError(
                    f"Refusing to stop {serial!r}: it is leased by a DIFFERENT "
                    f"live process ({describe_lease(lease)}, AVD {lease.avd!r}). "
                    "Killing it would terminate someone else's emulator out from "
                    "under them. Pass 'force': true to override (the result will "
                    "carry a prominent warning naming whose emulator was killed), "
                    "or leave it alone -- each session should have its own AVD; "
                    "see 'create_avd'.",
                    serial=serial,
                    owner_pid=lease.pid,
                    avd=lease.avd,
                    port=lease.port,
                    held_for_s=lease.held_for_s(),
                    suggested_operation="create_avd",
                )
            if orphaned:
                assert lease is not None
                raise AvdLeaseError(
                    f"Refusing to stop {serial!r}: it is orphaned -- its owning "
                    f"session (pid {lease.pid}) is gone, but the emulator itself "
                    f"is still running ({describe_lease(lease)}, AVD "
                    f"{lease.avd!r}). An emulator commonly outlives the session "
                    "that started it, so a dead owner process does not by "
                    "itself mean this is safe to kill. Pass 'force': true if "
                    "you are sure -- there is no live owner session to "
                    "disturb, only an unattended emulator.",
                    serial=serial,
                    owner_pid=lease.pid,
                    avd=lease.avd,
                    port=lease.port,
                    held_for_s=lease.held_for_s(),
                    orphaned=True,
                    suggested_operation="create_avd",
                )
            raise AvdLeaseError(
                f"Refusing to stop {serial!r}: no lease record names this (or "
                "any other live) process as the owner -- it may predate this "
                "tool's lease tracking, belong to a device started outside it, "
                "or its recorded owner may already be gone. Cannot verify this "
                "is safe. Pass 'force': true if you are sure.",
                serial=serial,
            )
        if live_other_owner:
            assert lease is not None
            warning = (
                f"FORCED stop of {serial!r}: leased by a DIFFERENT live process "
                f"({describe_lease(lease)}, AVD {lease.avd!r}) -- NOT this "
                "session. That emulator has been killed out from under its owner."
            )
        elif orphaned:
            assert lease is not None
            warning = (
                f"FORCED stop of {serial!r}: orphaned -- its owning session "
                f"(pid {lease.pid}) was already gone, AVD {lease.avd!r}. No "
                "live owner session was disturbed."
            )
        else:
            warning = (
                f"FORCED stop of {serial!r}: no lease record found -- ownership "
                "could not be verified in either direction."
            )

    result = client.emu_kill()

    reaped = False
    if pid:
        for _ in range(20):  # up to ~10s grace period for a clean exit
            if not _pid_alive(pid):
                reaped = True
                break
            time.sleep(0.5)
        if not reaped and _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(1.0)
                if _pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
                reaped = True
            except OSError:
                pass

    if lease is not None:
        release_avd_lease(lease.avd, lease_dir=lease_dir)

    out: dict[str, Any] = {"emu_kill_ok": result.ok, "process_reaped": reaped}
    if warning is not None:
        out["warning"] = warning
    return out
