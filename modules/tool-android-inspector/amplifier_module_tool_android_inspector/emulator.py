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
from pathlib import Path
from typing import Any

from .adb import AdbClient, AdbClientLike, list_raw_devices
from .evidence import unique_evidence_path

__all__ = [
    "DEFAULT_BOOT_TIMEOUT_S",
    "DEFAULT_DEVICE_APPEAR_TIMEOUT_S",
    "DEFAULT_EMULATOR_ARGS",
    "KEYCODE_MENU",
    "EmulatorError",
    "build_gdb_launch_script",
    "dismiss_keyguard",
    "launch_emulator_process",
    "ptrace_scope",
    "resolve_emulator_binary",
    "snapshot_serials",
    "start_emulator",
    "stop_emulator",
    "wait_boot_completed",
    "wait_for_new_serial",
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
) -> str:
    """Poll until a serial appears that was NOT in `before`. Only a genuinely
    new serial is accepted as ours — never "the first emulator- entry",
    which could belong to an already-running instance."""
    deadline = time.monotonic() + timeout_s
    while True:
        current = list_serials_fn()
        new = current - before
        if new:
            return min(new)
        if time.monotonic() >= deadline:
            raise EmulatorError(
                f"No new adb device serial appeared within {timeout_s}s."
            )
        time.sleep(poll_s)


# ---------------------------------------------------------------------------
# Process launch
# ---------------------------------------------------------------------------


def launch_emulator_process(
    avd: str,
    emulator_binary: str,
    config: dict[str, Any],
    run_dir: Path,
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    ptrace_scope_override: int | None | object = _AUTO,
) -> tuple[int, Path]:
    """Launch the emulator detached (survives the tool call returning), applying
    the gdb ptrace_scope workaround if the host needs it.

    Returns (pid, log_path).
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    # Collision-proof: a bare "emulator-{avd}.log" is silently truncated
    # (mode "wb") by every subsequent start_emulator call for the same AVD
    # name, destroying the previous boot's log. unique_evidence_path names
    # each boot's log distinctly instead.
    log_path = unique_evidence_path(run_dir, f"emulator-{avd}", "log")
    extra_args = list(config.get("emulator_args") or DEFAULT_EMULATOR_ARGS)

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

    return proc.pid, log_path


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


# ---------------------------------------------------------------------------
# Readiness (two-stage: wait-for-device THEN boot_completed THEN keyguard)
# ---------------------------------------------------------------------------


def wait_boot_completed(
    client: AdbClientLike,
    *,
    timeout_s: float = DEFAULT_BOOT_TIMEOUT_S,
    poll_s: float = BOOT_POLL_INTERVAL_S,
) -> None:
    client.wait_for_device(timeout=timeout_s)
    deadline = time.monotonic() + timeout_s
    while True:
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
) -> dict[str, Any]:
    if not avd:
        raise EmulatorError("start_emulator requires 'avd'.")

    emulator_binary = resolve_emulator_binary(config)
    before = snapshot_serials(adb_path)
    scope = ptrace_scope()

    pid, log_path = launch_emulator_process(avd, emulator_binary, config, run_dir)

    try:
        serial = wait_for_new_serial(
            before,
            list_serials_fn=lambda: snapshot_serials(adb_path),
            timeout_s=float(
                config.get("device_appear_timeout_s", DEFAULT_DEVICE_APPEAR_TIMEOUT_S)
            ),
        )
        client = AdbClient(serial=serial, adb_path=adb_path)
        wait_boot_completed(
            client,
            timeout_s=float(config.get("boot_timeout_s", DEFAULT_BOOT_TIMEOUT_S)),
        )
        dismiss_keyguard(client)
    except EmulatorError as exc:
        raise EmulatorError(f"{exc} See emulator log: {log_path}") from exc

    return {
        "serial": serial,
        "avd": avd,
        "pid": pid,
        "log_path": str(log_path),
        "used_gdb_workaround": scope is not None and scope != 0,
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_emulator(client: AdbClientLike, *, pid: int | None = None) -> dict[str, Any]:
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

    return {"emu_kill_ok": result.ok, "process_reaped": reaped}
