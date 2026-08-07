"""Unit tests for emulator.py — host workaround detection, gdb script
generation, process launch argv construction, and serial discovery. No real
emulator or gdb is spawned; subprocess.Popen and serial listing are injected."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from amplifier_module_tool_android_inspector.adb import AdbCommandResult, AdbError
from amplifier_module_tool_android_inspector.emulator import (
    DEFAULT_EMULATOR_ARGS,
    PORT_RANGE_MAX,
    PORT_RANGE_MIN,
    EmulatorError,
    LaunchedEmulatorProcess,
    build_gdb_launch_script,
    dismiss_keyguard,
    launch_emulator_process,
    ptrace_scope,
    start_emulator,
    stop_emulator,
    validate_emulator_port,
    wait_boot_completed,
    wait_for_new_serial,
    wait_for_specific_serial,
)
from amplifier_module_tool_android_inspector.leases import (
    AvdLease,
    AvdLeaseError,
    read_lease,
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


# ---------------------------------------------------------------------------
# start_emulator -- AVD lease acquisition/release (Defect 1) and atomic
# port allocation (Defect 2), at the full orchestration level.
# ---------------------------------------------------------------------------


def _happy_path_monkeypatches(monkeypatch, tmp_path, *, serials_seen=None):
    """Shared collaborator stubs for a start_emulator call that should
    succeed all the way through boot -- same technique as
    test_avd.py's `test_start_emulator_threads_port_through_and_waits_on_exact_serial`."""
    import amplifier_module_tool_android_inspector.emulator as emu_mod

    monkeypatch.setattr(
        emu_mod, "resolve_emulator_binary", lambda config: "/fake/emulator"
    )
    monkeypatch.setattr(emu_mod, "ptrace_scope", lambda *a, **k: 0)

    sequence = serials_seen if serials_seen is not None else [set()]
    calls = {"n": 0}

    def fake_snapshot_serials(adb_path):
        idx = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return sequence[idx]

    monkeypatch.setattr(emu_mod, "snapshot_serials", fake_snapshot_serials)

    def fake_launch_emulator_process(
        avd, emulator_binary, config, run_dir, *, port=None, **kw
    ):
        log_path = tmp_path / "fake-emulator.log"
        log_path.write_text("", encoding="utf-8")
        return LaunchedEmulatorProcess(
            pid=999, log_path=log_path, process=FakeProcess(pid=999)
        )

    monkeypatch.setattr(
        emu_mod, "launch_emulator_process", fake_launch_emulator_process
    )
    monkeypatch.setattr(emu_mod, "wait_boot_completed", lambda *a, **k: None)
    monkeypatch.setattr(emu_mod, "dismiss_keyguard", lambda client: None)
    return emu_mod


def test_start_emulator_refuses_when_avd_already_leased_by_live_process(
    tmp_path, monkeypatch
) -> None:
    """Defect 1: measured scenario -- a second `start_emulator` call for an
    AVD another live process already booted must fail immediately and
    loudly, WITHOUT ever launching a second emulator process."""
    emu_mod = _happy_path_monkeypatches(monkeypatch, tmp_path)

    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    other = AvdLease(
        avd="concur-probe",
        pid=999999,
        port=5554,
        serial="emulator-5554",
        started_at=time.time() - 5.0,
    )
    (lease_dir / "concur-probe.lease").write_text(
        json.dumps(other.to_dict()), encoding="utf-8"
    )
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.leases.is_pid_alive",
        lambda pid: pid == 999999,
    )

    launched = {"called": False}

    def fake_launch_emulator_process(*args, **kwargs):
        launched["called"] = True
        raise AssertionError("must not launch a second emulator for a leased AVD")

    monkeypatch.setattr(
        emu_mod, "launch_emulator_process", fake_launch_emulator_process
    )

    with pytest.raises(AvdLeaseError) as excinfo:
        start_emulator(
            avd="concur-probe",
            adb_path="/fake/adb",
            config={},
            run_dir=tmp_path / "run",
            lease_dir=lease_dir,
        )

    assert launched["called"] is False
    assert excinfo.value.extra["owner_pid"] == 999999
    assert "999999" in str(excinfo.value)


def test_start_emulator_acquires_and_records_lease_on_success(
    tmp_path, monkeypatch
) -> None:
    """Happy path: a successful boot leaves a live lease behind, recording
    this process's pid, the port used, and the serial once known."""
    serials_seen = [set(), {"emulator-5570"}]
    _happy_path_monkeypatches(monkeypatch, tmp_path, serials_seen=serials_seen)
    lease_dir = tmp_path / "leases"

    result = start_emulator(
        avd="my-avd",
        adb_path="/fake/adb",
        config={},
        run_dir=tmp_path / "run",
        port=5570,
        lease_dir=lease_dir,
    )

    assert result["serial"] == "emulator-5570"
    lease = read_lease("my-avd", lease_dir)
    assert lease is not None
    assert lease.pid == os.getpid()
    assert lease.port == 5570
    assert lease.serial == "emulator-5570"


