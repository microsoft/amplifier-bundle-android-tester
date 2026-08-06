"""Serial-scoped adb wrapper.

The single hardest-won rule this module encodes: **every adb invocation that
targets a device carries `-s <serial>`**. There is no code path here that runs
a device-scoped adb command without an explicit serial — `AdbClient` requires
a serial at construction and every action method funnels through
`AdbClient.build_args()` / `AdbClient.run()`, which always prepend
`[adb_path, "-s", serial]`.

The only adb invocations that legitimately omit `-s` are ones that are not
device-scoped at all: `adb devices` (enumerating everything attached) and
`adb version` (verifying the binary during resolution). Those live as
module-level functions, not on `AdbClient`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AM_START_ERROR_MARKERS",
    "DEFAULT_LAUNCH_FOCUS_POLL_S",
    "DEFAULT_LAUNCH_FOCUS_TIMEOUT_S",
    "KEYCODE_NAMES",
    "AdbClient",
    "AdbClientLike",
    "AdbCommandResult",
    "AdbError",
    "LaunchError",
    "RawDevice",
    "launch_app",
    "list_raw_devices",
    "parse_resolve_activity_component",
    "resolve_adb_binary",
    "resolve_keycode",
    "resolve_target_serial",
]


class AdbError(RuntimeError):
    """Raised when adb cannot be resolved, or a device-scoped command fails."""


class LaunchError(AdbError):
    """Raised by `launch_app` when every launch mechanism failed, or the
    launched app's arrival was never confirmed within the timeout.

    Carries `attempts` -- the full record of every mechanism tried, its adb
    command, and its outcome -- plus whatever else was learned before giving
    up (`launch_mechanism`, `launched_component`, `current_focus`), so the
    caller sees exactly what was attempted rather than a single collapsed
    message. Never silently degrades to "probably worked".
    """

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra: dict[str, Any] = extra


@dataclass
class AdbCommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class RawDevice:
    """One row from `adb devices -l`, unfiltered."""

    serial: str
    state: str  # "device", "offline", "unauthorized", "no permissions", ...
    info: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "device"


@runtime_checkable
class AdbClientLike(Protocol):
    """Structural type for anything that can play AdbClient's role in ui.py /
    emulator.py — the concrete `AdbClient`, or a test double (e.g.
    `FakeAdbClient` in tests/conftest.py) implementing the same shape."""

    serial: str

    def shell(
        self,
        command: str | list[str],
        *,
        check_output: bool = ...,
        timeout: float | None = ...,
    ) -> AdbCommandResult: ...

    def run(
        self,
        *args: str,
        timeout: float | None = ...,
        check_output: bool = ...,
    ) -> AdbCommandResult: ...

    def wait_for_device(self, timeout: float | None = ...) -> AdbCommandResult: ...

    def emu_kill(self) -> AdbCommandResult: ...


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

_ARM64_REMEDIATION = (
    "adb binary at '{path}' failed to execute (Exec format error). On aarch64 hosts, "
    "the stock platform-tools/adb build is frequently x86_64-only and cannot run "
    "under emulation. Fix: place a working aarch64 adb binary at "
    "$ANDROID_HOME/platform-tools-arm64/adb (see "
    "https://developer.android.com/tools/releases/platform-tools for an aarch64 "
    "build), or set tool config 'adb_path' to point at one directly. This is a "
    "hard failure — there is no silent fallback to a broken binary."
)

_NOT_FOUND_REMEDIATION = (
    "Could not resolve a working adb binary. Probed, in order: config override "
    "('adb_path'), $ANDROID_HOME/platform-tools-arm64/adb, "
    "$ANDROID_HOME/platform-tools/adb, and 'adb' on PATH. Set ANDROID_HOME (or "
    "ANDROID_SDK_ROOT), or configure tool 'adb_path' explicitly."
)


def _candidate_paths(config: dict[str, Any] | None) -> list[str]:
    config = config or {}
    candidates: list[str] = []

    override = config.get("adb_path")
    if override:
        candidates.append(str(Path(str(override)).expanduser()))

    android_home = (
        config.get("android_home")
        or os.environ.get("ANDROID_HOME")
        or os.environ.get("ANDROID_SDK_ROOT")
    )
    if android_home:
        home = Path(str(android_home)).expanduser()
        candidates.append(str(home / "platform-tools-arm64" / "adb"))
        candidates.append(str(home / "platform-tools" / "adb"))

    path_adb = shutil.which("adb")
    if path_adb:
        candidates.append(path_adb)

    return candidates


def resolve_adb_binary(
    config: dict[str, Any] | None = None,
    *,
    verify: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Resolve the adb binary path, verifying it actually executes.

    Probe order: config['adb_path'] override, $ANDROID_HOME/platform-tools-arm64/adb,
    $ANDROID_HOME/platform-tools/adb, 'adb' on PATH.

    Each candidate is verified with `adb version`. A candidate that exists but
    fails with "Exec format error" is a loud, non-recoverable failure (the
    aarch64/x86_64 mismatch) — we never silently fall through to a worse
    candidate once we've seen that specific error; we still try remaining
    candidates, but if NONE succeed and at least one hit Exec format error,
    that is the error surfaced (it is almost always the actionable one).
    """
    runner = verify or (
        lambda argv: subprocess.run(
            argv, capture_output=True, text=True, timeout=10, check=False
        )
    )

    candidates = _candidate_paths(config)
    saw_exec_format_error: str | None = None

    for candidate in candidates:
        if not candidate:
            continue
        if candidate != "adb" and not Path(candidate).exists():
            continue
        try:
            proc = runner([candidate, "version"])
        except FileNotFoundError:
            continue
        except OSError as exc:
            if "Exec format error" in str(exc):
                saw_exec_format_error = candidate
            continue

        if proc.returncode == 0:
            return candidate

        combined = (proc.stdout or "") + (proc.stderr or "")
        if "Exec format error" in combined:
            saw_exec_format_error = candidate

    if saw_exec_format_error:
        raise AdbError(_ARM64_REMEDIATION.format(path=saw_exec_format_error))

    raise AdbError(_NOT_FOUND_REMEDIATION)


