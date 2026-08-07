"""Unit tests for adb.py.

Hard requirement under test: every single adb invocation that targets a
device MUST include `-s <serial>`. These tests assert that directly, for
every AdbClient action method, plus binary resolution, device listing, and
serial-resolution ambiguity handling.
"""

from __future__ import annotations

import subprocess

import pytest
from amplifier_module_tool_android_inspector.adb import (
    AdbClient,
    AdbCommandResult,
    AdbError,
    list_raw_devices,
    parse_resolve_activity_component,
    resolve_adb_binary,
    resolve_keycode,
    resolve_target_serial,
)

from .conftest import make_text_runner

ADB_PATH = "/opt/sdk/platform-tools/adb"
SERIAL = "emulator-5554"


# ---------------------------------------------------------------------------
# build_args — the single source of truth for "-s <serial>" placement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra_args",
    [
        (),
        ("shell", "input", "tap", "1", "2"),
        ("install", "-r", "-g", "/tmp/app.apk"),
        ("exec-out", "screencap", "-p"),
        ("devices",),
        ("get-state",),
        ("emu", "kill"),
    ],
)
def test_build_args_always_includes_dash_s_serial(extra_args: tuple[str, ...]) -> None:
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH)
    argv = client.build_args(*extra_args)
    assert argv[0] == ADB_PATH
    assert argv[1] == "-s"
    assert argv[2] == SERIAL
    assert argv[3:] == list(extra_args)


def test_adb_client_requires_nonempty_serial() -> None:
    with pytest.raises(AdbError):
        AdbClient(serial="", adb_path=ADB_PATH)


def test_adb_client_requires_adb_path() -> None:
    with pytest.raises(AdbError):
        AdbClient(serial=SERIAL, adb_path="")


@pytest.mark.parametrize(
    "method_name,call_args,expected_subcommand_prefix",
    [
        ("get_state", (), ("get-state",)),
        ("wait_for_device", (), ("wait-for-device",)),
        ("uninstall", ("com.foo",), ("uninstall", "com.foo")),
        (
            "am_start",
            ("com.foo/.MainActivity",),
            ("shell", "am", "start", "-n", "com.foo/.MainActivity"),
        ),
        (
            "monkey_launch",
            ("com.foo",),
            (
                "shell",
                "monkey",
                "-p",
                "com.foo",
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ),
        ),
        ("am_force_stop", ("com.foo",), ("shell", "am", "force-stop", "com.foo")),
        ("input_tap", (10, 20), ("shell", "input", "tap", "10", "20")),
        (
            "input_swipe",
            (10, 20, 30, 40),
            ("shell", "input", "swipe", "10", "20", "30", "40", "300"),
        ),
        (
            "pull",
            ("/sdcard/x.xml", "/tmp/x.xml"),
            ("pull", "/sdcard/x.xml", "/tmp/x.xml"),
        ),
        (
            "push",
            ("/tmp/x.txt", "/sdcard/x.txt"),
            ("push", "/tmp/x.txt", "/sdcard/x.txt"),
        ),
        ("emu_kill", (), ("emu", "kill")),
    ],
)
def test_every_action_method_includes_dash_s_serial(
    method_name: str, call_args: tuple, expected_subcommand_prefix: tuple[str, ...]
) -> None:
    runner = make_text_runner()
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    getattr(client, method_name)(*call_args)

    assert runner.calls, f"{method_name} did not invoke the runner at all"
    argv = runner.calls[-1]
    assert argv[0] == ADB_PATH
    assert argv[1] == "-s"
    assert argv[2] == SERIAL
    assert tuple(argv[3:]) == expected_subcommand_prefix


def test_install_default_flags() -> None:
    runner = make_text_runner()
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    client.install("/tmp/app.apk")
    argv = runner.calls[-1]
    assert argv[1:3] == ["-s", SERIAL]
    assert argv[3:] == ["install", "-r", "-g", "/tmp/app.apk"]