def test_start_emulator_releases_lease_on_launch_failure(tmp_path, monkeypatch) -> None:
    """If anything fails after the lease is acquired, it must be released
    again -- otherwise a single failed attempt would permanently (until
    process exit) block every subsequent retry of the same AVD."""
    import amplifier_module_tool_android_inspector.emulator as emu_mod

    monkeypatch.setattr(
        emu_mod, "resolve_emulator_binary", lambda config: "/fake/emulator"
    )
    monkeypatch.setattr(emu_mod, "snapshot_serials", lambda adb_path: set())

    def failing_launch(*args, **kwargs):
        raise EmulatorError("boom -- simulated launch failure")

    monkeypatch.setattr(emu_mod, "launch_emulator_process", failing_launch)

    lease_dir = tmp_path / "leases"
    with pytest.raises(EmulatorError, match="boom"):
        start_emulator(
            avd="my-avd",
            adb_path="/fake/adb",
            config={},
            run_dir=tmp_path / "run",
            port=5554,
            lease_dir=lease_dir,
        )

    assert read_lease("my-avd", lease_dir) is None


def test_start_emulator_allocates_port_when_none_given(tmp_path, monkeypatch) -> None:
    """Defect 2: with no explicit port, a free one is chosen automatically
    and threaded through to the deterministic exact-serial wait -- not the
    'any new serial' heuristic."""
    serials_seen = [{"emulator-5554"}, {"emulator-5554", "emulator-5556"}]
    emu_mod = _happy_path_monkeypatches(
        monkeypatch, tmp_path, serials_seen=serials_seen
    )

    captured: dict = {}

    def capturing_launch(avd, emulator_binary, config, run_dir, *, port=None, **kw):
        captured["port"] = port
        log_path = tmp_path / "fake-emulator.log"
        log_path.write_text("", encoding="utf-8")
        return LaunchedEmulatorProcess(
            pid=999, log_path=log_path, process=FakeProcess(pid=999)
        )

    monkeypatch.setattr(emu_mod, "launch_emulator_process", capturing_launch)

    result = start_emulator(
        avd="my-avd",
        adb_path="/fake/adb",
        config={},
        run_dir=tmp_path / "run",
        port=None,
        lease_dir=tmp_path / "leases",
    )

    # 5554 is already attached (in `serials_seen`) -- the allocator must
    # skip it and pick the next even port, 5556.
    assert captured["port"] == 5556
    assert result["port"] == 5556
    assert result["serial"] == "emulator-5556"
    assert result["port_allocation_fallback"] is False


def test_start_emulator_falls_back_to_any_new_serial_when_port_exhausted(
    tmp_path, monkeypatch
) -> None:
    """Only fall back to the non-deterministic 'any new serial' wait if
    allocation is genuinely impossible -- and say so in the result."""
    import amplifier_module_tool_android_inspector.emulator as emu_mod

    monkeypatch.setattr(
        emu_mod, "resolve_emulator_binary", lambda config: "/fake/emulator"
    )
    monkeypatch.setattr(emu_mod, "ptrace_scope", lambda *a, **k: 0)
    # Every port in the (tiny, monkeypatched) range is already attached.
    all_attached = {
        f"emulator-{p}" for p in range(PORT_RANGE_MIN, PORT_RANGE_MIN + 4, 2)
    }
    monkeypatch.setattr(emu_mod, "PORT_RANGE_MIN", PORT_RANGE_MIN)
    monkeypatch.setattr(emu_mod, "PORT_RANGE_MAX", PORT_RANGE_MIN + 2)

    serials_seen = [all_attached, all_attached | {"emulator-9999"}]
    calls = {"n": 0}

    def fake_snapshot_serials(adb_path):
        idx = min(calls["n"], len(serials_seen) - 1)
        calls["n"] += 1
        return serials_seen[idx]

    monkeypatch.setattr(emu_mod, "snapshot_serials", fake_snapshot_serials)

    def fake_launch(avd, emulator_binary, config, run_dir, *, port=None, **kw):
        assert port is None  # allocation was exhausted -- no port to pass
        log_path = tmp_path / "fake-emulator.log"
        log_path.write_text("", encoding="utf-8")
        return LaunchedEmulatorProcess(
            pid=999, log_path=log_path, process=FakeProcess(pid=999)
        )

    monkeypatch.setattr(emu_mod, "launch_emulator_process", fake_launch)
    monkeypatch.setattr(emu_mod, "wait_boot_completed", lambda *a, **k: None)
    monkeypatch.setattr(emu_mod, "dismiss_keyguard", lambda client: None)

    result = start_emulator(
        avd="my-avd",
        adb_path="/fake/adb",
        config={},
        run_dir=tmp_path / "run",
        port=None,
        lease_dir=tmp_path / "leases",
    )

    assert result["port"] is None
    assert result["serial"] == "emulator-9999"
    assert result["port_allocation_fallback"] is True