# ---------------------------------------------------------------------------
# Launch resolution
# ---------------------------------------------------------------------------

# `am start` happily exits 0 while printing a real error to stdout, e.g.
# "Error: Activity class {com.foo/.Bar} does not exist." Exit code alone is
# never proof of success.
#
# NOTE: "Warning: Activity not started, ..." is deliberately NOT an error
# marker. Measured against a live emulator, `am start` emits it on the two
# most common *successful* paths:
#   "Warning: Activity not started, its current task has been brought to the front"
#   "Warning: Activity not started, intent has been delivered to currently
#    running top-most instance."
# In both cases mCurrentFocus confirmed the app was foregrounded. Treating
# that string as failure makes every relaunch of an already-running app
# report a false negative.
#
# The real arbiter of "did it launch" is the mCurrentFocus arrival poll in
# launch_app() -- observed reality, not a parsed hint string.
AM_START_ERROR_MARKERS = ("Error:",)

DEFAULT_LAUNCH_FOCUS_TIMEOUT_S = 10.0
DEFAULT_LAUNCH_FOCUS_POLL_S = 0.5


def parse_resolve_activity_component(stdout: str) -> str | None:
    """Parse `cmd package resolve-activity --brief` output.

    The command's own `--brief` output is one or more diagnostic lines
    followed by a final `pkg/activity` line -- the last non-empty line is
    the resolved component. Returns `None` if there's no output, or the last
    non-empty line doesn't look like a component (no `/`) -- e.g. "No
    activity found to run" style failures.
    """
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    candidate = lines[-1]
    if "/" not in candidate:
        return None
    return candidate


def _package_of(component: str) -> str:
    """The package portion of a `pkg/activity` (or `pkg/.Activity`) component."""
    return component.split("/", 1)[0]


def _record_attempt(
    mechanism: str, detail: str, *, success: bool, error: str | None
) -> dict[str, Any]:
    return {
        "mechanism": mechanism,
        "detail": detail,
        "success": success,
        "error": error,
    }


