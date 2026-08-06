"""AVD (Android Virtual Device) discovery, host diagnostics, and provisioning.

Owns everything the emulator lifecycle needs to know about *before* a process
is ever spawned:

- **Fast-fail AVD existence check** (`require_avd_exists`). Measured live: a
  `start_emulator` call against a nonexistent AVD name, on an otherwise
  healthy SDK, silently ate the full 60s device-appear timeout before
  reporting failure. A missing AVD is detectable instantly from `<name>.ini`
  files under the AVD home -- there is no reason to ever pay that timeout for
  a typo.
- **`doctor`** -- a full, always-completing host readiness report (adb,
  emulator binary, KVM, ptrace_scope/gdb, AVDs, cmdline-tools). Encodes the
  prerequisite checklist that otherwise lives only as prose (decaying) in
  `agents/android-operator.md` -- structural, not a reminder.
- **`create_avd`** -- provisions a new AVD from an installed (or
  sdkmanager-installable) system image, never silently clobbering an
  existing AVD or accepting SDK licenses on the caller's behalf.

Every host-workaround rationale (aarch64 adb, missing linux-aarch64 emulator,
ptrace_scope/gdb) is documented in `docs/TROUBLESHOOTING.md` in the bundle
root -- `doctor`'s remediation text points there rather than duplicating it.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .adb import AdbCommandResult, AdbError, list_raw_devices, resolve_adb_binary
from .emulator import EmulatorError, ptrace_scope, resolve_emulator_binary

__all__ = [
    "DEFAULT_API_LEVEL",
    "DEFAULT_DEVICE",
    "DEFAULT_TAG",
    "AvdError",
    "check_adb_binary",
    "check_adb_server",
    "check_android_home",
    "check_avds_available",
    "check_cmdline_tools",
    "check_emulator_binary",
    "check_gdb_present",
    "check_host_platform",
    "check_kvm",
    "check_ptrace_scope",
    "create_avd",
    "detect_host_abi",
    "list_avd_names_from_disk",
    "list_avds_via_emulator",
    "require_avd_exists",
    "resolve_avd_home",
    "resolve_cmdline_tool",
    "run_doctor",
]


class AvdError(RuntimeError):
    """Raised for AVD discovery/provisioning failures.

    Carries `.extra` -- structured fields (existing AVDs, the exact
    remediation command, missing tool names, ...) that must reach the caller
    intact rather than being collapsed into a single opaque string. Mirrors
    `LaunchError` in adb.py.
    """

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra: dict[str, Any] = extra


DEFAULT_API_LEVEL = 35
DEFAULT_TAG = "google_apis"
DEFAULT_DEVICE = "pixel_6"

_KVM_PATH = Path("/dev/kvm")


# ---------------------------------------------------------------------------
# Host ABI detection -- detected, never hardcoded
# ---------------------------------------------------------------------------


def detect_host_abi() -> str:
    """`arm64-v8a` on aarch64 hosts, `x86_64` otherwise. Detected from
    `platform.machine()` -- never hardcoded."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64-v8a"
    return "x86_64"


# ---------------------------------------------------------------------------
# AVD home resolution + disk/emulator listing
# ---------------------------------------------------------------------------


def resolve_avd_home(config: dict[str, Any] | None = None) -> Path:
    """Resolve the AVD home directory.

    Order: config['avd_home'] override, $ANDROID_AVD_HOME,
    $ANDROID_SDK_HOME/.android/avd, ~/.android/avd.
    """
    config = config or {}
    override = config.get("avd_home")
    if override:
        return Path(str(override)).expanduser()

    env_avd_home = os.environ.get("ANDROID_AVD_HOME")
    if env_avd_home:
        return Path(env_avd_home).expanduser()

    sdk_home = os.environ.get("ANDROID_SDK_HOME")
    if sdk_home:
        return Path(sdk_home).expanduser() / ".android" / "avd"

    return Path("~/.android/avd").expanduser()


def list_avd_names_from_disk(avd_home: Path) -> list[str]:
    """Names of every `<name>.ini` under `avd_home`.

    An absent `avd_home` is a valid state (no AVDs provisioned yet) -- an
    empty list, not an error.
    """
    if not avd_home.exists():
        return []
    return sorted(p.stem for p in avd_home.glob("*.ini"))


def _default_subprocess_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)


