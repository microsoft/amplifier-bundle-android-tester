"""Unit tests for emulator.py — host workaround detection, gdb script
generation, process launch argv construction, and serial discovery. No real
emulator or gdb is spawned; subprocess.Popen and serial listing are injected."""

from __future__ import annotations

import subprocess

import pytest
from amplifier_module_tool_android_inspector.adb import AdbCommandResult, AdbError
from amplifier_module_tool_android_inspector.emulator import (
    DEFAULT_EMULATOR_ARGS,
    PORT_RANGE_MAX,
    PORT_RANGE_MIN,
    EmulatorError,
    build_gdb_launch_script,
    dismiss_keyguard,
    launch_emulator_process,
    ptrace_scope,
    validate_emulator_port,
    wait_boot_completed,
    wait_for_new_serial,
    wait_for_specific_serial,
)

from .conftest import FakeAdbClient, FakeProcess

# ---------------------------------------------------------------------------
# ptrace_scope
# ---------------------------------------------------------------------------


def test_ptrace_scope_reads_value(tmp_path) -> None:
    path = tmp_path / "ptrace_scope"
    path.write_text("1\n", encoding="utf-8")
    assert ptrace_scope(path) == 1


def test_ptrace_scope_none_when_absent(tmp_path) -> None:
    path = tmp_path / "does_not_exist"
    assert ptrace_scope(path) is None


def test_ptrace_scope_zero_when_disabled(tmp_path) -> None:
    path = tmp_path / "ptrace_scope"
    path.write_text("0\n", encoding="utf-8")
    assert ptrace_scope(path) == 0


# ---------------------------------------------------------------------------
# gdb launch script — "handle all" is load-bearing
# ---------------------------------------------------------------------------


def test_build_gdb_launch_script_contains_handle_all() -> None:
    script = build_gdb_launch_script("my-avd", DEFAULT_EMULATOR_ARGS)
    assert "handle all nostop noprint pass" in script
    assert "set pagination off" in script
    assert "set confirm off" in script
    assert (
        "run -avd my-avd -no-window -no-snapshot -no-boot-anim -gpu swiftshader_indirect"
        in script
    )


def test_build_gdb_launch_script_does_not_only_silence_sigsegv() -> None:
    # Regression guard for the whack-a-mole bug: silencing only SIGSEGV lets a
    # perfectly healthy booting emulator get killed by its own internal
    # SIGUSR1/SIGUSR2/SIGCONT usage.
    script = build_gdb_launch_script("my-avd", [])
    assert "handle SIGSEGV" not in script
    assert "handle all" in script


# ---------------------------------------------------------------------------
# launch_emulator_process — argv construction, gdb workaround branch
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def test_launch_emulator_process_direct_when_ptrace_scope_zero(tmp_path) -> None:
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc(pid=4242)

    emulator_binary = str(tmp_path / "sdk" / "emulator" / "emulator")
    result = launch_emulator_process(
        "my-avd",
        emulator_binary,
        {},
        tmp_path / "run",
        popen_factory=fake_popen,
        ptrace_scope_override=0,
    )

    assert result.pid == 4242
    assert result.process.pid == 4242
    assert captured["argv"] == [
        emulator_binary,
        "-avd",
        "my-avd",
        *DEFAULT_EMULATOR_ARGS,
    ]
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert captured["kwargs"]["start_new_session"] is True
    assert result.log_path.parent == tmp_path / "run"


def test_launch_emulator_process_includes_port_in_direct_argv(tmp_path) -> None:
    """Defect 1: an explicit port must reach the emulator argv, never be
    silently discarded."""
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc(pid=1)

    launch_emulator_process(
        "my-avd",
        str(tmp_path / "emulator"),
        {},
        tmp_path / "run",
        port=5570,
        popen_factory=fake_popen,
        ptrace_scope_override=0,
    )
    assert captured["argv"][-2:] == ["-port", "5570"]