def _wait_for_focus_containing(
    client: AdbClient, package: str, *, timeout_s: float, poll_s: float
) -> tuple[str, bool]:
    """Poll `dumpsys window | grep mCurrentFocus` until it names `package`,
    or `timeout_s` elapses. Returns `(last_observed_focus, confirmed)`.

    This confirms the launched app actually arrived on screen -- not merely
    that the launch command exited without error, which is exactly the
    "assumed it worked" gap a bare `am start`/`monkey` exit code leaves open.
    """
    deadline = time.monotonic() + timeout_s
    current = ""
    while True:
        result = client.shell("dumpsys window | grep mCurrentFocus")
        current = result.stdout.strip()
        if package and package in current:
            return current, True
        if time.monotonic() >= deadline:
            return current, False
        time.sleep(poll_s)


def launch_app(
    client: AdbClient,
    *,
    component: str | None = None,
    package: str | None = None,
    timeout_s: float = DEFAULT_LAUNCH_FOCUS_TIMEOUT_S,
    poll_s: float = DEFAULT_LAUNCH_FOCUS_POLL_S,
) -> dict[str, Any]:
    """Launch an app, most-deterministic mechanism first, with confirmed arrival.

    Resolution order:

    1. Explicit `component` (`pkg/activity`) -> `am start -n <component>`
       directly. No fallback -- an explicit component is the caller's exact
       intent; guessing something else on failure would contradict it.
    2. Otherwise, resolve `package`'s launcher activity via
       `cmd package resolve-activity --brief -c
       android.intent.category.LAUNCHER`, then `am start -n <resolved>`.
    3. If resolution yields nothing usable, or the resolved activity itself
       fails to start, fall back to
       `monkey -p <package> -c android.intent.category.LAUNCHER 1`.

    `am start` exiting 0 is not treated as proof of success (see
    `AdbClient.am_start`).

    After a mechanism reports success, polls `dumpsys window | grep
    mCurrentFocus` until it names the target package, up to `timeout_s`.
    Launching without confirming arrival is exactly the "assumed it worked"
    failure this exists to prevent.

    Returns a dict with `launch_mechanism`, `launched_component`, `attempts`
    (every mechanism tried, in order, with its command and outcome), and
    `current_focus`.

    Raises:
        AdbError: neither `component` nor `package` was given.
        LaunchError: every mechanism failed, or arrival was never confirmed.
            `.extra["attempts"]` names every mechanism tried with its command,
            exit code, and stderr (via the underlying `AdbError` message).
    """
    if not component and not package:
        raise AdbError("launch_app requires 'component' or 'package'.")

    attempts: list[dict[str, Any]] = []
    launched_component: str | None = None
    launch_mechanism: str | None = None

    if component:
        detail = f"am start -n {component}"
        try:
            client.am_start(component)
        except AdbError as exc:
            attempts.append(
                _record_attempt(
                    "explicit_component", detail, success=False, error=str(exc)
                )
            )
        else:
            attempts.append(
                _record_attempt("explicit_component", detail, success=True, error=None)
            )
            launched_component = component
            launch_mechanism = "explicit_component"
    else:
        assert package is not None  # guaranteed by the guard above
        resolve_detail = (
            "cmd package resolve-activity --brief -c "
            f"android.intent.category.LAUNCHER {package}"
        )
        resolved = client.resolve_launcher_component(package)
        if not resolved:
            attempts.append(
                _record_attempt(
                    "resolve_activity",
                    resolve_detail,
                    success=False,
                    error="resolve-activity produced no usable pkg/activity component",
                )
            )
        else:
            start_detail = f"am start -n {resolved}"
            try:
                client.am_start(resolved)
            except AdbError as exc:
                attempts.append(
                    _record_attempt(
                        "resolve_activity", start_detail, success=False, error=str(exc)
                    )
                )
            else:
                attempts.append(
                    _record_attempt(
                        "resolve_activity", start_detail, success=True, error=None
                    )
                )
                launched_component = resolved
                launch_mechanism = "resolve_activity"

        if launch_mechanism is None:
            monkey_detail = f"monkey -p {package} -c android.intent.category.LAUNCHER 1"
            try:
                client.monkey_launch(package)
            except AdbError as exc:
                attempts.append(
                    _record_attempt(
                        "monkey", monkey_detail, success=False, error=str(exc)
                    )
                )
            else:
                attempts.append(
                    _record_attempt("monkey", monkey_detail, success=True, error=None)
                )
                launch_mechanism = "monkey"

    if launch_mechanism is None:
        raise LaunchError(
            f"Unable to launch {(component or package)!r}: every launch mechanism "
            "failed. See 'attempts' for the exit code/stderr of each.",
            attempts=attempts,
        )

    target_package = package or _package_of(component)  # type: ignore[arg-type]
    focus, confirmed = _wait_for_focus_containing(
        client, target_package, timeout_s=timeout_s, poll_s=poll_s
    )

    if not confirmed:
        raise LaunchError(
            f"Launch mechanism {launch_mechanism!r} reported success, but "
            f"mCurrentFocus never showed {target_package!r} within {timeout_s}s "
            f"-- arrival unconfirmed, not assumed. Last observed: {focus!r}",
            attempts=attempts,
            launch_mechanism=launch_mechanism,
            launched_component=launched_component,
            current_focus=focus,
        )

    return {
        "launch_mechanism": launch_mechanism,
        "launched_component": launched_component,
        "attempts": attempts,
        "current_focus": focus,
    }