def list_avds_via_emulator(
    emulator_binary: str | None,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[str] | None:
    """`emulator -list-avds`, for cross-checking the `<name>.ini` disk scan.

    A best-effort cross-check, not a hard requirement: returns `None`
    (never raises) if `emulator_binary` is falsy or fails to execute.
    """
    if not emulator_binary:
        return None
    active_runner = runner or _default_subprocess_runner
    try:
        proc = active_runner([emulator_binary, "-list-avds"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def require_avd_exists(
    avd: str,
    config: dict[str, Any] | None = None,
    *,
    emulator_binary: str | None = None,
    emulator_runner: Callable[[list[str]], subprocess.CompletedProcess[str]]
    | None = None,
) -> None:
    """Fast-fail check: does `avd` exist? Must be called BEFORE spawning any
    emulator process -- a missing AVD is detectable instantly and must never
    cost the ~60s device-appear timeout that a doomed `start_emulator` call
    would otherwise silently pay.

    Consults `<name>.ini` files under the AVD home, cross-checked with
    `emulator -list-avds` when `emulator_binary` is resolvable (best-effort;
    a `None` cross-check does not affect the verdict). `emulator_runner` is
    an injectable subprocess runner for that cross-check (testing only --
    production callers omit it and get the real subprocess).

    Raises:
        AvdError: if `avd` is not found. `.extra` carries `avd` (the name
            requested), `existing_avds` (every AVD that DOES exist, so a
            typo is instantly obvious), `remediation_command` (the exact
            `avdmanager create avd` invocation, ABI detected not hardcoded),
            and `create_avd_operation` (a pointer to the operation added for
            exactly this purpose).
    """
    home = resolve_avd_home(config)
    from_disk = list_avd_names_from_disk(home)
    from_emulator = list_avds_via_emulator(emulator_binary, runner=emulator_runner)

    existing = set(from_disk)
    if from_emulator is not None:
        existing |= set(from_emulator)

    if avd in existing:
        return

    abi = detect_host_abi()
    existing_sorted = sorted(existing)
    remediation = (
        f"avdmanager create avd -n {avd} -k "
        f'"system-images;android-{DEFAULT_API_LEVEL};{DEFAULT_TAG};{abi}" '
        f"-d {DEFAULT_DEVICE}"
    )
    cross_checked = " and 'emulator -list-avds'" if from_emulator is not None else ""
    raise AvdError(
        f"AVD {avd!r} does not exist (checked {home}{cross_checked}). "
        f"Existing AVDs: {existing_sorted or 'none'}. "
        f"Create it with: {remediation}  Or use the 'create_avd' operation.",
        avd=avd,
        existing_avds=existing_sorted,
        remediation_command=remediation,
        create_avd_operation="create_avd",
    )


# ---------------------------------------------------------------------------
# cmdline-tools resolution (sdkmanager / avdmanager)
# ---------------------------------------------------------------------------


def resolve_cmdline_tool(name: str, config: dict[str, Any] | None = None) -> str | None:
    """Resolve `sdkmanager`/`avdmanager` under
    `$ANDROID_HOME/cmdline-tools/latest/bin/`, then `cmdline-tools/bin/`,
    then PATH. Returns `None` (never raises) if unresolvable."""
    config = config or {}
    android_home = (
        config.get("android_home")
        or os.environ.get("ANDROID_HOME")
        or os.environ.get("ANDROID_SDK_ROOT")
    )
    candidates: list[str] = []
    if android_home:
        base = Path(str(android_home)).expanduser()
        candidates.append(str(base / "cmdline-tools" / "latest" / "bin" / name))
        candidates.append(str(base / "cmdline-tools" / "bin" / name))
    on_path = shutil.which(name)
    if on_path:
        candidates.append(on_path)

    for candidate in candidates:
        if candidate == on_path or Path(candidate).exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# doctor -- individual checks. Each returns {name, status, detail,
# remediation} and NEVER raises on its own (callers still wrap via `_safe`
# in run_doctor as a second line of defense).
# ---------------------------------------------------------------------------


def _check(
    name: str, status: str, detail: str, remediation: str | None
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "remediation": remediation,
    }


_NO_AARCH64_EMULATOR_REMEDIATION = (
    "Google ships no linux-aarch64 emulator build in the Android SDK repo. See "
    "docs/TROUBLESHOOTING.md ('No aarch64 emulator from Google') for a community "
    "build. This tool does not download or install anything automatically."
)


def check_host_platform() -> dict[str, Any]:
    """Informational: host OS/arch, and the ABI that follows from it."""
    system = platform.system()
    machine = platform.machine()
    abi = detect_host_abi()
    return _check(
        "host_platform", "ok", f"{system} {machine} (default AVD ABI: {abi}).", None
    )


def check_android_home(config: dict[str, Any]) -> dict[str, Any]:
    android_home = (
        config.get("android_home")
        or os.environ.get("ANDROID_HOME")
        or os.environ.get("ANDROID_SDK_ROOT")
    )
    if not android_home:
        return _check(
            "android_home",
            "fail",
            "Neither ANDROID_HOME nor ANDROID_SDK_ROOT is set, and no 'android_home' "
            "is configured.",
            "Set ANDROID_HOME (or ANDROID_SDK_ROOT) to your SDK root, or configure "
            "tool 'android_home'.",
        )
    path = Path(str(android_home)).expanduser()
    if not path.exists():
        return _check(
            "android_home",
            "fail",
            f"Resolved to {path}, but that directory does not exist.",
            f"Verify the SDK is installed at {path}, or correct ANDROID_HOME.",
        )
    return _check("android_home", "ok", f"Resolved to {path}.", None)


def check_adb_binary(
    config: dict[str, Any],
    *,
    verify: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    try:
        adb_path = resolve_adb_binary(config, verify=verify)
    except AdbError as exc:
        # resolve_adb_binary's own message IS the arm64-specific remediation
        # when it detected "Exec format error" -- never a generic fallback
        # message for that specific failure.
        message = str(exc)
        return _check("adb_binary", "fail", message, message)
    return _check("adb_binary", "ok", f"Resolved and verified at {adb_path}.", None)


def check_adb_server(
    adb_path: str | None,
    *,
    runner: Callable[[list[str], float], AdbCommandResult] | None = None,
) -> dict[str, Any]:
    if not adb_path:
        return _check(
            "adb_server",
            "fail",
            "adb binary not resolved; cannot check server reachability.",
            "Fix the 'adb_binary' check first.",
        )
    try:
        devices = list_raw_devices(adb_path, runner=runner)
    except AdbError as exc:
        return _check(
            "adb_server",
            "fail",
            str(exc),
            "Ensure the adb server can start: `adb kill-server && adb start-server`.",
        )
    if not devices:
        return _check(
            "adb_server", "ok", "adb server reachable; no devices attached.", None
        )
    problem = [d for d in devices if d.state in ("offline", "unauthorized")]
    detail = "; ".join(f"{d.serial} ({d.state})" for d in devices)
    if problem:
        flagged = ", ".join(f"{d.serial} ({d.state})" for d in problem)
        return _check(
            "adb_server",
            "warn",
            f"Devices: {detail}. Flagged as not usable: {flagged}.",
            "'unauthorized': accept the RSA key prompt on-device. 'offline': try "
            "`adb kill-server && adb start-server`, or restart the emulator/device.",
        )
    return _check("adb_server", "ok", f"Devices: {detail}.", None)


def check_emulator_binary(
    config: dict[str, Any],
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    system: str | None = None,
    abi: str | None = None,
) -> dict[str, Any]:
    effective_system = system if system is not None else platform.system()
    effective_abi = abi if abi is not None else detect_host_abi()
    is_aarch64_linux = effective_system == "Linux" and effective_abi == "arm64-v8a"

    try:
        binary = resolve_emulator_binary(config)
    except EmulatorError as exc:
        remediation = _NO_AARCH64_EMULATOR_REMEDIATION if is_aarch64_linux else str(exc)
        return _check("emulator_binary", "fail", str(exc), remediation)

    active_runner = runner or _default_subprocess_runner
    try:
        proc = active_runner([binary, "-version"])
    except OSError as exc:
        if "Exec format error" in str(exc):
            remediation = (
                _NO_AARCH64_EMULATOR_REMEDIATION
                if is_aarch64_linux
                else "Wrong-architecture emulator binary."
            )
            return _check(
                "emulator_binary",
                "fail",
                f"{binary} exists but failed to execute (Exec format error).",
                remediation,
            )
        return _check(
            "emulator_binary",
            "fail",
            f"{binary} exists but failed to execute: {exc}",
            str(exc),
        )

    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and "Exec format error" in combined:
        remediation = (
            _NO_AARCH64_EMULATOR_REMEDIATION
            if is_aarch64_linux
            else "Wrong-architecture emulator binary."
        )
        return _check(
            "emulator_binary",
            "fail",
            f"{binary} exists but failed to execute (Exec format error).",
            remediation,
        )
    if proc.returncode != 0:
        return _check(
            "emulator_binary",
            "warn",
            f"{binary} exists but `-version` exited {proc.returncode}: {combined.strip()}",
            None,
        )
    return _check("emulator_binary", "ok", f"Resolved and executes at {binary}.", None)


def check_kvm(
    path: Path = _KVM_PATH,
    *,
    system: str | None = None,
    access_fn: Callable[[Path, int], bool] | None = None,
) -> dict[str, Any]:
    effective_system = system if system is not None else platform.system()
    if effective_system != "Linux":
        return _check(
            "kvm",
            "ok",
            f"Not Linux ({effective_system}); KVM check not applicable.",
            None,
        )
    if not path.exists():
        return _check(
            "kvm",
            "fail",
            f"{path} does not exist -- no hardware acceleration available.",
            "Enable virtualization in BIOS/hypervisor settings and ensure the kvm "
            "kernel module is loaded.",
        )
    checker = access_fn or os.access
    readable = checker(path, os.R_OK)
    writable = checker(path, os.W_OK)
    if readable and writable:
        return _check("kvm", "ok", f"{path} exists and is readable+writable.", None)
    return _check(
        "kvm",
        "fail",
        f"{path} exists but is not accessible (readable={readable}, writable={writable}).",
        "Add your user to the 'kvm' group (`sudo usermod -aG kvm $USER`) and "
        "re-login (or `newgrp kvm`).",
    )


def check_ptrace_scope(
    scope: int | None, *, system: str | None = None
) -> dict[str, Any]:
    effective_system = system if system is not None else platform.system()
    if effective_system != "Linux":
        return _check(
            "ptrace_scope",
            "ok",
            f"Not Linux ({effective_system}); ptrace_scope check not applicable.",
            None,
        )
    if scope is None:
        return _check(
            "ptrace_scope",
            "ok",
            "kernel.yama.ptrace_scope not present (no YAMA restriction).",
            None,
        )
    if scope == 0:
        return _check(
            "ptrace_scope",
            "ok",
            "kernel.yama.ptrace_scope=0; no gdb wrapper needed.",
            None,
        )
    return _check(
        "ptrace_scope",
        "warn",
        f"kernel.yama.ptrace_scope={scope}. start_emulator automatically launches under "
        "a gdb wrapper (handle-all-signals-pass) to work around this -- no action needed "
        "unless gdb is missing (see 'gdb_present').",
        None,
    )


def check_gdb_present(
    scope: int | None, *, which_fn: Callable[[str], str | None] | None = None
) -> dict[str, Any]:
    finder = which_fn or shutil.which
    gdb_path = finder("gdb")
    needs_gdb = scope is not None and scope != 0
    if gdb_path:
        detail = f"gdb found at {gdb_path}."
        if needs_gdb:
            detail += " Required because ptrace_scope != 0."
        return _check("gdb_present", "ok", detail, None)
    if needs_gdb:
        return _check(
            "gdb_present",
            "fail",
            "gdb not found on PATH, but kernel.yama.ptrace_scope != 0 requires launching "
            "the emulator under gdb (its self-ptrace startup check fails otherwise).",
            "Install gdb (e.g. `apt install gdb`), or configure 'gdb_path'.",
        )
    return _check(
        "gdb_present",
        "ok",
        "gdb not found on PATH, but not required (ptrace_scope is 0 or absent).",
        None,
    )


def check_avds_available(
    config: dict[str, Any], *, emulator_binary: str | None = None
) -> dict[str, Any]:
    home = resolve_avd_home(config)
    from_disk = list_avd_names_from_disk(home)
    from_emulator = list_avds_via_emulator(emulator_binary)
    existing = sorted(set(from_disk) | set(from_emulator or []))
    if not existing:
        return _check(
            "avds_available",
            "warn",
            f"No AVDs found under {home}.",
            "Create one with the 'create_avd' operation, or `avdmanager create avd ...` directly.",
        )
    return _check("avds_available", "ok", f"AVDs found: {', '.join(existing)}.", None)


def check_cmdline_tools(config: dict[str, Any]) -> dict[str, Any]:
    sdkmanager = resolve_cmdline_tool("sdkmanager", config)
    avdmanager = resolve_cmdline_tool("avdmanager", config)
    missing = [
        n for n, p in (("sdkmanager", sdkmanager), ("avdmanager", avdmanager)) if not p
    ]
    if missing:
        return _check(
            "cmdline_tools",
            "warn",
            f"Missing: {', '.join(missing)}. Needed for the 'create_avd' operation.",
            "Install cmdline-tools via Android Studio's SDK Manager, or "
            '`sdkmanager --install "cmdline-tools;latest"`.',
        )
    return _check(
        "cmdline_tools",
        "ok",
        f"sdkmanager: {sdkmanager}; avdmanager: {avdmanager}.",
        None,
    )


def _run_doctor_impl(config: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def _safe(fn: Callable[[], dict[str, Any]], name: str) -> dict[str, Any]:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- doctor must never raise
            return _check(
                name,
                "fail",
                f"Check crashed: {exc}",
                "This is a bug in the check itself; report it.",
            )

    checks.append(_safe(check_host_platform, "host_platform"))
    checks.append(_safe(lambda: check_android_home(config), "android_home"))

    adb_check = _safe(lambda: check_adb_binary(config), "adb_binary")
    checks.append(adb_check)
    adb_path: str | None = None
    if adb_check["status"] == "ok":
        try:
            adb_path = resolve_adb_binary(config)
        except AdbError:
            adb_path = None
    checks.append(_safe(lambda: check_adb_server(adb_path), "adb_server"))

    emulator_check = _safe(lambda: check_emulator_binary(config), "emulator_binary")
    checks.append(emulator_check)
    emulator_binary: str | None = None
    if emulator_check["status"] == "ok":
        try:
            emulator_binary = resolve_emulator_binary(config)
        except EmulatorError:
            emulator_binary = None

    checks.append(_safe(check_kvm, "kvm"))

    system = platform.system()
    scope = ptrace_scope() if system == "Linux" else None
    checks.append(_safe(lambda: check_ptrace_scope(scope), "ptrace_scope"))
    checks.append(_safe(lambda: check_gdb_present(scope), "gdb_present"))

    checks.append(
        _safe(
            lambda: check_avds_available(config, emulator_binary=emulator_binary),
            "avds_available",
        )
    )
    checks.append(_safe(lambda: check_cmdline_tools(config), "cmdline_tools"))

    failing = [c for c in checks if c["status"] == "fail"]
    warning = [c for c in checks if c["status"] == "warn"]
    ready = not failing

    if failing:
        first = failing[0]
        summary = f"Fix first: {first['name']} -- {first['detail']}"
    elif warning:
        first = warning[0]
        summary = f"Ready, but review {first['name']}: {first['detail']}"
    else:
        summary = "All checks passed."

    return {"ready": ready, "checks": checks, "summary": summary}


def run_doctor(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run every host-readiness check and return a full report.

    Never raises, and never stops at the first failure -- the whole point is
    to show everything wrong at once. `ready` is true iff no check reported
    `fail`. `success` (the tool-envelope field) is always true for this
    operation: a machine with problems is a successful diagnosis, not a tool
    error.
    """
    config = config or {}
    try:
        return _run_doctor_impl(config)
    except Exception as exc:  # noqa: BLE001 -- absolute backstop; must never raise
        return {
            "ready": False,
            "checks": [
                _check(
                    "doctor",
                    "fail",
                    f"doctor crashed unexpectedly: {exc}",
                    "This is a bug in the doctor orchestration itself; report it.",
                )
            ],
            "summary": f"doctor crashed unexpectedly: {exc}",
        }


# ---------------------------------------------------------------------------
# create_avd
# ---------------------------------------------------------------------------


def _default_cmd_runner(
    argv: list[str], *, input_text: str | None = None, timeout: float = 600.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _require_cmdline_tools(config: dict[str, Any]) -> tuple[str, str]:
    sdkmanager = resolve_cmdline_tool("sdkmanager", config)
    avdmanager = resolve_cmdline_tool("avdmanager", config)
    missing = [
        n for n, p in (("sdkmanager", sdkmanager), ("avdmanager", avdmanager)) if not p
    ]
    if missing:
        raise AvdError(
            f"Missing required cmdline-tools: {', '.join(missing)}. Probed "
            "$ANDROID_HOME/cmdline-tools/latest/bin/, cmdline-tools/bin/, and PATH. "
            "Install via Android Studio's SDK Manager, or `sdkmanager --install "
            '"cmdline-tools;latest"`.',
            missing_tools=missing,
        )
    assert sdkmanager is not None and avdmanager is not None
    return sdkmanager, avdmanager


def _check_avd_not_already_present(
    name: str, config: dict[str, Any], *, force: bool
) -> None:
    home = resolve_avd_home(config)
    existing = list_avd_names_from_disk(home)
    if name in existing and not force:
        raise AvdError(
            f"AVD {name!r} already exists under {home}. Pass 'force': true to "
            "overwrite, or choose a different name -- never silently clobbered.",
            existing_avds=existing,
        )


def _is_system_image_installed(
    sdkmanager: str,
    package: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> bool:
    result = runner([sdkmanager, "--list_installed"])
    if result.returncode != 0:
        raise AvdError(
            f"sdkmanager --list_installed failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return package in result.stdout


def create_avd(
    *,
    name: str,
    config: dict[str, Any],
    api_level: int = DEFAULT_API_LEVEL,
    tag: str = DEFAULT_TAG,
    abi: str | None = None,
    device: str = DEFAULT_DEVICE,
    accept_licenses: bool = False,
    force: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Provision a new AVD from an installed (or sdkmanager-installable)
    system image.

    Fails loud throughout -- no silent fallback to a different ABI or API
    level than requested, no silent license acceptance, no silent clobbering
    of an existing AVD, and no trusting a zero exit code without verifying
    the AVD is actually there afterward.

    Raises:
        AvdError: missing cmdline-tools, AVD already exists (without
            `force`), system image absent with `accept_licenses=False`,
            `sdkmanager`/`avdmanager` failure, or post-create verification
            failure. `.extra` carries structured context for each case.
    """
    if not name:
        raise AvdError("create_avd requires 'name'.")

    resolved_abi = abi or detect_host_abi()
    active_runner = runner or _default_cmd_runner

    sdkmanager, avdmanager = _require_cmdline_tools(config)
    _check_avd_not_already_present(name, config, force=force)

    image_package = f"system-images;android-{api_level};{tag};{resolved_abi}"
    installed = _is_system_image_installed(sdkmanager, image_package, active_runner)

    downloaded = False
    if not installed:
        if not accept_licenses:
            raise AvdError(
                f"System image {image_package!r} is not installed. Installing it may "
                "require accepting an SDK license; this tool will not accept licenses "
                "silently on your behalf. Re-run with 'accept_licenses': true to accept "
                "automatically, or run `sdkmanager --licenses` yourself first, then "
                "retry. Note: this download is large and can take several minutes.",
                image_package=image_package,
            )
        install_result = active_runner(
            [sdkmanager, image_package], input_text="y\n" * 32
        )
        if install_result.returncode != 0:
            raise AvdError(
                f"sdkmanager failed to install {image_package!r} (exit "
                f"{install_result.returncode}): "
                f"{(install_result.stderr or install_result.stdout).strip()}",
                image_package=image_package,
            )
        downloaded = True

    avdmanager_args = ["create", "avd", "-n", name, "-k", image_package, "-d", device]
    if force:
        avdmanager_args.append("--force")
    # `input_text="no\n"` answers the "Do you wish to create a custom hardware
    # profile? [no]" prompt -- avdmanager blocks on stdin without it.
    create_result = active_runner([avdmanager, *avdmanager_args], input_text="no\n")
    if create_result.returncode != 0:
        raise AvdError(
            f"avdmanager failed to create AVD {name!r} (exit {create_result.returncode}): "
            f"{(create_result.stderr or create_result.stdout).strip()}",
            name=name,
        )

    try:
        emulator_binary = resolve_emulator_binary(config)
    except EmulatorError:
        emulator_binary = None

    if emulator_binary:
        avds_after = list_avds_via_emulator(emulator_binary, runner=active_runner)
        verification_method = "emulator -list-avds"
        verified = avds_after is not None and name in avds_after
    else:
        avds_after = list_avd_names_from_disk(resolve_avd_home(config))
        verification_method = (
            "avd .ini files (emulator binary unavailable for -list-avds)"
        )
        verified = name in avds_after

    if not verified:
        raise AvdError(
            f"avdmanager reported success creating {name!r}, but post-create "
            f"verification ({verification_method}) did not find it afterward -- exit "
            "code alone is not proof of success.",
            name=name,
            verification_method=verification_method,
        )

    return {
        "name": name,
        "package": image_package,
        "abi": resolved_abi,
        "device": device,
        "downloaded": downloaded,
        "note": (
            "System image was downloaded (large, can take several minutes)."
            if downloaded
            else None
        ),
        "verification_method": verification_method,
        "verified": True,
    }