def test_launch_emulator_process_omits_port_args_when_not_given(tmp_path) -> None:
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc(pid=1)

    launch_emulator_process(
        "my-avd",
        str(tmp_path / "emulator"),
        {},
        tmp_path / "run",
        popen_factory=fake_popen,
        ptrace_scope_override=0,
    )
    assert "-port" not in captured["argv"]


def test_launch_emulator_process_direct_when_ptrace_scope_none(tmp_path) -> None:
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc(pid=1)

    launch_emulator_process(
        "my-avd",
        str(tmp_path / "emulator"),
        {},
        tmp_path / "run",
        popen_factory=fake_popen,
        ptrace_scope_override=None,
    )
    assert "gdb" not in captured["argv"][0]


def test_launch_emulator_process_uses_gdb_when_ptrace_scope_nonzero(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc(pid=99)

    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.emulator._which",
        lambda name: "/usr/bin/gdb",
    )

    emulator_binary = str(tmp_path / "sdk" / "emulator" / "emulator")
    run_dir = tmp_path / "run"
    result = launch_emulator_process(
        "my-avd",
        emulator_binary,
        {},
        run_dir,
        popen_factory=fake_popen,
        ptrace_scope_override=1,
    )

    assert result.pid == 99
    argv = captured["argv"]
    assert argv[0] == "/usr/bin/gdb"
    assert argv[1] == "-batch"
    assert argv[2] == "-x"
    script_path = argv[3]
    assert "gdb-launch-my-avd.txt" in script_path
    assert argv[4] == "./emulator"
    assert captured["cwd"] == str(tmp_path / "sdk" / "emulator")

    script_content = (run_dir / "gdb-launch-my-avd.txt").read_text(encoding="utf-8")
    assert "handle all nostop noprint pass" in script_content


def test_launch_emulator_process_includes_port_in_gdb_script(
    tmp_path, monkeypatch
) -> None:
    """Defect 1: port must reach argv in the gdb-wrapped branch too, since
    that's the branch taken whenever kernel.yama.ptrace_scope != 0."""
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.emulator._which",
        lambda name: "/usr/bin/gdb",
    )
    run_dir = tmp_path / "run"
    launch_emulator_process(
        "my-avd",
        str(tmp_path / "sdk" / "emulator" / "emulator"),
        {},
        run_dir,
        port=5570,
        popen_factory=lambda *a, **k: _FakeProc(pid=1),
        ptrace_scope_override=1,
    )
    script_content = (run_dir / "gdb-launch-my-avd.txt").read_text(encoding="utf-8")
    assert "-port 5570" in script_content


def test_launch_emulator_process_raises_when_gdb_needed_but_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.emulator._which", lambda name: None
    )

    with pytest.raises(EmulatorError, match="gdb"):
        launch_emulator_process(
            "my-avd",
            str(tmp_path / "emulator"),
            {},
            tmp_path / "run",
            popen_factory=lambda *a, **k: _FakeProc(pid=1),
            ptrace_scope_override=1,
        )


# ---------------------------------------------------------------------------
# validate_emulator_port — Defect 1: invalid ports rejected before launch
# ---------------------------------------------------------------------------


def test_validate_emulator_port_accepts_valid_ports() -> None:
    validate_emulator_port(PORT_RANGE_MIN)
    validate_emulator_port(PORT_RANGE_MAX)
    validate_emulator_port(5570)  # no raise


def test_validate_emulator_port_rejects_odd_port() -> None:
    with pytest.raises(EmulatorError, match="even"):
        validate_emulator_port(5555)


def test_validate_emulator_port_rejects_port_below_range() -> None:
    with pytest.raises(EmulatorError, match=f"{PORT_RANGE_MIN}-{PORT_RANGE_MAX}"):
        validate_emulator_port(PORT_RANGE_MIN - 2)


def test_validate_emulator_port_rejects_port_above_range() -> None:
    with pytest.raises(EmulatorError, match=f"{PORT_RANGE_MIN}-{PORT_RANGE_MAX}"):
        validate_emulator_port(PORT_RANGE_MAX + 2)


# ---------------------------------------------------------------------------
# wait_for_new_serial — only a genuinely NEW serial is accepted
# ---------------------------------------------------------------------------


