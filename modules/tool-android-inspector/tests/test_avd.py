"""Unit tests for avd.py -- AVD discovery/fast-fail, doctor checks, and
create_avd provisioning. No real adb/emulator/sdkmanager dependency;
subprocess interactions are injected via runner/verify/which_fn callables."""

from __future__ import annotations

import subprocess

import pytest
from amplifier_module_tool_android_inspector.adb import AdbCommandResult
from amplifier_module_tool_android_inspector.avd import (
    AvdError,
    check_adb_binary,
    check_adb_server,
    check_android_home,
    check_avds_available,
    check_cmdline_tools,
    check_emulator_binary,
    check_gdb_present,
    check_host_platform,
    check_kvm,
    check_ptrace_scope,
    create_avd,
    detect_host_abi,
    list_avd_names_from_disk,
    list_avds_via_emulator,
    require_avd_exists,
    resolve_avd_home,
    resolve_cmdline_tool,
    run_doctor,
)
from amplifier_module_tool_android_inspector.emulator import (
    EmulatorError,
    LaunchedEmulatorProcess,
    start_emulator,
)

from .conftest import FakeProcess


def _proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# detect_host_abi
# ---------------------------------------------------------------------------


def test_detect_host_abi_aarch64(monkeypatch) -> None:
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.avd.platform.machine",
        lambda: "aarch64",
    )
    assert detect_host_abi() == "arm64-v8a"


def test_detect_host_abi_x86_64(monkeypatch) -> None:
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.avd.platform.machine", lambda: "x86_64"
    )
    assert detect_host_abi() == "x86_64"


# ---------------------------------------------------------------------------
# resolve_avd_home -- fallback chain
# ---------------------------------------------------------------------------


