"""Tool-level tests for Defect 2 (logcat package filtering) -- exercises
`AndroidInspectorTool._logcat` (and `execute()` for the error-path) against a
real `AdbClient` whose adb invocations are faked at the `subprocess.run`
boundary, dispatching by argv (same technique as `test_launch.py`'s
`RecordingRunner`, but at the process boundary like `test_screenshot.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from amplifier_module_tool_android_inspector import (
    AndroidInspectorState,
    AndroidInspectorTool,
)

ADB_PATH = "/opt/sdk/platform-tools/adb"
SERIAL = "emulator-5554"


def _fresh_state(tmp_path: Path) -> AndroidInspectorState:
    base_dir = tmp_path / "sessions"
    run_dir = base_dir / "_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return AndroidInspectorState(
        config={}, base_dir=base_dir, run_dir=run_dir, _adb_path=ADB_PATH
    )


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_dispatch(
    monkeypatch: pytest.MonkeyPatch, responses: dict
) -> list[list[str]]:
    """Fake `subprocess.run` dispatching on the subcommand (argv[3:], after
    [adb_path, '-s', serial]). `responses` keys are tuples of the subcommand
    argv; `default` (key `None`) is used for anything unmatched."""
    calls: list[list[str]] = []

    def fake_run(argv, capture_output, timeout, check, text=True):
        calls.append(list(argv))
        key = tuple(argv[3:])
        if key in responses:
            rc, out, err = responses[key]
            return _FakeCompleted(rc, out, err)
        default = responses.get(None, (0, "", ""))
        rc, out, err = default
        return _FakeCompleted(rc, out, err)

    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.adb.subprocess.run", fake_run
    )
    return calls


def test_logcat_without_package_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_dispatch(
        monkeypatch,
        {("logcat", "-d", "-t", "200"): (0, "line1\nline2\n", "")},
    )
    tool = AndroidInspectorTool()
    state = _fresh_state(tmp_path)
    result = tool._logcat(state, {"serial": SERIAL})
    assert result["success"] is True
    assert result["lines"] == ["line1", "line2"]
    assert "package" not in result
    assert calls[-1][3:] == ["logcat", "-d", "-t", "200"]


def test_logcat_with_package_resolves_pid_and_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_dispatch(
        monkeypatch,
        {
            ("shell", "pidof com.foo"): (0, "4321\n", ""),
            ("logcat", "-d", "-t", "200", "--pid", "4321"): (0, "app log line\n", ""),
        },
    )
    tool = AndroidInspectorTool()
    state = _fresh_state(tmp_path)
    result = tool._logcat(state, {"serial": SERIAL, "package": "com.foo"})

    assert result["success"] is True
    assert result["lines"] == ["app log line"]
    assert result["package"] == "com.foo"
    assert result["pids_requested"] == ["4321"]
    assert result["pids_used"] == ["4321"]
    assert "pid_fallback_reason" not in result

    logcat_call = next(c for c in calls if c[3:4] == ["logcat"])
    assert logcat_call[3:] == ["logcat", "-d", "-t", "200", "--pid", "4321"]


def test_logcat_package_and_filter_spec_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_dispatch(
        monkeypatch,
        {
            ("shell", "pidof com.foo"): (0, "4321\n", ""),
            (
                "logcat",
                "-d",
                "-t",
                "200",
                "--pid",
                "4321",
                "MyTag:D",
                "*:S",
            ): (0, "tagged line\n", ""),
        },
    )
    tool = AndroidInspectorTool()
    state = _fresh_state(tmp_path)
    result = tool._logcat(
        state, {"serial": SERIAL, "package": "com.foo", "filter_spec": "MyTag:D *:S"}
    )
    assert result["success"] is True
    assert result["lines"] == ["tagged line"]


def test_logcat_package_not_running_is_structured_error_not_empty_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_dispatch(
        monkeypatch,
        {
            ("shell", "pidof com.notrunning"): (1, "", ""),
            ("shell", "ps -A"): (
                0,
                "USER PID PPID VSZ RSS WCHAN ADDR S NAME\nu0_a1 1 1 1 1 0 0 S com.other\n",
                "",
            ),
        },
    )
    tool = AndroidInspectorTool()
    state = _fresh_state(tmp_path)
    result = tool._logcat(state, {"serial": SERIAL, "package": "com.notrunning"})

    assert result["success"] is False
    assert "com.notrunning" in result["error"]
    assert "not running" in result["error"]
    assert result["package"] == "com.notrunning"
    assert result["serial"] == SERIAL


def test_logcat_multi_pid_fallback_surfaced_through_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv, capture_output, timeout, check, text=True):
        sub = argv[3:]
        if sub == ["shell", "pidof com.multi"]:
            return _FakeCompleted(0, "111 222\n", "")
        if sub.count("--pid") > 1:
            return _FakeCompleted(1, "", "unrecognized option --pid")
        if sub[:1] == ["logcat"]:
            return _FakeCompleted(0, "single pid log\n", "")
        return _FakeCompleted(0, "", "")

    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.adb.subprocess.run", fake_run
    )

    tool = AndroidInspectorTool()
    state = _fresh_state(tmp_path)
    result = tool._logcat(state, {"serial": SERIAL, "package": "com.multi"})

    assert result["success"] is True
    assert result["pids_requested"] == ["111", "222"]
    assert result["pids_used"] == ["111"]
    assert "pid_fallback_reason" in result
    assert "111" in result["pid_fallback_reason"]