def test_wait_for_new_serial_accepts_only_new_entries() -> None:
    calls = {"n": 0}
    sequence = [
        {"emulator-5554"},
        {"emulator-5554"},
        {"emulator-5554", "emulator-5556"},
    ]

    def list_serials_fn():
        idx = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return sequence[idx]

    serial = wait_for_new_serial(
        before={"emulator-5554"},
        list_serials_fn=list_serials_fn,
        timeout_s=1.0,
        poll_s=0.01,
    )
    assert serial == "emulator-5556"


def test_wait_for_new_serial_times_out() -> None:
    def list_serials_fn():
        return {"emulator-5554"}

    with pytest.raises(EmulatorError, match="No new adb device serial"):
        wait_for_new_serial(
            before={"emulator-5554"},
            list_serials_fn=list_serials_fn,
            timeout_s=0.05,
            poll_s=0.02,
        )


def test_wait_for_new_serial_stops_immediately_when_process_dies(tmp_path) -> None:
    """Defect 2, stage 1 (serial wait, no port): the emulator process dies
    almost immediately. Waiting must stop the moment that's detected --
    never run out a (deliberately huge) timeout -- and the error must carry
    the exit signal and a diagnostic excerpt from the log, not a bare
    symptom like 'adb timed out'. Elapsed behaviour is asserted via the
    injected poller's call count, not via real wall-clock timing."""
    log_path = tmp_path / "emulator.log"
    log_path.write_text(
        "Initializing...\n"
        "Program terminated with signal SIGSEGV, Segmentation fault.\n"
        "The program no longer exists.\n",
        encoding="utf-8",
    )
    process = FakeProcess(pid=4321, exit_after=2, exit_returncode=-11)
    poller_calls = {"n": 0}

    def list_serials_fn():
        poller_calls["n"] += 1
        return {"emulator-5554"}  # never a new serial -- would time out otherwise

    with pytest.raises(EmulatorError) as excinfo:
        wait_for_new_serial(
            before={"emulator-5554"},
            list_serials_fn=list_serials_fn,
            timeout_s=100.0,  # deliberately huge -- must not be reached
            poll_s=0.001,
            process=process,
            log_path=log_path,
        )

    message = str(excinfo.value)
    assert "SIGSEGV" in message
    assert "4321" in message
    assert str(log_path) in message
    # Stopped almost immediately -- nowhere near exhausting the huge timeout.
    assert poller_calls["n"] <= 2


# ---------------------------------------------------------------------------
# wait_for_specific_serial — used when an explicit port pins the serial
# ---------------------------------------------------------------------------


def test_wait_for_specific_serial_accepts_only_the_exact_serial() -> None:
    calls = {"n": 0}
    sequence = [
        {"emulator-5554"},  # some other, unrelated device
        {"emulator-5554", "emulator-5570"},
    ]

    def list_serials_fn():
        idx = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return sequence[idx]

    serial = wait_for_specific_serial(
        "emulator-5570",
        list_serials_fn=list_serials_fn,
        timeout_s=1.0,
        poll_s=0.01,
    )
    assert serial == "emulator-5570"


def test_wait_for_specific_serial_times_out_if_never_seen() -> None:
    def list_serials_fn():
        return {"emulator-5554"}  # present, but never the expected one

    with pytest.raises(EmulatorError, match="'emulator-5570' did not appear"):
        wait_for_specific_serial(
            "emulator-5570",
            list_serials_fn=list_serials_fn,
            timeout_s=0.05,
            poll_s=0.02,
        )


def test_wait_for_specific_serial_stops_immediately_when_process_dies(
    tmp_path,
) -> None:
    log_path = tmp_path / "emulator.log"
    log_path.write_text("Program terminated with signal SIGSEGV.\n", encoding="utf-8")
    process = FakeProcess(pid=777, exit_after=2, exit_returncode=-11)

    with pytest.raises(EmulatorError, match="SIGSEGV"):
        wait_for_specific_serial(
            "emulator-5570",
            list_serials_fn=lambda: set(),
            timeout_s=100.0,  # deliberately huge -- must not be reached
            poll_s=0.001,
            process=process,
            log_path=log_path,
        )