def test_resolve_avd_home_config_override(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ANDROID_AVD_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_HOME", raising=False)
    override = tmp_path / "custom-avd-home"
    assert resolve_avd_home({"avd_home": str(override)}) == override


def test_resolve_avd_home_env_avd_home(monkeypatch, tmp_path) -> None:
    avd_home = tmp_path / "avd-home"
    monkeypatch.setenv("ANDROID_AVD_HOME", str(avd_home))
    monkeypatch.delenv("ANDROID_SDK_HOME", raising=False)
    assert resolve_avd_home({}) == avd_home


def test_resolve_avd_home_sdk_home_fallback(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ANDROID_AVD_HOME", raising=False)
    monkeypatch.setenv("ANDROID_SDK_HOME", str(tmp_path))
    assert resolve_avd_home({}) == tmp_path / ".android" / "avd"


def test_resolve_avd_home_final_fallback(monkeypatch) -> None:
    monkeypatch.delenv("ANDROID_AVD_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_HOME", raising=False)
    from pathlib import Path

    assert resolve_avd_home({}) == Path("~/.android/avd").expanduser()


# ---------------------------------------------------------------------------
# list_avd_names_from_disk
# ---------------------------------------------------------------------------


def test_list_avd_names_from_disk_missing_dir(tmp_path) -> None:
    assert list_avd_names_from_disk(tmp_path / "does-not-exist") == []


def test_list_avd_names_from_disk_lists_ini_files(tmp_path) -> None:
    (tmp_path / "Pixel_6.ini").write_text("", encoding="utf-8")
    (tmp_path / "Nexus_5.ini").write_text("", encoding="utf-8")
    (tmp_path / "not-an-avd.txt").write_text("", encoding="utf-8")
    assert list_avd_names_from_disk(tmp_path) == ["Nexus_5", "Pixel_6"]


# ---------------------------------------------------------------------------
# list_avds_via_emulator
# ---------------------------------------------------------------------------


def test_list_avds_via_emulator_none_when_no_binary() -> None:
    assert list_avds_via_emulator(None) is None


def test_list_avds_via_emulator_none_when_runner_raises() -> None:
    def runner(argv):
        raise OSError("Exec format error")

    assert list_avds_via_emulator("/opt/sdk/emulator/emulator", runner=runner) is None


def test_list_avds_via_emulator_none_when_nonzero_exit() -> None:
    def runner(argv):
        return _proc(returncode=1, stderr="boom")

    assert list_avds_via_emulator("/opt/sdk/emulator/emulator", runner=runner) is None


def test_list_avds_via_emulator_parses_names() -> None:
    def runner(argv):
        assert argv == ["/opt/sdk/emulator/emulator", "-list-avds"]
        return _proc(returncode=0, stdout="Pixel_6\nNexus_5\n")

    result = list_avds_via_emulator("/opt/sdk/emulator/emulator", runner=runner)
    assert result == ["Pixel_6", "Nexus_5"]


# ---------------------------------------------------------------------------
# require_avd_exists -- the fast-fail path (CHANGE 1)
# ---------------------------------------------------------------------------


def test_require_avd_exists_passes_when_avd_on_disk(tmp_path) -> None:
    (tmp_path / "my-avd.ini").write_text("", encoding="utf-8")
    # Must not raise.
    require_avd_exists("my-avd", {"avd_home": str(tmp_path)})


def test_require_avd_exists_passes_when_avd_only_via_emulator_crosscheck(
    tmp_path,
) -> None:
    # Nothing on disk, but the emulator cross-check reports it.
    def runner(argv):
        return _proc(returncode=0, stdout="my-avd\n")

    require_avd_exists(
        "my-avd",
        {"avd_home": str(tmp_path)},
        emulator_binary="/opt/sdk/emulator/emulator",
        emulator_runner=runner,
    )
    # (uses the module's default subprocess runner internally when none is
    # injected via list_avds_via_emulator directly -- exercised above; here
    # we just confirm the disk-only path above is independently sufficient.)


def test_require_avd_exists_raises_immediately_with_structured_extra(tmp_path) -> None:
    (tmp_path / "real-avd-1.ini").write_text("", encoding="utf-8")
    (tmp_path / "real-avd-2.ini").write_text("", encoding="utf-8")

    with pytest.raises(AvdError) as excinfo:
        require_avd_exists("no-such-avd", {"avd_home": str(tmp_path)})

    exc = excinfo.value
    assert exc.extra["avd"] == "no-such-avd"
    assert exc.extra["existing_avds"] == ["real-avd-1", "real-avd-2"]
    assert exc.extra["create_avd_operation"] == "create_avd"
    remediation = exc.extra["remediation_command"]
    assert remediation.startswith("avdmanager create avd -n no-such-avd -k")
    assert "system-images;android-35;google_apis;" in remediation
    assert "-d pixel_6" in remediation
    # The message itself also carries the same information for human readers.
    assert "no-such-avd" in str(exc)
    assert "real-avd-1" in str(exc)


def test_require_avd_exists_remediation_uses_detected_abi(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.avd.platform.machine",
        lambda: "aarch64",
    )
    with pytest.raises(AvdError) as excinfo:
        require_avd_exists("missing", {"avd_home": str(tmp_path)})
    assert "arm64-v8a" in excinfo.value.extra["remediation_command"]

    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.avd.platform.machine", lambda: "x86_64"
    )
    with pytest.raises(AvdError) as excinfo2:
        require_avd_exists("missing", {"avd_home": str(tmp_path)})
    assert "x86_64" in excinfo2.value.extra["remediation_command"]


def test_require_avd_exists_no_avds_reports_none(tmp_path) -> None:
    with pytest.raises(AvdError) as excinfo:
        require_avd_exists("missing", {"avd_home": str(tmp_path)})
    assert excinfo.value.extra["existing_avds"] == []
    assert "none" in str(excinfo.value)


def test_start_emulator_fast_fail_spawns_nothing(tmp_path, monkeypatch) -> None:
    """Full tool-level regression guard for CHANGE 1: a missing AVD must
    fail immediately through AndroidInspectorTool.execute(), and must never
    invoke the emulator-launch implementation (no Popen, no 60s wait)."""
    import amplifier_module_tool_android_inspector as pkg

    (tmp_path / "existing-avd.ini").write_text("", encoding="utf-8")

    called = {"start_emulator_impl": False}

    def spy_start_emulator_impl(**kwargs):
        called["start_emulator_impl"] = True
        raise AssertionError("emulator implementation must not be invoked")

    monkeypatch.setattr(pkg, "_start_emulator_impl", spy_start_emulator_impl)
    monkeypatch.setattr(
        pkg,
        "resolve_emulator_binary",
        lambda config: (_ for _ in ()).throw(EmulatorError("no emulator")),
    )

    # Defect 4: config is per-instance now -- construct the tool with its
    # config directly rather than mutating a module-level singleton state.
    tool = pkg.AndroidInspectorTool(config={"avd_home": str(tmp_path)})

    import asyncio

    result = asyncio.run(
        tool.execute({"operation": "start_emulator", "avd": "no-such-avd"})
    )

    assert called["start_emulator_impl"] is False
    assert result["success"] is False
    assert result["avd"] == "no-such-avd"
    assert result["existing_avds"] == ["existing-avd"]
    assert "avdmanager create avd" in result["remediation_command"]
    assert result["create_avd_operation"] == "create_avd"


# ---------------------------------------------------------------------------
# start_emulator — Defect 1: explicit `port` validated, threaded through,
# and never silently discarded. Orchestration is exercised directly against
# emulator.start_emulator() with its collaborators monkeypatched (same
# module-level-lookup technique the fast-fail test above relies on).
# ---------------------------------------------------------------------------


def test_start_emulator_rejects_odd_port_without_launching(
    tmp_path, monkeypatch
) -> None:
    import amplifier_module_tool_android_inspector.emulator as emu_mod

    monkeypatch.setattr(
        emu_mod, "resolve_emulator_binary", lambda config: "/fake/emulator"
    )
    monkeypatch.setattr(emu_mod, "snapshot_serials", lambda adb_path: set())

    def fake_launch_emulator_process(*args, **kwargs):
        raise AssertionError("must not launch on an invalid port")

    monkeypatch.setattr(
        emu_mod, "launch_emulator_process", fake_launch_emulator_process
    )

    with pytest.raises(EmulatorError, match="even"):
        start_emulator(
            avd="my-avd",
            adb_path="/fake/adb",
            config={},
            run_dir=tmp_path / "run",
            port=5555,
            lease_dir=tmp_path / "leases",
        )


def test_start_emulator_rejects_out_of_range_port_without_launching(
    tmp_path, monkeypatch
) -> None:
    import amplifier_module_tool_android_inspector.emulator as emu_mod

    monkeypatch.setattr(
        emu_mod, "resolve_emulator_binary", lambda config: "/fake/emulator"
    )
    monkeypatch.setattr(emu_mod, "snapshot_serials", lambda adb_path: set())

    def fake_launch_emulator_process(*args, **kwargs):
        raise AssertionError("must not launch on an out-of-range port")

    monkeypatch.setattr(
        emu_mod, "launch_emulator_process", fake_launch_emulator_process
    )

    with pytest.raises(EmulatorError, match="5554-5682"):
        start_emulator(
            avd="my-avd",
            adb_path="/fake/adb",
            config={},
            run_dir=tmp_path / "run",
            port=5000,
            lease_dir=tmp_path / "leases",
        )


def test_start_emulator_refuses_when_expected_serial_already_attached(
    tmp_path, monkeypatch
) -> None:
    """The cross-AVD-install hazard: refuse to adopt an already-attached
    device/emulator's serial rather than silently operating on it."""
    import amplifier_module_tool_android_inspector.emulator as emu_mod

    monkeypatch.setattr(
        emu_mod, "resolve_emulator_binary", lambda config: "/fake/emulator"
    )
    monkeypatch.setattr(emu_mod, "snapshot_serials", lambda adb_path: {"emulator-5570"})

    launched = {"called": False}

    def fake_launch_emulator_process(*args, **kwargs):
        launched["called"] = True
        raise AssertionError("must not launch when the target serial is attached")

    monkeypatch.setattr(
        emu_mod, "launch_emulator_process", fake_launch_emulator_process
    )

    with pytest.raises(EmulatorError, match="already attached"):
        start_emulator(
            avd="my-avd",
            adb_path="/fake/adb",
            config={},
            run_dir=tmp_path / "run",
            port=5570,
            lease_dir=tmp_path / "leases",
        )

    assert launched["called"] is False


def test_start_emulator_threads_port_through_and_waits_on_exact_serial(
    tmp_path, monkeypatch
) -> None:
    """Defect 1, happy path: port reaches launch_emulator_process, and the
    deterministic 'emulator-<port>' serial (not 'any new serial') is what's
    waited on and returned."""
    import amplifier_module_tool_android_inspector.emulator as emu_mod

    monkeypatch.setattr(
        emu_mod, "resolve_emulator_binary", lambda config: "/fake/emulator"
    )
    monkeypatch.setattr(emu_mod, "ptrace_scope", lambda *a, **k: 0)

    serials_seen = [set(), {"emulator-5570"}]
    snapshot_calls = {"n": 0}

    def fake_snapshot_serials(adb_path):
        idx = min(snapshot_calls["n"], len(serials_seen) - 1)
        snapshot_calls["n"] += 1
        return serials_seen[idx]

    monkeypatch.setattr(emu_mod, "snapshot_serials", fake_snapshot_serials)

    captured: dict = {}

    def fake_launch_emulator_process(
        avd, emulator_binary, config, run_dir, *, port=None, **kw
    ):
        captured["port"] = port
        log_path = tmp_path / "fake-emulator.log"
        log_path.write_text("", encoding="utf-8")
        return LaunchedEmulatorProcess(
            pid=999, log_path=log_path, process=FakeProcess(pid=999)
        )

    monkeypatch.setattr(
        emu_mod, "launch_emulator_process", fake_launch_emulator_process
    )

    boot_calls = {"n": 0}

    def fake_wait_boot_completed(
        client, *, timeout_s, poll_s=None, process=None, log_path=None
    ):
        boot_calls["n"] += 1

    monkeypatch.setattr(emu_mod, "wait_boot_completed", fake_wait_boot_completed)
    monkeypatch.setattr(emu_mod, "dismiss_keyguard", lambda client: None)

    result = start_emulator(
        avd="my-avd",
        adb_path="/fake/adb",
        config={},
        run_dir=tmp_path / "run",
        port=5570,
        lease_dir=tmp_path / "leases",
    )

    assert captured["port"] == 5570
    assert result["serial"] == "emulator-5570"
    assert result["port"] == 5570
    assert result["pid"] == 999
    assert boot_calls["n"] == 1


# ---------------------------------------------------------------------------
# resolve_cmdline_tool
# ---------------------------------------------------------------------------


def test_resolve_cmdline_tool_finds_latest_bin(tmp_path) -> None:
    latest_bin = tmp_path / "cmdline-tools" / "latest" / "bin"
    latest_bin.mkdir(parents=True)
    sdkmanager = latest_bin / "sdkmanager"
    sdkmanager.write_text("", encoding="utf-8")
    result = resolve_cmdline_tool("sdkmanager", {"android_home": str(tmp_path)})
    assert result == str(sdkmanager)


def test_resolve_cmdline_tool_finds_plain_bin(tmp_path) -> None:
    plain_bin = tmp_path / "cmdline-tools" / "bin"
    plain_bin.mkdir(parents=True)
    avdmanager = plain_bin / "avdmanager"
    avdmanager.write_text("", encoding="utf-8")
    result = resolve_cmdline_tool("avdmanager", {"android_home": str(tmp_path)})
    assert result == str(avdmanager)


def test_resolve_cmdline_tool_none_when_unresolvable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.avd.shutil.which", lambda name: None
    )
    assert resolve_cmdline_tool("sdkmanager", {"android_home": str(tmp_path)}) is None