# ---------------------------------------------------------------------------
# start_emulator -- Orphaned vs Stale (owner-pid-dead is NOT the same as
# stale; measured live: a session ends, its emulator keeps running).
# ---------------------------------------------------------------------------


def test_start_emulator_refuses_when_orphaned_dead_pid_but_emulator_still_running(
    tmp_path, monkeypatch
) -> None:
    """Measured live: session S1 (some pid) started 'concur-probe' then
    exited normally -- the emulator kept running on 'emulator-5560'. A
    second start_emulator call for the SAME avd must refuse and name the
    existing serial, NEVER proceed to launch a second instance against the
    same AVD (we must not rely on QEMU's own file lock to catch this)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    # `snapshot_serials` (via `before`) reports the orphan's serial as
    # still attached -- this is the live signal that makes it Orphaned,
    # not Stale.
    emu_mod = _happy_path_monkeypatches(
        monkeypatch, tmp_path, serials_seen=[{"emulator-5560"}]
    )

    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    orphaned = AvdLease(
        avd="concur-probe",
        pid=dead_pid,
        port=5560,
        serial="emulator-5560",
        started_at=time.time() - 500,
    )
    (lease_dir / "concur-probe.lease").write_text(
        json.dumps(orphaned.to_dict()), encoding="utf-8"
    )

    launched = {"called": False}

    def fake_launch_emulator_process(*args, **kwargs):
        launched["called"] = True
        raise AssertionError("must not launch a second emulator for an orphaned AVD")

    monkeypatch.setattr(
        emu_mod, "launch_emulator_process", fake_launch_emulator_process
    )

    with pytest.raises(AvdLeaseError) as excinfo:
        start_emulator(
            avd="concur-probe",
            adb_path="/fake/adb",
            config={},
            run_dir=tmp_path / "run",
            lease_dir=lease_dir,
        )

    assert launched["called"] is False
    assert excinfo.value.extra["orphaned"] is True
    assert excinfo.value.extra["owner_pid"] == dead_pid
    assert excinfo.value.extra["serial"] == "emulator-5560"
    assert "emulator-5560" in str(excinfo.value)
    assert str(dead_pid) in str(excinfo.value)
    # Lease is untouched -- refusal must not release someone else's data.
    assert read_lease("concur-probe", lease_dir) is not None


def test_start_emulator_reclaims_when_dead_pid_and_emulator_also_gone(
    tmp_path, monkeypatch
) -> None:
    """Owner pid dead AND the emulator's serial no longer appears in `adb
    devices` -- genuinely Stale. Reclaim and proceed to boot normally,
    exactly like the pre-existing (pid-only) stale-reclaim behaviour."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    # `before` (first snapshot) has nothing attached -- the stale lease's
    # serial is confirmed gone, not merely "pid dead".
    serials_seen = [set(), {"emulator-5570"}]
    _happy_path_monkeypatches(monkeypatch, tmp_path, serials_seen=serials_seen)

    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    stale = AvdLease(
        avd="my-avd",
        pid=dead_pid,
        port=5554,
        serial="emulator-5554",  # NOT in `before` -- truly gone
        started_at=time.time() - 500,
    )
    (lease_dir / "my-avd.lease").write_text(
        json.dumps(stale.to_dict()), encoding="utf-8"
    )

    result = start_emulator(
        avd="my-avd",
        adb_path="/fake/adb",
        config={},
        run_dir=tmp_path / "run",
        port=5570,
        lease_dir=lease_dir,
    )

    assert result["serial"] == "emulator-5570"
    lease = read_lease("my-avd", lease_dir)
    assert lease is not None
    assert lease.pid == os.getpid()


# ---------------------------------------------------------------------------
# stop_emulator -- ownership-gated stop (Defect 3)
# ---------------------------------------------------------------------------


def test_stop_emulator_succeeds_when_owned_by_self(tmp_path, monkeypatch) -> None:
    from amplifier_module_tool_android_inspector.leases import acquire_avd_lease

    lease_dir = tmp_path / "leases"
    acquire_avd_lease("my-avd", port=5554, serial="emulator-5554", lease_dir=lease_dir)
    client = FakeAdbClient(serial="emulator-5554")

    result = stop_emulator(client, lease_dir=lease_dir)

    assert result["emu_kill_ok"] is True
    assert "warning" not in result
    assert read_lease("my-avd", lease_dir) is None  # released after stop


def test_stop_emulator_refuses_when_leased_by_live_other_process(
    tmp_path, monkeypatch
) -> None:
    """Defect 3: the measured cross-project incident this prevents -- a
    sibling session's live emulator must never be silently killed."""
    other = AvdLease(
        avd="their-avd",
        pid=999999,
        port=5554,
        serial="emulator-5554",
        started_at=time.time(),
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "their-avd.lease").write_text(
        json.dumps(other.to_dict()), encoding="utf-8"
    )
    # emulator.py imports `is_pid_alive` by name (`from .leases import
    # is_pid_alive`), which binds a SEPARATE reference in emulator.py's own
    # module namespace -- patching leases.is_pid_alive would not affect
    # calls made from within emulator.py. Patch at the point of use.
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.emulator.is_pid_alive",
        lambda pid: pid == 999999,
    )
    client = FakeAdbClient(serial="emulator-5554")

    with pytest.raises(AvdLeaseError) as excinfo:
        stop_emulator(client, lease_dir=lease_dir)

    assert excinfo.value.extra["owner_pid"] == 999999
    assert "emu" not in [c[0] for c in client.calls]  # emu_kill was never called
    # Lease is untouched -- refusal must not release someone else's lease.
    assert read_lease("their-avd", lease_dir) is not None


