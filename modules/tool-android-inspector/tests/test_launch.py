"""Unit tests for `launch_app` (Defect 1) — the resolution order, per the
design doc: explicit component -> resolve-activity -> monkey fallback, with
`am start` exit-0-but-error detection and mCurrentFocus arrival confirmation.

All scenarios use `AdbClient` with an injected `RecordingRunner` keyed by
subcommand argv, exactly like `test_adb.py` — no real adb/device dependency.
"""

from __future__ import annotations

import pytest
from amplifier_module_tool_android_inspector.adb import (
    AdbClient,
    AdbCommandResult,
    LaunchError,
    launch_app,
)

from .conftest import make_text_runner

ADB_PATH = "/opt/sdk/platform-tools/adb"
SERIAL = "emulator-5554"

_RESOLVE_ARGS = (
    "shell",
    "cmd",
    "package",
    "resolve-activity",
    "--brief",
    "-c",
    "android.intent.category.LAUNCHER",
    "com.android.settings",
)
_MONKEY_ARGS = (
    "shell",
    "monkey",
    "-p",
    "com.android.settings",
    "-c",
    "android.intent.category.LAUNCHER",
    "1",
)
_DUMPSYS_ARGS = ("shell", "dumpsys window | grep mCurrentFocus")


def _am_start_args(component: str) -> tuple[str, ...]:
    return ("shell", "am", "start", "-n", component)


def _ok_result(stdout: str = "") -> AdbCommandResult:
    return AdbCommandResult(args=[], returncode=0, stdout=stdout, stderr="")


def _fail_result(returncode: int, stderr: str) -> AdbCommandResult:
    return AdbCommandResult(args=[], returncode=returncode, stdout="", stderr=stderr)


def _focused(package: str) -> AdbCommandResult:
    return _ok_result(
        f"  mCurrentFocus=Window{{abc123 u0 {package}/{package}.MainActivity}}"
    )


# ---------------------------------------------------------------------------
# 1. Explicit component path — most deterministic, no fallback
# ---------------------------------------------------------------------------