# ---------------------------------------------------------------------------
# doctor checks (CHANGE 2) -- ok/warn/fail branches
# ---------------------------------------------------------------------------


def test_check_host_platform_always_ok() -> None:
    result = check_host_platform()
    assert result["status"] == "ok"
    assert result["name"] == "host_platform"


def test_check_android_home_fail_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    result = check_android_home({})
    assert result["status"] == "fail"


def test_check_android_home_fail_when_dir_missing(tmp_path) -> None:
    missing = tmp_path / "does-not-exist"
    result = check_android_home({"android_home": str(missing)})
    assert result["status"] == "fail"
    assert str(missing) in result["detail"]


def test_check_android_home_ok_when_present(tmp_path) -> None:
    result = check_android_home({"android_home": str(tmp_path)})
    assert result["status"] == "ok"


def test_check_adb_binary_ok(tmp_path) -> None:
    adb_path = tmp_path / "adb"
    adb_path.write_text("", encoding="utf-8")

    def verify(argv):
        return _proc(returncode=0, stdout="Android Debug Bridge version 1.0.41")

    result = check_adb_binary({"adb_path": str(adb_path)}, verify=verify)
    assert result["status"] == "ok"


def test_check_adb_binary_exec_format_error_remediation(tmp_path) -> None:
    adb_path = tmp_path / "adb"
    adb_path.write_text("", encoding="utf-8")

    def verify(argv):
        return _proc(returncode=1, stderr="Exec format error")

    result = check_adb_binary({"adb_path": str(adb_path)}, verify=verify)
    assert result["status"] == "fail"
    assert "aarch64" in result["remediation"]
    assert "platform-tools-arm64" in result["remediation"]