def test_stop_emulator_force_overrides_with_prominent_warning(
    tmp_path, monkeypatch
) -> None:
    other = AvdLease(
        avd="their-avd",
        pid=999999,
        port=5554,
        serial="emulator-5554",
        started_at=time.time(),
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "their-avd.lease").write_text(
        json.dumps(other.to_dict()), encoding="utf-8"
    )
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.emulator.is_pid_alive",
        lambda pid: pid == 999999,
    )
    client = FakeAdbClient(serial="emulator-5554")

    result = stop_emulator(client, force=True, lease_dir=lease_dir)

    assert result["emu_kill_ok"] is True
    assert "warning" in result
    assert "999999" in result["warning"]
    assert "their-avd" in result["warning"]
    # Force-stopping releases the (now-defunct) lease.
    assert read_lease("their-avd", lease_dir) is None


def test_stop_emulator_refuses_when_no_lease_record_found(tmp_path) -> None:
    """No lease at all (predates tracking, started outside this tool, or a
    physical device) -- ownership cannot be verified either way, so this
    refuses by default too (the stricter, safer reading)."""
    client = FakeAdbClient(serial="emulator-5554")
    with pytest.raises(AvdLeaseError, match="no lease record"):
        stop_emulator(client, lease_dir=tmp_path / "leases")


def test_stop_emulator_force_when_no_lease_record_still_warns(tmp_path) -> None:
    client = FakeAdbClient(serial="emulator-5554")
    result = stop_emulator(client, force=True, lease_dir=tmp_path / "leases")
    assert result["emu_kill_ok"] is True
    assert "warning" in result
    assert "no lease record" in result["warning"]


# ---------------------------------------------------------------------------
# stop_emulator -- Orphaned vs Stale (owner-pid-dead is NOT the same as
# stale; measured live: session D killed session S1's still-running
# emulator on 'emulator-5558' because the dead owner pid alone was read as
# "safe to kill").
# ---------------------------------------------------------------------------