# ---------------------------------------------------------------------------
# Two-stage readiness: wait-for-device THEN boot_completed THEN keyguard
# ---------------------------------------------------------------------------


def test_wait_boot_completed_polls_until_booted() -> None:
    client = FakeAdbClient()
    responses = iter(["0", "0", "1"])

    def shell(command, *, check_output=False, timeout=None):
        return AdbCommandResult(
            args=["shell", str(command)],
            returncode=0,
            stdout=next(responses),
            stderr="",
        )

    client.shell = shell  # type: ignore[assignment]
    wait_boot_completed(client, timeout_s=1.0, poll_s=0.01)
    # wait_for_device (via client.run(...)) was invoked at least once
    assert any(c[:1] == ("wait-for-device",) for c in client.calls)


def test_wait_boot_completed_stops_immediately_when_process_dies_during_device_wait(
    tmp_path,
) -> None:
    """Defect 2, device-ready stage: `client.wait_for_device` never
    succeeds (simulating a device that never reports ready), and the
    emulator process dies partway through the retry loop. Must stop the
    moment that's detected -- never run out a (deliberately huge) timeout
    on a blocking-by-design `wait-for-device` call."""
    log_path = tmp_path / "emulator.log"
    log_path.write_text("Program terminated with signal SIGSEGV.\n", encoding="utf-8")

    client = FakeAdbClient()

    def wait_for_device(timeout=None):
        raise AdbError("adb command timed out")

    client.wait_for_device = wait_for_device  # type: ignore[assignment]
    process = FakeProcess(pid=222, exit_after=2, exit_returncode=-11)

    with pytest.raises(EmulatorError, match="SIGSEGV"):
        wait_boot_completed(
            client,
            timeout_s=100.0,  # deliberately huge -- must not be reached
            poll_s=0.001,
            process=process,
            log_path=log_path,
        )


def test_wait_boot_completed_stops_immediately_when_process_dies_during_boot_poll(
    tmp_path,
) -> None:
    """Defect 2, boot-completed poll stage: the device-ready stage succeeds
    (device attaches fine), but the process dies partway through the
    `sys.boot_completed` poll -- e.g. it crashed moments after connecting
    to adb. This is the exact live-reproduced scenario: previously the tool
    sat in this wait for the full 240s boot timeout and reported a bare
    'adb timed out' symptom instead of the cause. Must stop immediately
    with the exit signal and log excerpt."""
    log_path = tmp_path / "emulator.log"
    log_path.write_text(
        "Booting...\n"
        "Program terminated with signal SIGSEGV, Segmentation fault.\n"
        "The program no longer exists.\n",
        encoding="utf-8",
    )
    client = FakeAdbClient()  # default wait_for_device succeeds immediately

    def shell(command, *, check_output=False, timeout=None):
        return AdbCommandResult(args=["shell"], returncode=0, stdout="0", stderr="")

    client.shell = shell  # type: ignore[assignment]
    process = FakeProcess(pid=555, exit_after=2, exit_returncode=-11)

    with pytest.raises(EmulatorError) as excinfo:
        wait_boot_completed(
            client,
            timeout_s=100.0,  # deliberately huge -- must not be reached
            poll_s=0.001,
            process=process,
            log_path=log_path,
        )

    message = str(excinfo.value)
    assert "SIGSEGV" in message
    assert "555" in message
    assert str(log_path) in message


def test_wait_boot_completed_times_out() -> None:
    client = FakeAdbClient()

    def shell(command, *, check_output=False, timeout=None):
        return AdbCommandResult(args=["shell"], returncode=0, stdout="0", stderr="")

    client.shell = shell  # type: ignore[assignment]
    with pytest.raises(EmulatorError, match="boot did not complete"):
        wait_boot_completed(client, timeout_s=0.05, poll_s=0.02)


def test_dismiss_keyguard_sends_keyevent_82() -> None:
    client = FakeAdbClient()
    dismiss_keyguard(client)
    assert client.keyevent_calls() == ["82"]