# ---------------------------------------------------------------------------
# Keycodes
# ---------------------------------------------------------------------------

KEYCODE_NAMES: dict[str, int] = {
    "HOME": 3,
    "BACK": 4,
    "CALL": 5,
    "ENDCALL": 6,
    "DPAD_UP": 19,
    "DPAD_DOWN": 20,
    "DPAD_LEFT": 21,
    "DPAD_RIGHT": 22,
    "DPAD_CENTER": 23,
    "VOLUME_UP": 24,
    "VOLUME_DOWN": 25,
    "POWER": 26,
    "CAMERA": 27,
    "ENTER": 66,
    "DEL": 67,
    "TAB": 61,
    "SPACE": 62,
    "MENU": 82,
    "ESCAPE": 111,
    "FORWARD_DEL": 112,
    "MOVE_HOME": 122,
    "MOVE_END": 123,
    "APP_SWITCH": 187,
    "SLEEP": 223,
    "WAKEUP": 224,
}


def resolve_keycode(key: str | int) -> int:
    """Resolve a keyevent name (e.g. 'BACK', 'KEYCODE_BACK') or numeric code to an int."""
    if isinstance(key, int):
        return key
    text = str(key).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    upper = text.upper()
    upper = upper.removeprefix("KEYCODE_")
    if upper in KEYCODE_NAMES:
        return KEYCODE_NAMES[upper]
    raise AdbError(
        f"Unknown keycode name '{key}'. Known names: {sorted(KEYCODE_NAMES)}, "
        "or pass a numeric keycode."
    )


def _encode_input_text(text: str) -> str:
    """Encode text for `adb shell input text`.

    Android's `input text` splits on whitespace, so literal spaces must become
    '%s' (device-side space token) before the string is sent as a single argv
    element. The result is then shell-quoted so any on-device shell
    metacharacters in the remainder (quotes, `$`, `;`, backticks, ...) are not
    reinterpreted by the device's `/system/bin/sh -c`.
    """
    encoded = text.replace(" ", "%s")
    return shlex.quote(encoded)


# ---------------------------------------------------------------------------
# Non-device-scoped enumeration
# ---------------------------------------------------------------------------