def test_stop_emulator_refuses_when_orphaned_dead_pid_but_device_alive(
    tmp_path,
) -> None:
    """Measured live incident: owner pid is dead, but the emulator/device
    is still alive -- refuses by default (no force), just like a live
    other-owner would, and names it as orphaned rather than a bare
    'leased by a different live process' (there IS no live process here)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    orphaned = AvdLease(
        avd="concur-probe",
        pid=dead_pid,
        port=5558,
        serial="emulator-5558",
        started_at=time.time() - 500,
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "concur-probe.lease").write_text(
        json.dumps(orphaned.to_dict()), encoding="utf-8"
    )
    client = FakeAdbClient(serial="emulator-5558")

    with pytest.raises(AvdLeaseError) as excinfo:
        stop_emulator(client, lease_dir=lease_dir, device_liveness_probe=lambda: True)

    assert excinfo.value.extra["orphaned"] is True
    assert excinfo.value.extra["owner_pid"] == dead_pid
    assert "orphaned" in str(excinfo.value)
    assert str(dead_pid) in str(excinfo.value)
    assert "emu" not in [c[0] for c in client.calls]  # emu_kill never called
    # Lease untouched -- refusal must not release someone else's data.
    assert read_lease("concur-probe", lease_dir) is not None


def test_stop_emulator_force_overrides_orphaned_with_prominent_warning(
    tmp_path,
) -> None:
    """`force=True` on an orphaned emulator is an obviously safe, informed
    call -- there is no live owner session to disturb -- but it must still
    carry a prominent warning naming it as orphaned, never silent."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    orphaned = AvdLease(
        avd="concur-probe",
        pid=dead_pid,
        port=5558,
        serial="emulator-5558",
        started_at=time.time() - 500,
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "concur-probe.lease").write_text(
        json.dumps(orphaned.to_dict()), encoding="utf-8"
    )
    client = FakeAdbClient(serial="emulator-5558")

    result = stop_emulator(
        client, force=True, lease_dir=lease_dir, device_liveness_probe=lambda: True
    )

    assert result["emu_kill_ok"] is True
    assert "warning" in result
    assert "orphaned" in result["warning"]
    assert str(dead_pid) in result["warning"]
    # Force-stopping releases the (now-defunct) lease.
    assert read_lease("concur-probe", lease_dir) is None


def test_stop_emulator_reclaims_freely_when_dead_pid_and_device_also_gone(
    tmp_path,
) -> None:
    """Owner pid dead AND the device itself is confirmed gone too --
    genuinely Stale. Proceeds without requiring force and without a
    warning, exactly like the pre-existing 'owned by self' happy path."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    stale = AvdLease(
        avd="my-avd",
        pid=dead_pid,
        port=5554,
        serial="emulator-5554",
        started_at=time.time() - 500,
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "my-avd.lease").write_text(
        json.dumps(stale.to_dict()), encoding="utf-8"
    )
    client = FakeAdbClient(serial="emulator-5554")

    result = stop_emulator(
        client, lease_dir=lease_dir, device_liveness_probe=lambda: False
    )

    assert result["emu_kill_ok"] is True
    assert "warning" not in result
    assert read_lease("my-avd", lease_dir) is None


def test_stop_emulator_default_probe_uses_get_state_when_device_alive(
    tmp_path,
) -> None:
    """Without an injected probe, liveness is determined via `adb -s
    <serial> get-state` through `client` itself -- no adb_path plumbing
    required. FakeAdbClient.run() defaults to returncode=0 for anything
    other than 'exec-out cat', i.e. the device reports present/alive."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    orphaned = AvdLease(
        avd="concur-probe",
        pid=dead_pid,
        port=5558,
        serial="emulator-5558",
        started_at=time.time() - 500,
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "concur-probe.lease").write_text(
        json.dumps(orphaned.to_dict()), encoding="utf-8"
    )
    client = FakeAdbClient(serial="emulator-5558")

    with pytest.raises(AvdLeaseError) as excinfo:
        stop_emulator(client, lease_dir=lease_dir)

    assert excinfo.value.extra["orphaned"] is True
    assert ("get-state",) in [tuple(c) for c in client.calls]


def test_stop_emulator_default_probe_reclaims_when_get_state_reports_gone(
    tmp_path,
) -> None:
    """Default probe path, Stale branch: `adb -s <serial> get-state`
    reporting the device gone (nonzero exit, as real adb does for an
    unknown serial) reclaims freely -- no force required."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    stale = AvdLease(
        avd="my-avd",
        pid=dead_pid,
        port=5554,
        serial="emulator-5554",
        started_at=time.time() - 500,
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "my-avd.lease").write_text(
        json.dumps(stale.to_dict()), encoding="utf-8"
    )
    client = FakeAdbClient(serial="emulator-5554")

    def run(*args, timeout=None, check_output=False):
        client.calls.append(args)
        if args[:1] == ("get-state",):
            return AdbCommandResult(
                args=list(args),
                returncode=1,
                stdout="",
                stderr="error: device 'emulator-5554' not found",
            )
        return AdbCommandResult(args=list(args), returncode=0, stdout="", stderr="")

    client.run = run  # type: ignore[assignment]

    result = stop_emulator(client, lease_dir=lease_dir)

    assert result["emu_kill_ok"] is True  # emu_kill still attempted -- harmless no-op
    assert "warning" not in result
    assert read_lease("my-avd", lease_dir) is None