def test_check_adb_binary_fail_when_not_found(monkeypatch, tmp_path) -> None:
    # Hermetic on purpose. This asserts "no adb resolvable ANYWHERE -> fail",
    # so the *host's* adb has to be excluded or the assertion means nothing.
    # resolve_adb_binary probes, in order: config['adb_path'],
    # $ANDROID_HOME/platform-tools{-arm64}/adb, then shutil.which("adb").
    # GitHub's ubuntu-latest runners ship the Android SDK -- ANDROID_HOME set
    # and adb on PATH -- so unpinned this test resolved a real adb and the
    # check correctly reported "ok". The test was wrong, not the code.
    import amplifier_module_tool_android_inspector.adb as adb_mod

    monkeypatch.setattr(adb_mod.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)

    result = check_adb_binary(
        {"adb_path": "/nonexistent/adb", "android_home": str(tmp_path / "no-sdk")},
        verify=lambda argv: _proc(),
    )
    assert result["status"] == "fail"


def test_check_adb_server_fail_when_no_adb_path() -> None:
    result = check_adb_server(None)
    assert result["status"] == "fail"


def test_check_adb_server_ok_no_devices() -> None:
    def runner(argv, timeout):
        return AdbCommandResult(
            args=argv, returncode=0, stdout="List of devices attached\n\n", stderr=""
        )

    result = check_adb_server("/opt/sdk/platform-tools/adb", runner=runner)
    assert result["status"] == "ok"


def test_check_adb_server_warn_on_offline_or_unauthorized() -> None:
    def runner(argv, timeout):
        stdout = (
            "List of devices attached\n"
            "emulator-5554\tdevice\n"
            "emulator-5556\toffline\n"
            "abcd1234\tunauthorized\n"
        )
        return AdbCommandResult(args=argv, returncode=0, stdout=stdout, stderr="")

    result = check_adb_server("/opt/sdk/platform-tools/adb", runner=runner)
    assert result["status"] == "warn"
    assert "emulator-5556" in result["detail"]
    assert "abcd1234" in result["detail"]


def test_check_adb_server_ok_when_all_ready() -> None:
    def runner(argv, timeout):
        return AdbCommandResult(
            args=argv,
            returncode=0,
            stdout="List of devices attached\nemulator-5554\tdevice\n",
            stderr="",
        )

    result = check_adb_server("/opt/sdk/platform-tools/adb", runner=runner)
    assert result["status"] == "ok"


def test_check_adb_server_fail_when_devices_command_fails() -> None:
    def runner(argv, timeout):
        return AdbCommandResult(
            args=argv, returncode=1, stdout="", stderr="server broken"
        )

    result = check_adb_server("/opt/sdk/platform-tools/adb", runner=runner)
    assert result["status"] == "fail"


def test_check_emulator_binary_ok(tmp_path) -> None:
    binary = tmp_path / "emulator" / "emulator"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")

    def runner(argv):
        return _proc(returncode=0, stdout="Android emulator version 35.0")

    result = check_emulator_binary({"emulator_path": str(binary)}, runner=runner)
    assert result["status"] == "ok"