def test_launch_app_explicit_component_goes_straight_to_am_start() -> None:
    component = "com.android.settings/.Settings"
    runner = make_text_runner(
        responses={
            _am_start_args(component): _ok_result("Starting: Intent { ... }\n"),
            _DUMPSYS_ARGS: _focused("com.android.settings"),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    result = launch_app(client, component=component, timeout_s=1.0, poll_s=0.01)

    assert result["launch_mechanism"] == "explicit_component"
    assert result["launched_component"] == component
    assert result["attempts"] == [
        {
            "mechanism": "explicit_component",
            "detail": f"am start -n {component}",
            "success": True,
            "error": None,
        }
    ]
    assert "com.android.settings" in result["current_focus"]
    # No resolve-activity or monkey call was ever made.
    subcommands = {tuple(argv[3:]) for argv in runner.calls}
    assert not any(
        sc[:3] == ("cmd", "package", "resolve-activity") for sc in subcommands
    )
    assert not any(sc[:2] == ("monkey", "-p") for sc in subcommands)


def test_launch_app_explicit_component_failure_does_not_fall_back() -> None:
    """An explicit component is the caller's exact intent — if it fails,
    launch_app must not silently guess a different mechanism."""
    component = "com.foo/.Bar"
    runner = make_text_runner(
        responses={
            _am_start_args(component): _fail_result(1, "no devices/emulators found"),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    with pytest.raises(LaunchError) as excinfo:
        launch_app(client, component=component, timeout_s=1.0, poll_s=0.01)

    attempts = excinfo.value.extra["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["mechanism"] == "explicit_component"
    assert attempts[0]["success"] is False
    assert "no devices/emulators found" in attempts[0]["error"]


# ---------------------------------------------------------------------------
# 2. Resolve-activity path — package resolves to a component, am_start succeeds
# ---------------------------------------------------------------------------


def test_launch_app_resolve_activity_path_succeeds_without_monkey() -> None:
    resolved = "com.android.settings/com.android.settings.Settings"
    runner = make_text_runner(
        responses={
            _RESOLVE_ARGS: _ok_result(f"priority=0\n{resolved}\n"),
            _am_start_args(resolved): _ok_result("Starting: Intent { ... }\n"),
            _DUMPSYS_ARGS: _focused("com.android.settings"),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    result = launch_app(
        client, package="com.android.settings", timeout_s=1.0, poll_s=0.01
    )

    assert result["launch_mechanism"] == "resolve_activity"
    assert result["launched_component"] == resolved
    assert [a["mechanism"] for a in result["attempts"]] == ["resolve_activity"]
    assert result["attempts"][0]["success"] is True

    subcommands = {tuple(argv[3:]) for argv in runner.calls}
    assert not any(sc[:2] == ("monkey", "-p") for sc in subcommands)


# ---------------------------------------------------------------------------
# 3. Monkey fallback — resolution yields nothing usable
# ---------------------------------------------------------------------------


def test_launch_app_falls_back_to_monkey_when_resolution_yields_nothing() -> None:
    runner = make_text_runner(
        responses={
            _RESOLVE_ARGS: _ok_result("No activity found to run.\n"),
            _MONKEY_ARGS: _ok_result(""),
            _DUMPSYS_ARGS: _focused("com.android.settings"),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    result = launch_app(
        client, package="com.android.settings", timeout_s=1.0, poll_s=0.01
    )

    assert result["launch_mechanism"] == "monkey"
    assert result["launched_component"] is None
    mechanisms = [a["mechanism"] for a in result["attempts"]]
    assert mechanisms == ["resolve_activity", "monkey"]
    assert result["attempts"][0]["success"] is False
    assert result["attempts"][1]["success"] is True


def test_launch_app_falls_back_to_monkey_when_resolved_am_start_fails() -> None:
    """Resolution DOES yield a component, but am_start on it fails — this is
    still an "everything short of monkey didn't work" state, so monkey is
    tried as the last resort rather than giving up."""
    resolved = "com.android.settings/com.android.settings.Settings"
    runner = make_text_runner(
        responses={
            _RESOLVE_ARGS: _ok_result(f"priority=0\n{resolved}\n"),
            _am_start_args(resolved): _fail_result(
                255, "activity class does not exist"
            ),
            _MONKEY_ARGS: _ok_result(""),
            _DUMPSYS_ARGS: _focused("com.android.settings"),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    result = launch_app(
        client, package="com.android.settings", timeout_s=1.0, poll_s=0.01
    )

    assert result["launch_mechanism"] == "monkey"
    mechanisms = [a["mechanism"] for a in result["attempts"]]
    assert mechanisms == ["resolve_activity", "monkey"]
    assert result["attempts"][0]["success"] is False
    assert "activity class does not exist" in result["attempts"][0]["error"]


# ---------------------------------------------------------------------------
# 4. All-fail — structured error naming every mechanism tried
# ---------------------------------------------------------------------------


def test_launch_app_all_mechanisms_fail_raises_structured_error() -> None:
    """This is the exact live-reproduced Defect 1 scenario, but with the fix
    in place: monkey also fails (exit 251), and there is no resolve-activity
    result either — the error must name every mechanism tried with its exit
    code / stderr, not a single collapsed monkey failure."""
    runner = make_text_runner(
        responses={
            _RESOLVE_ARGS: _ok_result("No activity found to run.\n"),
            _MONKEY_ARGS: _fail_result(
                251,
                "** Monkey aborted due to error.\nType: monkey.SecurityException",
            ),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    with pytest.raises(LaunchError) as excinfo:
        launch_app(client, package="com.android.settings", timeout_s=1.0, poll_s=0.01)

    attempts = excinfo.value.extra["attempts"]
    mechanisms = [a["mechanism"] for a in attempts]
    assert mechanisms == ["resolve_activity", "monkey"]
    assert all(a["success"] is False for a in attempts)
    # The monkey attempt's error names the exit code and stderr.
    monkey_attempt = attempts[1]
    assert "251" in monkey_attempt["error"]
    assert "Monkey aborted" in monkey_attempt["error"]
    # No mechanism silently degraded to a "probably worked" result.
    assert "launch_mechanism" not in excinfo.value.extra


def test_launch_app_no_component_or_package_raises() -> None:
    runner = make_text_runner()
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    with pytest.raises(Exception, match="requires 'component' or 'package'"):
        launch_app(client)


# ---------------------------------------------------------------------------
# 5. am_start exit 0 but "Error:" in stdout — treated as failure
# ---------------------------------------------------------------------------


def test_launch_app_explicit_component_am_start_error_marker_treated_as_failure() -> (
    None
):
    """Reproduces the "exit code alone is not proof" requirement inside the
    full launch_app orchestration, not just the raw am_start unit test."""
    component = "com.foo/.Bar"
    runner = make_text_runner(
        responses={
            _am_start_args(component): _ok_result(
                "Starting: Intent { ... }\n"
                "Error: Activity class {com.foo/.Bar} does not exist.\n"
            ),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    with pytest.raises(LaunchError) as excinfo:
        launch_app(client, component=component, timeout_s=1.0, poll_s=0.01)

    attempts = excinfo.value.extra["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["mechanism"] == "explicit_component"
    assert attempts[0]["success"] is False
    assert "does not exist" in attempts[0]["error"]


def test_launch_app_resolve_path_am_start_error_marker_falls_back_to_monkey() -> None:
    resolved = "com.android.settings/com.android.settings.Settings"
    runner = make_text_runner(
        responses={
            _RESOLVE_ARGS: _ok_result(f"priority=0\n{resolved}\n"),
            _am_start_args(resolved): _ok_result(
                "Starting: Intent { ... }\nError type 3\nError: Activity class does not exist.\n"
            ),
            _MONKEY_ARGS: _ok_result(""),
            _DUMPSYS_ARGS: _focused("com.android.settings"),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    result = launch_app(
        client, package="com.android.settings", timeout_s=1.0, poll_s=0.01
    )

    assert result["launch_mechanism"] == "monkey"
    assert result["attempts"][0]["mechanism"] == "resolve_activity"
    assert result["attempts"][0]["success"] is False
    assert "does not exist" in result["attempts"][0]["error"]


# ---------------------------------------------------------------------------
# 6. Arrival confirmation — never report success on exit code alone
# ---------------------------------------------------------------------------


def test_launch_app_never_confirming_arrival_raises_with_evidence() -> None:
    component = "com.foo/.Bar"
    runner = make_text_runner(
        responses={
            _am_start_args(component): _ok_result("Starting: Intent { ... }\n"),
            _DUMPSYS_ARGS: _ok_result(
                "  mCurrentFocus=Window{abc123 u0 com.android.launcher3/.Launcher}"
            ),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    with pytest.raises(LaunchError) as excinfo:
        launch_app(client, component=component, timeout_s=0.05, poll_s=0.01)

    extra = excinfo.value.extra
    assert extra["launch_mechanism"] == "explicit_component"
    assert "com.android.launcher3" in extra["current_focus"]
    assert extra["attempts"][0]["success"] is True  # am_start itself did succeed


def test_launch_app_confirms_arrival_and_returns_evidence() -> None:
    component = "com.android.settings/.Settings"
    runner = make_text_runner(
        responses={
            _am_start_args(component): _ok_result("Starting: Intent { ... }\n"),
            _DUMPSYS_ARGS: _focused("com.android.settings"),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    result = launch_app(client, component=component, timeout_s=1.0, poll_s=0.01)
    assert "com.android.settings" in result["current_focus"]