def test_keyevent_resolves_name_and_includes_serial() -> None:
    runner = make_text_runner()
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    client.keyevent("BACK")
    argv = runner.calls[-1]
    assert argv[1:3] == ["-s", SERIAL]
    assert argv[3:] == ["shell", "input", "keyevent", "4"]


def test_input_text_encodes_spaces_and_shell_quotes_and_includes_serial() -> None:
    runner = make_text_runner()
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    client.input_text("hello world")
    argv = runner.calls[-1]
    assert argv[1:3] == ["-s", SERIAL]
    assert argv[3:6] == ["shell", "input", "text"]
    # space -> %s, no literal space left in the encoded token
    assert " " not in argv[6].replace("'", "")
    assert "hello%sworld" in argv[6]


def test_logcat_dump_includes_serial_and_filter_spec() -> None:
    runner = make_text_runner()
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    client.logcat_dump(lines=50, filter_spec="MyTag:D *:S")
    argv = runner.calls[-1]
    assert argv[1:3] == ["-s", SERIAL]
    assert argv[3:] == ["logcat", "-d", "-t", "50", "MyTag:D", "*:S"]


# ---------------------------------------------------------------------------
# resolve_package_pids (Defect 2)
# ---------------------------------------------------------------------------


def test_resolve_package_pids_via_pidof_single() -> None:
    runner = make_text_runner(
        responses={
            ("shell", "pidof com.foo"): AdbCommandResult(
                args=[], returncode=0, stdout="1234\n", stderr=""
            )
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    assert client.resolve_package_pids("com.foo") == ["1234"]


def test_resolve_package_pids_via_pidof_multiple_not_truncated() -> None:
    """Multi-process apps: pidof returns several pids -- ALL must come back,
    never silently truncated to the first."""
    runner = make_text_runner(
        responses={
            ("shell", "pidof com.foo"): AdbCommandResult(
                args=[], returncode=0, stdout="1234 5678 9012\n", stderr=""
            )
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    assert client.resolve_package_pids("com.foo") == ["1234", "5678", "9012"]


def test_resolve_package_pids_falls_back_to_ps_when_pidof_fails() -> None:
    ps_output = (
        "USER   PID  PPID VSZ RSS WCHAN ADDR S NAME\n"
        "u0_a123 4321 456  100 200 0     0    S com.foo\n"
        "u0_a999 999  456  100 200 0     0    S com.other\n"
    )
    runner = make_text_runner(
        responses={
            ("shell", "pidof com.foo"): AdbCommandResult(
                args=[], returncode=1, stdout="", stderr=""
            ),
            ("shell", "ps -A"): AdbCommandResult(
                args=[], returncode=0, stdout=ps_output, stderr=""
            ),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    assert client.resolve_package_pids("com.foo") == ["4321"]


def test_resolve_package_pids_falls_back_to_ps_when_pidof_empty() -> None:
    ps_output = (
        "USER PID PPID VSZ RSS WCHAN ADDR S NAME\nu0_a1 111 1 1 1 0 0 S com.foo\n"
    )
    runner = make_text_runner(
        responses={
            ("shell", "pidof com.foo"): AdbCommandResult(
                args=[], returncode=0, stdout="", stderr=""
            ),
            ("shell", "ps -A"): AdbCommandResult(
                args=[], returncode=0, stdout=ps_output, stderr=""
            ),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    assert client.resolve_package_pids("com.foo") == ["111"]


def test_resolve_package_pids_not_running_returns_empty_list() -> None:
    ps_output = (
        "USER PID PPID VSZ RSS WCHAN ADDR S NAME\nu0_a1 111 1 1 1 0 0 S com.other\n"
    )
    runner = make_text_runner(
        responses={
            ("shell", "pidof com.foo"): AdbCommandResult(
                args=[], returncode=1, stdout="", stderr=""
            ),
            ("shell", "ps -A"): AdbCommandResult(
                args=[], returncode=0, stdout=ps_output, stderr=""
            ),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    assert client.resolve_package_pids("com.foo") == []


def test_resolve_package_pids_ps_failure_returns_empty_list() -> None:
    runner = make_text_runner(
        responses={
            ("shell", "pidof com.foo"): AdbCommandResult(
                args=[], returncode=1, stdout="", stderr=""
            ),
            ("shell", "ps -A"): AdbCommandResult(
                args=[], returncode=1, stdout="", stderr="permission denied"
            ),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    assert client.resolve_package_pids("com.foo") == []


def test_resolve_package_pids_unrecognized_ps_header_returns_empty_list() -> None:
    runner = make_text_runner(
        responses={
            ("shell", "pidof com.foo"): AdbCommandResult(
                args=[], returncode=1, stdout="", stderr=""
            ),
            ("shell", "ps -A"): AdbCommandResult(
                args=[], returncode=0, stdout="garbage header\nrow1 row2\n", stderr=""
            ),
        }
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    assert client.resolve_package_pids("com.foo") == []


# ---------------------------------------------------------------------------
# logcat_dump with pids -- composition, multi-pid, fallback (Defect 2)
# ---------------------------------------------------------------------------


def test_logcat_dump_single_pid_adds_pid_flag() -> None:
    runner = make_text_runner()
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    dump = client.logcat_dump(lines=100, pids=["1234"])
    argv = runner.calls[-1]
    assert argv[3:] == ["logcat", "-d", "-t", "100", "--pid", "1234"]
    assert dump.pids_requested == ["1234"]
    assert dump.pids_used == ["1234"]
    assert dump.pid_fallback_reason is None


def test_logcat_dump_pid_and_filter_spec_compose() -> None:
    runner = make_text_runner()
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    client.logcat_dump(lines=100, filter_spec="MyTag:D *:S", pids=["1234"])
    argv = runner.calls[-1]
    assert argv[3:] == [
        "logcat",
        "-d",
        "-t",
        "100",
        "--pid",
        "1234",
        "MyTag:D",
        "*:S",
    ]


def test_logcat_dump_multiple_pids_all_included_when_accepted() -> None:
    runner = make_text_runner(
        default=AdbCommandResult(args=[], returncode=0, stdout="log lines\n", stderr="")
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    dump = client.logcat_dump(lines=100, pids=["1234", "5678"])
    argv = runner.calls[-1]
    assert argv[3:] == ["logcat", "-d", "-t", "100", "--pid", "1234", "--pid", "5678"]
    assert dump.pids_used == ["1234", "5678"]
    assert dump.pid_fallback_reason is None
    assert dump.stdout == "log lines\n"


def test_logcat_dump_falls_back_to_first_pid_when_multi_pid_rejected() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> AdbCommandResult:
        calls.append(argv)
        if argv.count("--pid") > 1:
            return AdbCommandResult(
                args=argv, returncode=1, stdout="", stderr="unrecognized option --pid"
            )
        return AdbCommandResult(args=argv, returncode=0, stdout="ok\n", stderr="")

    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    dump = client.logcat_dump(lines=100, pids=["1234", "5678"])

    assert len(calls) == 2  # multi-pid attempt, then single-pid fallback
    assert calls[0][3:] == [
        "logcat",
        "-d",
        "-t",
        "100",
        "--pid",
        "1234",
        "--pid",
        "5678",
    ]
    assert calls[1][3:] == ["logcat", "-d", "-t", "100", "--pid", "1234"]
    assert dump.pids_requested == ["1234", "5678"]
    assert dump.pids_used == ["1234"]
    assert dump.pid_fallback_reason is not None
    assert "1234" in dump.pid_fallback_reason
    assert dump.stdout == "ok\n"


def test_logcat_dump_single_pid_failure_raises_no_fallback() -> None:
    runner = make_text_runner(
        default=AdbCommandResult(
            args=[], returncode=1, stdout="", stderr="no such process"
        )
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    with pytest.raises(AdbError, match="no such process"):
        client.logcat_dump(lines=100, pids=["1234"])


def test_shell_with_string_vs_list_command() -> None:
    runner = make_text_runner()
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)

    client.shell("getprop sys.boot_completed")
    assert runner.calls[-1][3:] == ["shell", "getprop sys.boot_completed"]

    client.shell(["input", "keyevent", "4"])
    assert runner.calls[-1][3:] == ["shell", "input", "keyevent", "4"]


# ---------------------------------------------------------------------------
# am_start — exit code alone is not proof of success (Defect 1)
# ---------------------------------------------------------------------------


def test_am_start_succeeds_when_no_error_marker() -> None:
    runner = make_text_runner(
        default=AdbCommandResult(
            args=[], returncode=0, stdout="Starting: Intent { ... }\n", stderr=""
        )
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    result = client.am_start("com.android.settings/.Settings")
    assert result.ok
    argv = runner.calls[-1]
    assert argv[3:] == ["shell", "am", "start", "-n", "com.android.settings/.Settings"]


def test_am_start_raises_on_nonzero_exit() -> None:
    runner = make_text_runner(
        default=AdbCommandResult(
            args=[], returncode=1, stdout="", stderr="no devices/emulators found"
        )
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    with pytest.raises(AdbError, match="no devices/emulators found"):
        client.am_start("com.foo/.Bar")


@pytest.mark.parametrize(
    "stdout",
    [
        "Starting: Intent { ... }\nError: Activity class {com.foo/.Bar} does not exist.\n",
        (
            "Starting: Intent { ... }\nError type 3\nError: Activity class {com.foo/.Bar} "
            "does not exist.\n"
        ),
    ],
)
def test_am_start_raises_when_exit_zero_but_error_marker_in_stdout(stdout: str) -> None:
    """The exact reproduced-live failure mode: `am start` exits 0 while
    printing an error. Exit code alone must never be treated as proof of
    success."""
    runner = make_text_runner(
        default=AdbCommandResult(args=[], returncode=0, stdout=stdout, stderr="")
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    with pytest.raises(AdbError):
        client.am_start("com.foo/.Bar")


# ---------------------------------------------------------------------------
# resolve_launcher_component / parse_resolve_activity_component (Defect 1)
# ---------------------------------------------------------------------------


def test_parse_resolve_activity_component_returns_last_nonempty_line() -> None:
    stdout = (
        "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n"
        "\n"
        "com.android.settings/com.android.settings.Settings\n"
    )
    assert (
        parse_resolve_activity_component(stdout)
        == "com.android.settings/com.android.settings.Settings"
    )


def test_parse_resolve_activity_component_none_when_no_slash() -> None:
    assert parse_resolve_activity_component("No activity found to run.\n") is None


def test_parse_resolve_activity_component_none_when_empty() -> None:
    assert parse_resolve_activity_component("") is None
    assert parse_resolve_activity_component("   \n  \n") is None


def test_resolve_launcher_component_uses_serial_and_returns_component() -> None:
    runner = make_text_runner(
        default=AdbCommandResult(
            args=[],
            returncode=0,
            stdout="priority=0\ncom.android.settings/com.android.settings.Settings\n",
            stderr="",
        )
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    resolved = client.resolve_launcher_component("com.android.settings")

    assert resolved == "com.android.settings/com.android.settings.Settings"
    argv = runner.calls[-1]
    assert argv[1:3] == ["-s", SERIAL]
    assert argv[3:] == [
        "shell",
        "cmd",
        "package",
        "resolve-activity",
        "--brief",
        "-c",
        "android.intent.category.LAUNCHER",
        "com.android.settings",
    ]


def test_resolve_launcher_component_returns_none_on_command_failure() -> None:
    runner = make_text_runner(
        default=AdbCommandResult(
            args=[], returncode=1, stdout="", stderr="no such package"
        )
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    assert client.resolve_launcher_component("com.nonexistent") is None


def test_resolve_launcher_component_returns_none_when_output_unusable() -> None:
    runner = make_text_runner(
        default=AdbCommandResult(
            args=[], returncode=0, stdout="No activity found to run.\n", stderr=""
        )
    )
    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=runner)
    assert client.resolve_launcher_component("com.foo") is None


def test_run_check_output_raises_on_failure() -> None:
    def failing_runner(argv, timeout):
        return AdbCommandResult(args=argv, returncode=1, stdout="", stderr="boom")

    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH, runner=failing_runner)
    with pytest.raises(AdbError, match="boom"):
        client.run("shell", "false", check_output=True)


def test_screencap_bytes_includes_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    class FakeCompleted:
        returncode = 0
        stdout = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        stderr = b""

    def fake_run(argv, capture_output, timeout, check):
        captured["argv"] = argv
        return FakeCompleted()

    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.adb.subprocess.run", fake_run
    )

    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH)
    data = client.screencap_bytes()

    assert data.startswith(b"\x89PNG")
    argv = captured["argv"]
    assert argv[0] == ADB_PATH
    assert argv[1] == "-s"
    assert argv[2] == SERIAL
    assert argv[3:] == ["exec-out", "screencap", "-p"]


def test_screencap_bytes_raises_on_nonzero_returncode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCompleted:
        returncode = 1
        stdout = b""
        stderr = b"no device"

    def fake_run(argv, capture_output, timeout, check):
        return FakeCompleted()

    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.adb.subprocess.run", fake_run
    )

    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH)
    with pytest.raises(AdbError, match="no device"):
        client.screencap_bytes()


def test_screencap_bytes_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, capture_output, timeout, check):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.adb.subprocess.run", fake_run
    )

    client = AdbClient(serial=SERIAL, adb_path=ADB_PATH)
    with pytest.raises(AdbError, match="timed out"):
        client.screencap_bytes()


# ---------------------------------------------------------------------------
# resolve_keycode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("BACK", 4),
        ("back", 4),
        ("KEYCODE_BACK", 4),
        (67, 67),
        ("123", 123),
    ],
)
def test_resolve_keycode(key, expected) -> None:
    assert resolve_keycode(key) == expected


def test_resolve_keycode_unknown_name_raises() -> None:
    with pytest.raises(AdbError):
        resolve_keycode("NOT_A_REAL_KEY")


# ---------------------------------------------------------------------------
# resolve_adb_binary
# ---------------------------------------------------------------------------


def test_resolve_adb_binary_config_override_success(tmp_path) -> None:
    adb_path = tmp_path / "adb"
    adb_path.write_text("#!/bin/sh\n", encoding="utf-8")

    def fake_verify(argv):
        assert argv == [str(adb_path), "version"]
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout="Android Debug Bridge 1.0.41", stderr=""
        )

    resolved = resolve_adb_binary({"adb_path": str(adb_path)}, verify=fake_verify)
    assert resolved == str(adb_path)


def test_resolve_adb_binary_falls_back_to_arm64_platform_tools(tmp_path) -> None:
    android_home = tmp_path / "sdk"
    arm64_adb = android_home / "platform-tools-arm64" / "adb"
    arm64_adb.parent.mkdir(parents=True)
    arm64_adb.write_text("#!/bin/sh\n", encoding="utf-8")

    def fake_verify(argv):
        if argv[0] == str(arm64_adb):
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout="ok", stderr=""
            )
        return subprocess.CompletedProcess(
            argv, returncode=1, stdout="", stderr="not found"
        )

    resolved = resolve_adb_binary(
        {"android_home": str(android_home)}, verify=fake_verify
    )
    assert resolved == str(arm64_adb)


def test_resolve_adb_binary_exec_format_error_is_loud_with_remediation(
    tmp_path,
) -> None:
    android_home = tmp_path / "sdk"
    x86_adb = android_home / "platform-tools" / "adb"
    x86_adb.parent.mkdir(parents=True)
    x86_adb.write_text("#!/bin/sh\n", encoding="utf-8")

    def fake_verify(argv):
        return subprocess.CompletedProcess(
            argv, returncode=1, stdout="", stderr="Exec format error"
        )

    with pytest.raises(AdbError, match="Exec format error"):
        resolve_adb_binary({"android_home": str(android_home)}, verify=fake_verify)


def test_resolve_adb_binary_nothing_found_raises_with_remediation() -> None:
    def fake_verify(argv):
        return subprocess.CompletedProcess(
            argv, returncode=1, stdout="", stderr="not found"
        )

    with pytest.raises(AdbError, match="Could not resolve"):
        resolve_adb_binary({}, verify=fake_verify)


# ---------------------------------------------------------------------------
# list_raw_devices — the one legitimate case with no -s <serial>
# ---------------------------------------------------------------------------


def test_list_raw_devices_parses_states() -> None:
    output = (
        "List of devices attached\n"
        "emulator-5554\tdevice product:sdk_gphone64_arm64 model:sdk_gphone64_arm64\n"
        "emulator-5556\toffline\n"
        "R3CN123\tunauthorized\n"
        "\n"
    )

    def runner(argv, timeout):
        assert argv == [ADB_PATH, "devices", "-l"]
        return AdbCommandResult(args=argv, returncode=0, stdout=output, stderr="")

    devices = list_raw_devices(ADB_PATH, runner=runner)
    assert [d.serial for d in devices] == ["emulator-5554", "emulator-5556", "R3CN123"]
    assert devices[0].state == "device"
    assert devices[0].ready is True
    assert devices[1].state == "offline"
    assert devices[1].ready is False
    assert devices[2].state == "unauthorized"
    assert devices[2].ready is False


def test_list_raw_devices_raises_on_command_failure() -> None:
    def runner(argv, timeout):
        return AdbCommandResult(
            args=argv, returncode=1, stdout="", stderr="adb server not running"
        )

    with pytest.raises(AdbError, match="adb server not running"):
        list_raw_devices(ADB_PATH, runner=runner)


# ---------------------------------------------------------------------------
# resolve_target_serial — ambiguity and offline/unauthorized handling
# ---------------------------------------------------------------------------


def test_resolve_target_serial_explicit_wins() -> None:
    assert resolve_target_serial("abc123", ADB_PATH) == "abc123"


def test_resolve_target_serial_default_config_used_when_no_explicit() -> None:
    assert (
        resolve_target_serial(None, ADB_PATH, default_serial="cfg-serial")
        == "cfg-serial"
    )


def test_resolve_target_serial_single_ready_device_auto_resolves() -> None:
    def runner(argv, timeout):
        return AdbCommandResult(
            args=argv,
            returncode=0,
            stdout="List of devices attached\nemulator-5554\tdevice\n",
            stderr="",
        )

    assert resolve_target_serial(None, ADB_PATH, runner=runner) == "emulator-5554"


def test_resolve_target_serial_ambiguous_raises() -> None:
    def runner(argv, timeout):
        return AdbCommandResult(
            args=argv,
            returncode=0,
            stdout="List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n",
            stderr="",
        )

    with pytest.raises(AdbError, match="Ambiguous"):
        resolve_target_serial(None, ADB_PATH, runner=runner)


def test_resolve_target_serial_only_offline_raises() -> None:
    def runner(argv, timeout):
        return AdbCommandResult(
            args=argv,
            returncode=0,
            stdout="List of devices attached\nemulator-5554\toffline\n",
            stderr="",
        )

    with pytest.raises(AdbError, match="offline"):
        resolve_target_serial(None, ADB_PATH, runner=runner)


def test_resolve_target_serial_no_devices_raises() -> None:
    def runner(argv, timeout):
        return AdbCommandResult(
            args=argv, returncode=0, stdout="List of devices attached\n", stderr=""
        )

    with pytest.raises(AdbError, match="No devices attached"):
        resolve_target_serial(None, ADB_PATH, runner=runner)