def test_check_emulator_binary_fail_missing_on_aarch64_linux(tmp_path) -> None:
    result = check_emulator_binary(
        {"emulator_path": str(tmp_path / "no-such-emulator")},
        system="Linux",
        abi="arm64-v8a",
    )
    assert result["status"] == "fail"
    assert "Google ships no linux-aarch64 emulator" in result["remediation"]
    assert "docs/TROUBLESHOOTING.md" in result["remediation"]


def test_check_emulator_binary_fail_missing_on_other_host(tmp_path) -> None:
    result = check_emulator_binary(
        {"emulator_path": str(tmp_path / "no-such-emulator")},
        system="Darwin",
        abi="x86_64",
    )
    assert result["status"] == "fail"
    assert "Google ships no linux-aarch64 emulator" not in result["remediation"]


def test_check_emulator_binary_fail_missing_on_x86_64_linux_names_sdkmanager_install(
    tmp_path,
) -> None:
    """Defect 5, the exact measured scenario: a real x86_64 Linux host
    (e.g. 'alienware-r13') with ANDROID_HOME resolving fine but no emulator
    installed under it used to get a remediation that just repeated the
    detail string verbatim -- zero new information. Google DOES ship a
    linux-x86_64 emulator, so the fix is a single sdkmanager command.
    `android_home` (not `emulator_path`) reproduces the exact
    `resolve_emulator_binary` code path that produced the measured
    "emulator binary not found at ..." message."""
    result = check_emulator_binary(
        {"android_home": str(tmp_path)},
        system="Linux",
        abi="x86_64",
    )
    assert result["status"] == "fail"
    assert "emulator binary not found" in result["detail"]
    assert result["remediation"] != result["detail"]
    assert "sdkmanager" in result["remediation"]
    assert "--install" in result["remediation"]
    assert '"emulator"' in result["remediation"]
    assert "system image" in result["remediation"].lower()
    assert "Google ships no linux-aarch64 emulator" not in result["remediation"]


def test_check_emulator_binary_fail_missing_on_aarch64_linux_uses_sdkmanager_too(
    tmp_path,
) -> None:
    """Same 'ANDROID_HOME resolves, emulator subdir missing' scenario as
    above, but on aarch64 -- must NOT get the sdkmanager remediation (no
    linux-aarch64 emulator exists to install); the community-build pointer
    is still correct and takes priority."""
    result = check_emulator_binary(
        {"android_home": str(tmp_path)},
        system="Linux",
        abi="arm64-v8a",
    )
    assert result["status"] == "fail"
    assert "Google ships no linux-aarch64 emulator" in result["remediation"]
    assert "sdkmanager" not in result["remediation"]


def test_check_emulator_binary_aarch64_message_unchanged_by_defect_5_fix(
    tmp_path,
) -> None:
    """The aarch64 case must be completely untouched by the Defect 5 fix."""
    result = check_emulator_binary(
        {"emulator_path": str(tmp_path / "no-such-emulator")},
        system="Linux",
        abi="arm64-v8a",
    )
    assert result["status"] == "fail"
    assert "Google ships no linux-aarch64 emulator" in result["remediation"]
    assert "docs/TROUBLESHOOTING.md" in result["remediation"]
    assert "sdkmanager" not in result["remediation"]


def test_check_emulator_binary_exec_format_error(tmp_path) -> None:
    binary = tmp_path / "emulator" / "emulator"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")

    def runner(argv):
        return _proc(returncode=1, stderr="Exec format error")

    result = check_emulator_binary(
        {"emulator_path": str(binary)}, runner=runner, system="Linux", abi="arm64-v8a"
    )
    assert result["status"] == "fail"
    assert "Exec format error" in result["detail"]
    assert "Google ships no linux-aarch64 emulator" in result["remediation"]


def test_check_kvm_ok_when_not_linux(tmp_path) -> None:
    result = check_kvm(tmp_path / "kvm", system="Darwin")
    assert result["status"] == "ok"


def test_check_kvm_fail_when_missing(tmp_path) -> None:
    result = check_kvm(tmp_path / "does-not-exist", system="Linux")
    assert result["status"] == "fail"


def test_check_kvm_fail_when_not_accessible(tmp_path) -> None:
    kvm_path = tmp_path / "kvm"
    kvm_path.write_text("", encoding="utf-8")
    result = check_kvm(kvm_path, system="Linux", access_fn=lambda p, mode: False)
    assert result["status"] == "fail"
    assert "kvm" in result["remediation"].lower()


def test_check_kvm_ok_when_accessible(tmp_path) -> None:
    kvm_path = tmp_path / "kvm"
    kvm_path.write_text("", encoding="utf-8")
    result = check_kvm(kvm_path, system="Linux", access_fn=lambda p, mode: True)
    assert result["status"] == "ok"


def test_check_ptrace_scope_ok_when_not_linux() -> None:
    result = check_ptrace_scope(1, system="Darwin")
    assert result["status"] == "ok"


def test_check_ptrace_scope_ok_when_none() -> None:
    result = check_ptrace_scope(None, system="Linux")
    assert result["status"] == "ok"


def test_check_ptrace_scope_ok_when_zero() -> None:
    result = check_ptrace_scope(0, system="Linux")
    assert result["status"] == "ok"


