"""Unit tests for emulator.py — host workaround detection, gdb script
generation, process launch argv construction, and serial discovery. No real
emulator or gdb is spawned; subprocess.Popen and serial listing are injected."""

from __future__ import annotations

import subprocess

import pytest
from amplifier_module_tool_android_inspector.adb import AdbCommandResult
from amplifier_module_tool_android_inspector.emulator import (
    DEFAULT_EMULATOR_ARGS,
    EmulatorError,
    build_gdb_launch_script,
    dismiss_keyguard,
    launch_emulator_process,
    ptrace_scope,
    wait_boot_completed,
    wait_for_new_serial,
)

from .conftest import FakeAdbClient

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
    pid, log_path = launch_emulator_process(
        "my-avd",
        emulator_binary,
        {},
        tmp_path / "run",
        popen_factory=fake_popen,
        ptrace_scope_override=0,
    )

    assert pid == 4242
    assert captured["argv"] == [
        emulator_binary,
        "-avd",
        "my-avd",
        *DEFAULT_EMULATOR_ARGS,
    ]
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert captured["kwargs"]["start_new_session"] is True
    assert log_path.parent == tmp_path / "run"


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
    pid, _log_path = launch_emulator_process(
        "my-avd",
        emulator_binary,
        {},
        run_dir,
        popen_factory=fake_popen,
        ptrace_scope_override=1,
    )

    assert pid == 99
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