def _default_text_runner(argv: list[str], timeout: float) -> AdbCommandResult:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        partial_stdout = exc.stdout
        if isinstance(partial_stdout, bytes):
            partial_stdout = partial_stdout.decode("utf-8", errors="replace")
        return AdbCommandResult(
            args=argv,
            returncode=-1,
            stdout=partial_stdout or "",
            stderr=f"adb command timed out after {timeout}s: {' '.join(argv)}",
        )
    return AdbCommandResult(
        args=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


def list_raw_devices(
    adb_path: str,
    *,
    runner: Callable[[list[str], float], AdbCommandResult] | None = None,
    timeout: float = 15.0,
) -> list[RawDevice]:
    """Run `adb devices -l` (no serial — this is the one legitimate exception,
    since the whole point is enumerating every attached device) and parse it.
    """
    active_runner = runner or _default_text_runner
    argv = [adb_path, "devices", "-l"]
    result = active_runner(argv, timeout)
    if not result.ok:
        raise AdbError(f"adb devices failed: {result.stderr or result.stdout}")

    devices: list[RawDevice] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        info = parts[2] if len(parts) > 2 else ""
        devices.append(RawDevice(serial=serial, state=state, info=info))
    return devices


def resolve_target_serial(
    explicit_serial: str | None,
    adb_path: str,
    *,
    default_serial: str | None = None,
    runner: Callable[[list[str], float], AdbCommandResult] | None = None,
) -> str:
    """Resolve which device serial an operation should target.

    Ambiguity (more than one ready device, no explicit serial) is a hard
    error demanding disambiguation — never a silent first-match. Offline and
    unauthorized devices are reported distinctly and never silently treated
    as usable.
    """
    if explicit_serial:
        return explicit_serial
    if default_serial:
        return default_serial

    devices = list_raw_devices(adb_path, runner=runner)
    ready = [d for d in devices if d.ready]
    not_ready = [d for d in devices if not d.ready]

    if len(ready) > 1:
        detail = ", ".join(f"{d.serial} ({d.state})" for d in ready)
        raise AdbError(
            f"Ambiguous: {len(ready)} ready devices attached with no explicit serial: "
            f"{detail}. Pass an explicit 'serial' parameter."
        )
    if ready:
        return ready[0].serial

    if not_ready:
        detail = ", ".join(f"{d.serial} ({d.state})" for d in not_ready)
        raise AdbError(f"No ready devices — found only offline/unauthorized: {detail}.")

    raise AdbError("No devices attached. Run start_emulator, or connect a device.")


# ---------------------------------------------------------------------------
# AdbClient — every method below funnels through build_args()/run(), which
# always prepend [adb_path, "-s", serial].
# ---------------------------------------------------------------------------


class AdbClient:
    """A device-scoped adb wrapper. Requires a serial at construction — there
    is no way to obtain an AdbClient that can issue a command without one."""

    def __init__(
        self,
        serial: str,
        adb_path: str,
        *,
        runner: Callable[[list[str], float], AdbCommandResult] | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not serial:
            raise AdbError(
                "AdbClient requires a non-empty serial — there is no code path "
                "that issues a device-scoped adb command without -s <serial>."
            )
        if not adb_path:
            raise AdbError("AdbClient requires a resolved adb_path.")
        self.serial = serial
        self.adb_path = adb_path
        self.timeout = timeout
        self._runner = runner or _default_text_runner

    # -- Core -----------------------------------------------------------

    def build_args(self, *args: str) -> list[str]:
        """Every device-scoped adb invocation is built here. `-s <serial>`
        is always present, immediately after the binary path."""
        return [self.adb_path, "-s", self.serial, *args]

    def run(
        self,
        *args: str,
        timeout: float | None = None,
        check_output: bool = False,
    ) -> AdbCommandResult:
        argv = self.build_args(*args)
        result = self._runner(argv, timeout or self.timeout)
        if check_output and not result.ok:
            raise AdbError(
                f"adb command failed ({result.returncode}): {' '.join(argv)}\n"
                f"stderr: {result.stderr.strip()}\nstdout: {result.stdout.strip()}"
            )
        return result

    def shell(
        self,
        command: str | list[str],
        *,
        check_output: bool = False,
        timeout: float | None = None,
    ) -> AdbCommandResult:
        if isinstance(command, (list, tuple)):
            return self.run(
                "shell", *command, check_output=check_output, timeout=timeout
            )
        return self.run("shell", command, check_output=check_output, timeout=timeout)

    # -- Device state -----------------------------------------------------

    def get_state(self) -> str:
        result = self.run("get-state")
        return result.stdout.strip() if result.ok else f"error:{result.stderr.strip()}"

    def wait_for_device(self, timeout: float | None = None) -> AdbCommandResult:
        return self.run("wait-for-device", check_output=True, timeout=timeout)

    def getprop(self, name: str) -> str:
        result = self.shell(f"getprop {name}", check_output=True)
        return result.stdout.strip()

    # -- App lifecycle ------------------------------------------------------

    def install(
        self,
        apk_path: str,
        *,
        reinstall: bool = True,
        grant: bool = True,
        timeout: float | None = None,
    ) -> AdbCommandResult:
        args = ["install"]
        if reinstall:
            args.append("-r")
        if grant:
            args.append("-g")
        args.append(apk_path)
        return self.run(*args, check_output=True, timeout=timeout)

    def uninstall(self, package: str) -> AdbCommandResult:
        return self.run("uninstall", package, check_output=True)

    def am_start(self, component: str) -> AdbCommandResult:
        """`am start -n <component>`.

        A nonzero exit raises `AdbError` (via `check_output=True`) as usual.
        But `am start` is also notorious for exiting 0 while printing an
        error to stdout (`Error: Activity class {...} does not exist.`,
        `Warning: Activity not started, ...`) -- exit code alone is NOT proof
        of success. stdout/stderr are checked for those markers and raise
        `AdbError` even on a zero exit.
        """
        result = self.run("shell", "am", "start", "-n", component, check_output=True)
        combined = f"{result.stdout}\n{result.stderr}"
        if any(marker in combined for marker in AM_START_ERROR_MARKERS):
            raise AdbError(
                f"am start -n {component} exited 0 but reported a failure "
                f"(exit code alone is not proof of success): {result.stdout.strip()}"
            )
        return result

    def monkey_launch(self, package: str) -> AdbCommandResult:
        return self.run(
            "shell",
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            check_output=True,
        )

    def resolve_launcher_component(self, package: str) -> str | None:
        """Resolve `package`'s launcher activity via
        `cmd package resolve-activity --brief -c
        android.intent.category.LAUNCHER <package>`.

        This is a probe, not a hard requirement -- returns `None` (never
        raises) if the command fails or yields nothing usable, so callers can
        fall back to another launch mechanism instead of hard-failing on a
        resolution step that was only ever "most deterministic if available".
        """
        result = self.shell(
            [
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-c",
                "android.intent.category.LAUNCHER",
                package,
            ],
            check_output=False,
        )
        if not result.ok:
            return None
        return parse_resolve_activity_component(result.stdout)

    def am_force_stop(self, package: str) -> AdbCommandResult:
        return self.run("shell", "am", "force-stop", package, check_output=True)

    # -- Input --------------------------------------------------------------

    def keyevent(self, key: str | int) -> AdbCommandResult:
        code = resolve_keycode(key)
        return self.run("shell", "input", "keyevent", str(code), check_output=True)

    def input_tap(self, x: int, y: int) -> AdbCommandResult:
        return self.run("shell", "input", "tap", str(x), str(y), check_output=True)

    def input_text(self, text: str) -> AdbCommandResult:
        encoded = _encode_input_text(text)
        return self.run("shell", "input", "text", encoded, check_output=True)

    def input_swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300
    ) -> AdbCommandResult:
        return self.run(
            "shell",
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
            check_output=True,
        )

    # -- Logs / files ---------------------------------------------------------

    def logcat_dump(
        self, *, lines: int = 200, filter_spec: str | None = None
    ) -> AdbCommandResult:
        args = ["logcat", "-d", "-t", str(lines)]
        if filter_spec:
            args.extend(filter_spec.split())
        return self.run(*args, check_output=True)

    def pull(self, remote: str, local: str) -> AdbCommandResult:
        return self.run("pull", remote, local, check_output=True)

    def push(self, local: str, remote: str) -> AdbCommandResult:
        return self.run("push", local, remote, check_output=True)

    # -- Emulator control (still device-scoped: `adb -s <serial> emu kill`) --

    def emu_kill(self) -> AdbCommandResult:
        return self.run("emu", "kill")

    # -- Binary capture (screenshots) ----------------------------------------

    def screencap_bytes(self, timeout: float | None = None) -> bytes:
        """`exec-out screencap -p`, captured as raw bytes (never text-decoded,
        which would corrupt the PNG)."""
        argv = self.build_args("exec-out", "screencap", "-p")
        effective_timeout = timeout or self.timeout
        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=effective_timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"screencap timed out after {effective_timeout}s") from exc
        if proc.returncode != 0:
            raise AdbError(
                f"screencap failed: {proc.stderr.decode('utf-8', errors='replace')}"
            )
        return proc.stdout