def test_check_ptrace_scope_warn_when_nonzero() -> None:
    result = check_ptrace_scope(1, system="Linux")
    assert result["status"] == "warn"
    assert "gdb" in result["detail"]


def test_check_gdb_present_ok_when_found_and_not_required() -> None:
    result = check_gdb_present(0, which_fn=lambda name: "/usr/bin/gdb")
    assert result["status"] == "ok"


def test_check_gdb_present_ok_when_found_and_required() -> None:
    result = check_gdb_present(1, which_fn=lambda name: "/usr/bin/gdb")
    assert result["status"] == "ok"
    assert "Required" in result["detail"]


def test_check_gdb_present_fail_when_missing_and_required() -> None:
    result = check_gdb_present(1, which_fn=lambda name: None)
    assert result["status"] == "fail"


def test_check_gdb_present_ok_when_missing_and_not_required() -> None:
    result = check_gdb_present(0, which_fn=lambda name: None)
    assert result["status"] == "ok"


def test_check_avds_available_warn_when_empty(tmp_path) -> None:
    result = check_avds_available({"avd_home": str(tmp_path)})
    assert result["status"] == "warn"
    assert "create_avd" in result["remediation"]


def test_check_avds_available_ok_when_present(tmp_path) -> None:
    (tmp_path / "my-avd.ini").write_text("", encoding="utf-8")
    result = check_avds_available({"avd_home": str(tmp_path)})
    assert result["status"] == "ok"
    assert "my-avd" in result["detail"]


def test_check_cmdline_tools_warn_when_missing(tmp_path) -> None:
    result = check_cmdline_tools({"android_home": str(tmp_path)})
    assert result["status"] == "warn"


def test_check_cmdline_tools_ok_when_present(tmp_path) -> None:
    latest_bin = tmp_path / "cmdline-tools" / "latest" / "bin"
    latest_bin.mkdir(parents=True)
    (latest_bin / "sdkmanager").write_text("", encoding="utf-8")
    (latest_bin / "avdmanager").write_text("", encoding="utf-8")
    result = check_cmdline_tools({"android_home": str(tmp_path)})
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# run_doctor -- orchestration: never raises, always full report
# ---------------------------------------------------------------------------


def test_run_doctor_returns_ten_checks_and_never_raises(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_AVD_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_HOME", raising=False)

    report = run_doctor({})

    assert isinstance(report["ready"], bool)
    assert isinstance(report["checks"], list)
    assert len(report["checks"]) == 10
    assert isinstance(report["summary"], str)
    names = {c["name"] for c in report["checks"]}
    assert names == {
        "host_platform",
        "android_home",
        "adb_binary",
        "adb_server",
        "emulator_binary",
        "kvm",
        "ptrace_scope",
        "gdb_present",
        "avds_available",
        "cmdline_tools",
    }


def test_run_doctor_ready_false_when_any_check_fails(monkeypatch) -> None:
    import amplifier_module_tool_android_inspector.avd as avd_mod

    monkeypatch.setattr(
        avd_mod,
        "check_android_home",
        lambda config: {
            "name": "android_home",
            "status": "fail",
            "detail": "x",
            "remediation": "y",
        },
    )
    report = run_doctor({})
    assert report["ready"] is False
    assert "android_home" in report["summary"]


def test_run_doctor_survives_a_check_that_raises(monkeypatch) -> None:
    import amplifier_module_tool_android_inspector.avd as avd_mod

    def boom(config):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(avd_mod, "check_android_home", boom)
    report = run_doctor({})  # must not raise
    assert report["ready"] is False
    android_home_check = next(
        c for c in report["checks"] if c["name"] == "android_home"
    )
    assert android_home_check["status"] == "fail"
    assert "kaboom" in android_home_check["detail"]


def test_run_doctor_summary_ready_when_all_ok(monkeypatch) -> None:
    import amplifier_module_tool_android_inspector.avd as avd_mod

    def ok_check(*a, **k):
        return {
            "name": "x",
            "status": "ok",
            "detail": "fine",
            "remediation": None,
        }

    for name in (
        "check_host_platform",
        "check_android_home",
        "check_adb_binary",
        "check_adb_server",
        "check_emulator_binary",
        "check_kvm",
        "check_ptrace_scope",
        "check_gdb_present",
        "check_avds_available",
        "check_cmdline_tools",
    ):
        monkeypatch.setattr(avd_mod, name, ok_check)

    report = run_doctor({})
    assert report["ready"] is True
    assert report["summary"] == "All checks passed."


# ---------------------------------------------------------------------------
# create_avd (CHANGE 3)
# ---------------------------------------------------------------------------


def _cmdline_tools(tmp_path):
    latest_bin = tmp_path / "cmdline-tools" / "latest" / "bin"
    latest_bin.mkdir(parents=True)
    sdkmanager = latest_bin / "sdkmanager"
    avdmanager = latest_bin / "avdmanager"
    sdkmanager.write_text("", encoding="utf-8")
    avdmanager.write_text("", encoding="utf-8")
    return str(sdkmanager), str(avdmanager)


def test_create_avd_abi_auto_detection_aarch64(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.avd.platform.machine",
        lambda: "aarch64",
    )
    sdkmanager, avdmanager = _cmdline_tools(tmp_path)

    calls: list[list[str]] = []

    def runner(argv, *, input_text=None, timeout=600.0):
        calls.append(argv)
        if argv[:2] == [sdkmanager, "--list_installed"]:
            return _proc(
                returncode=0, stdout="system-images;android-35;google_apis;arm64-v8a\n"
            )
        if argv[0] == avdmanager:
            # Simulate the real side effect: avdmanager writes the AVD's
            # .ini file on success. No emulator_path is configured, so
            # create_avd falls back to the disk-based verification path.
            (tmp_path / "my-new-avd.ini").write_text("", encoding="utf-8")
            return _proc(returncode=0)
        return _proc(returncode=0)

    result = create_avd(
        name="my-new-avd",
        config={"android_home": str(tmp_path), "avd_home": str(tmp_path)},
        runner=runner,
    )
    assert result["abi"] == "arm64-v8a"
    assert "system-images;android-35;google_apis;arm64-v8a" in result["package"]
    assert result["verification_method"].startswith("avd .ini files")


def test_create_avd_abi_auto_detection_x86_64(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.avd.platform.machine", lambda: "x86_64"
    )
    sdkmanager, avdmanager = _cmdline_tools(tmp_path)

    def runner(argv, *, input_text=None, timeout=600.0):
        if argv[:2] == [sdkmanager, "--list_installed"]:
            return _proc(
                returncode=0, stdout="system-images;android-35;google_apis;x86_64\n"
            )
        if argv[0] == avdmanager:
            (tmp_path / "my-new-avd.ini").write_text("", encoding="utf-8")
            return _proc(returncode=0)
        return _proc(returncode=0)

    result = create_avd(
        name="my-new-avd",
        config={"android_home": str(tmp_path), "avd_home": str(tmp_path)},
        runner=runner,
    )
    assert result["abi"] == "x86_64"


def test_create_avd_abi_explicit_override_respected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.avd.platform.machine",
        lambda: "aarch64",
    )
    sdkmanager, avdmanager = _cmdline_tools(tmp_path)

    def runner(argv, *, input_text=None, timeout=600.0):
        if argv[:2] == [sdkmanager, "--list_installed"]:
            return _proc(
                returncode=0, stdout="system-images;android-35;google_apis;x86_64\n"
            )
        if argv[0] == avdmanager:
            (tmp_path / "my-new-avd.ini").write_text("", encoding="utf-8")
            return _proc(returncode=0)
        return _proc(returncode=0)

    result = create_avd(
        name="my-new-avd",
        config={"android_home": str(tmp_path), "avd_home": str(tmp_path)},
        abi="x86_64",  # explicit override even though host is aarch64
        runner=runner,
    )
    assert result["abi"] == "x86_64"


def test_create_avd_verifies_via_emulator_list_avds_when_resolvable(tmp_path) -> None:
    """The primary verification path per spec: `emulator -list-avds`, not
    the disk fallback. Deliberately does NOT write the .ini file, so if the
    implementation fell back to disk it would (wrongly) report unverified."""
    sdkmanager, avdmanager = _cmdline_tools(tmp_path)
    emulator_dir = tmp_path / "emulator"
    emulator_dir.mkdir()
    emulator_binary = emulator_dir / "emulator"
    emulator_binary.write_text("", encoding="utf-8")

    def runner(argv, *, input_text=None, timeout=600.0):
        if argv[:2] == [sdkmanager, "--list_installed"]:
            return _proc(
                returncode=0, stdout="system-images;android-35;google_apis;arm64-v8a\n"
            )
        if argv[0] == avdmanager:
            return _proc(returncode=0)
        if argv == [str(emulator_binary), "-list-avds"]:
            return _proc(returncode=0, stdout="my-new-avd\n")
        return _proc(returncode=0)

    result = create_avd(
        name="my-new-avd",
        config={
            "android_home": str(tmp_path),
            "avd_home": str(tmp_path),
            "emulator_path": str(emulator_binary),
        },
        abi="arm64-v8a",
        runner=runner,
    )
    assert result["verified"] is True
    assert result["verification_method"] == "emulator -list-avds"


def test_create_avd_missing_cmdline_tools_raises(tmp_path) -> None:
    with pytest.raises(AvdError) as excinfo:
        create_avd(name="my-avd", config={"android_home": str(tmp_path)})
    assert "sdkmanager" in excinfo.value.extra["missing_tools"]
    assert "avdmanager" in excinfo.value.extra["missing_tools"]


def test_create_avd_already_exists_guard(tmp_path) -> None:
    _cmdline_tools(tmp_path)
    (tmp_path / "my-avd.ini").write_text("", encoding="utf-8")

    with pytest.raises(AvdError) as excinfo:
        create_avd(
            name="my-avd",
            config={"android_home": str(tmp_path), "avd_home": str(tmp_path)},
        )
    assert "already exists" in str(excinfo.value)
    assert "my-avd" in excinfo.value.extra["existing_avds"]


def test_create_avd_force_allows_overwrite(tmp_path) -> None:
    sdkmanager, avdmanager = _cmdline_tools(tmp_path)
    (tmp_path / "my-avd.ini").write_text("", encoding="utf-8")

    captured_argv: list[list[str]] = []

    def runner(argv, *, input_text=None, timeout=600.0):
        captured_argv.append(argv)
        if argv[:2] == [sdkmanager, "--list_installed"]:
            return _proc(
                returncode=0, stdout="system-images;android-35;google_apis;arm64-v8a\n"
            )
        return _proc(returncode=0)

    result = create_avd(
        name="my-avd",
        config={"android_home": str(tmp_path), "avd_home": str(tmp_path)},
        abi="arm64-v8a",
        force=True,
        runner=runner,
    )
    assert result["verified"] is True
    avdmanager_call = next(argv for argv in captured_argv if argv[0] == avdmanager)
    assert "--force" in avdmanager_call


def test_create_avd_license_not_accepted_raises_and_never_installs(tmp_path) -> None:
    sdkmanager, _avdmanager = _cmdline_tools(tmp_path)

    install_invoked = {"called": False}

    def runner(argv, *, input_text=None, timeout=600.0):
        if argv[:2] == [sdkmanager, "--list_installed"]:
            return _proc(returncode=0, stdout="")  # nothing installed
        if argv[0] == sdkmanager and len(argv) > 1 and "system-images" in argv[1]:
            install_invoked["called"] = True
            return _proc(returncode=0)
        return _proc(returncode=0)

    with pytest.raises(AvdError) as excinfo:
        create_avd(
            name="my-avd",
            config={"android_home": str(tmp_path), "avd_home": str(tmp_path)},
            abi="arm64-v8a",
            accept_licenses=False,
            runner=runner,
        )

    assert install_invoked["called"] is False
    assert "accept_licenses" in str(excinfo.value)
    assert "image_package" in excinfo.value.extra


def test_create_avd_accept_licenses_installs_image(tmp_path) -> None:
    sdkmanager, avdmanager = _cmdline_tools(tmp_path)

    install_calls: list[list[str]] = []

    def runner(argv, *, input_text=None, timeout=600.0):
        if argv[:2] == [sdkmanager, "--list_installed"]:
            return _proc(returncode=0, stdout="")
        if argv[0] == sdkmanager:
            install_calls.append(argv)
            assert input_text is not None  # license acceptance was piped
            return _proc(returncode=0)
        if argv[0] == avdmanager:
            (tmp_path / "my-avd.ini").write_text("", encoding="utf-8")
            return _proc(returncode=0)
        return _proc(returncode=0)

    result = create_avd(
        name="my-avd",
        config={"android_home": str(tmp_path), "avd_home": str(tmp_path)},
        abi="arm64-v8a",
        accept_licenses=True,
        runner=runner,
    )
    assert result["downloaded"] is True
    assert len(install_calls) == 1


def test_create_avd_post_create_verification_failure_raises(tmp_path) -> None:
    sdkmanager, _avdmanager = _cmdline_tools(tmp_path)
    # Deliberately do NOT create my-avd.ini -- verification will fail even
    # though avdmanager "succeeds" (exit 0).

    def runner(argv, *, input_text=None, timeout=600.0):
        if argv[:2] == [sdkmanager, "--list_installed"]:
            return _proc(
                returncode=0, stdout="system-images;android-35;google_apis;arm64-v8a\n"
            )
        return _proc(returncode=0)

    with pytest.raises(AvdError) as excinfo:
        create_avd(
            name="my-avd",
            config={"android_home": str(tmp_path), "avd_home": str(tmp_path)},
            abi="arm64-v8a",
            runner=runner,
        )
    assert "verification" in str(excinfo.value)
    assert excinfo.value.extra["name"] == "my-avd"


def test_create_avd_sdkmanager_install_failure_raises(tmp_path) -> None:
    sdkmanager, _avdmanager = _cmdline_tools(tmp_path)

    def runner(argv, *, input_text=None, timeout=600.0):
        if argv[:2] == [sdkmanager, "--list_installed"]:
            return _proc(returncode=0, stdout="")
        if argv[0] == sdkmanager:
            return _proc(returncode=1, stderr="disk full")
        return _proc(returncode=0)

    with pytest.raises(AvdError, match="disk full"):
        create_avd(
            name="my-avd",
            config={"android_home": str(tmp_path), "avd_home": str(tmp_path)},
            abi="arm64-v8a",
            accept_licenses=True,
            runner=runner,
        )


def test_create_avd_avdmanager_create_failure_raises(tmp_path) -> None:
    sdkmanager, avdmanager = _cmdline_tools(tmp_path)

    def runner(argv, *, input_text=None, timeout=600.0):
        if argv[:2] == [sdkmanager, "--list_installed"]:
            return _proc(
                returncode=0, stdout="system-images;android-35;google_apis;arm64-v8a\n"
            )
        if argv[0] == avdmanager:
            return _proc(returncode=1, stderr="permission denied")
        return _proc(returncode=0)

    with pytest.raises(AvdError, match="permission denied"):
        create_avd(
            name="my-avd",
            config={"android_home": str(tmp_path), "avd_home": str(tmp_path)},
            abi="arm64-v8a",
            runner=runner,
        )
