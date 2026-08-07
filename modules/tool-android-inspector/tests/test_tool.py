"""Sanity tests for the AndroidInspectorTool dispatch surface itself — name,
schema shape, and error handling for missing/unknown operations. No device
or emulator dependency (these never reach adb resolution)."""

from __future__ import annotations

from pathlib import Path

import amplifier_module_tool_android_inspector as pkg
import pytest
from amplifier_module_tool_android_inspector import (
    AndroidInspectorState,
    AndroidInspectorTool,
)
from amplifier_module_tool_android_inspector.leases import AvdLeaseError


@pytest.fixture
def tool() -> AndroidInspectorTool:
    return AndroidInspectorTool()


def test_tool_name(tool: AndroidInspectorTool) -> None:
    assert tool.name == "android_inspector"


def test_input_schema_declares_all_operations(tool: AndroidInspectorTool) -> None:
    schema = tool.input_schema
    op_enum = schema["properties"]["operation"]["enum"]
    expected = {
        "list_devices",
        "start_emulator",
        "stop_emulator",
        "doctor",
        "create_avd",
        "install",
        "launch",
        "stop_app",
        "screenshot",
        "ui_dump",
        "find",
        "logcat",
        "tap",
        "tap_xy",
        "type_text",
        "key",
        "swipe",
        "wait_for",
        "dismiss_anr",
    }
    assert set(op_enum) == expected
    assert schema["required"] == ["operation"]


def test_input_schema_declares_port_parameter(tool: AndroidInspectorTool) -> None:
    """Defect 1 regression guard: 'port' must be a declared parameter, not
    silently accepted-and-ignored. See test_avd.py for start_emulator's use
    of it (validation, argv threading, exact-serial wait)."""
    schema = tool.input_schema
    assert schema["properties"]["port"]["type"] == "integer"


async def test_execute_missing_operation_errors(tool: AndroidInspectorTool) -> None:
    result = await tool.execute({})
    assert result["success"] is False
    assert "operation" in result["error"]


async def test_execute_unknown_operation_errors(tool: AndroidInspectorTool) -> None:
    result = await tool.execute({"operation": "not_a_real_op"})
    assert result["success"] is False
    assert "Unknown operation" in result["error"]


async def test_execute_tap_missing_selector_errors(tool: AndroidInspectorTool) -> None:
    result = await tool.execute({"operation": "tap"})
    assert result["success"] is False
    assert "selector" in result["error"]


async def test_execute_install_missing_apk_path_errors(
    tool: AndroidInspectorTool,
) -> None:
    result = await tool.execute({"operation": "install"})
    assert result["success"] is False
    assert "apk_path" in result["error"]


async def test_execute_start_emulator_missing_avd_errors(
    tool: AndroidInspectorTool,
) -> None:
    result = await tool.execute({"operation": "start_emulator"})
    assert result["success"] is False
    assert "avd" in result["error"]


async def test_execute_swipe_missing_coords_errors(tool: AndroidInspectorTool) -> None:
    result = await tool.execute({"operation": "swipe"})
    assert result["success"] is False
    assert "x1" in result["error"]


async def test_execute_create_avd_missing_name_errors(
    tool: AndroidInspectorTool,
) -> None:
    result = await tool.execute({"operation": "create_avd"})
    assert result["success"] is False
    assert "name" in result["error"]


async def test_execute_doctor_never_fails_and_returns_full_report(
    tool: AndroidInspectorTool,
) -> None:
    # doctor performs real (but read-only / non-destructive) host checks --
    # it must never raise or return success=False regardless of what it
    # finds on this particular host.
    result = await tool.execute({"operation": "doctor"})
    assert result["success"] is True
    assert isinstance(result["ready"], bool)
    assert isinstance(result["checks"], list)
    assert len(result["checks"]) == 10
    assert isinstance(result["summary"], str)
    for check in result["checks"]:
        assert check["status"] in ("ok", "warn", "fail")


def test_input_schema_force_description_covers_stop_emulator(
    tool: AndroidInspectorTool,
) -> None:
    """Defect 3: 'force' is shared between create_avd and stop_emulator --
    its description must document both uses, not just the original one."""
    schema = tool.input_schema
    description = schema["properties"]["force"]["description"]
    assert "stop_emulator" in description
    assert "create_avd" in description


def test_input_schema_port_description_covers_auto_allocation(
    tool: AndroidInspectorTool,
) -> None:
    """Defect 2: 'port' is now sometimes auto-allocated -- the schema must
    say so, not just describe the explicit-port behaviour."""
    schema = tool.input_schema
    description = schema["properties"]["port"]["description"]
    assert "allocated" in description.lower()
    assert "port_allocation_fallback" in description


# ---------------------------------------------------------------------------
# Defect 4 -- mount()/config is per-instance, not a module-level singleton.
# ---------------------------------------------------------------------------


class _FakeCoordinator:
    async def mount(self, kind, obj, *, name):
        return None


async def test_mount_builds_independent_state_per_call(tmp_path: Path) -> None:
    """The measured Defect 4 scenario: three `mount()` calls in one process
    with different `work_dir`s must each honour their OWN config -- not all
    silently write to the first mount's directory."""
    work_dir_1 = tmp_path / "at-c1"
    work_dir_2 = tmp_path / "at-c2"
    work_dir_3 = tmp_path / "at-c3"
    coordinator = _FakeCoordinator()

    tool1 = await pkg.mount(coordinator, {"work_dir": str(work_dir_1)})
    tool2 = await pkg.mount(coordinator, {"work_dir": str(work_dir_2)})
    tool3 = await pkg.mount(coordinator, {"work_dir": str(work_dir_3)})

    state1 = tool1._get_state()
    state2 = tool2._get_state()
    state3 = tool3._get_state()

    assert state1.base_dir == work_dir_1
    assert state2.base_dir == work_dir_2
    assert state3.base_dir == work_dir_3
    assert work_dir_1.exists()
    assert work_dir_2.exists()
    assert work_dir_3.exists()
    # Each mount's own state is a genuinely distinct object.
    assert state1 is not state2 is not state3


async def test_mount_with_no_config_defaults_independently() -> None:
    """Two mounts with no config at all must still be independent instances
    (not sharing a module-level default either)."""
    coordinator = _FakeCoordinator()
    tool_a = await pkg.mount(coordinator)
    tool_b = await pkg.mount(coordinator)
    assert tool_a is not tool_b
    assert tool_a._config is not tool_b._config


# ---------------------------------------------------------------------------
# Defect 3 -- stop_emulator's `force` flag and ownership-refusal surfacing.
# ---------------------------------------------------------------------------


def _state_with_fake_adb(
    tmp_path: Path, config: dict | None = None
) -> AndroidInspectorState:
    base_dir = tmp_path / "sessions"
    run_dir = base_dir / "_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return AndroidInspectorState(
        config=config or {}, base_dir=base_dir, run_dir=run_dir, _adb_path="/fake/adb"
    )


async def test_stop_emulator_passes_force_and_lease_dir_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_stop_emulator_impl(client, *, pid=None, force=False, lease_dir=None):
        captured["force"] = force
        captured["lease_dir"] = lease_dir
        captured["serial"] = client.serial
        return {"emu_kill_ok": True, "process_reaped": True}

    monkeypatch.setattr(pkg, "_stop_emulator_impl", fake_stop_emulator_impl)

    tool = pkg.AndroidInspectorTool()
    # `tool._state` is seeded directly (bypassing the lazy _get_state()
    # build), so the lease_dir override belongs on the STATE's config, not
    # the tool's own __init__ config, which is never consulted once _state
    # is already set.
    tool._state = _state_with_fake_adb(
        tmp_path, config={"lease_dir": str(tmp_path / "leases")}
    )

    result = await tool.execute(
        {"operation": "stop_emulator", "serial": "emulator-5554", "force": True}
    )

    assert result["success"] is True
    assert captured["force"] is True
    assert captured["serial"] == "emulator-5554"
    assert captured["lease_dir"] == tmp_path / "leases"


async def test_stop_emulator_surfaces_avd_lease_error_with_structured_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AvdLeaseError's `.extra` (owner_pid, avd, ...) must reach the caller
    intact via execute()'s exception cascade -- same pattern as AvdError/
    LaunchError elsewhere in this module."""

    def fake_stop_emulator_impl(client, *, pid=None, force=False, lease_dir=None):
        raise AvdLeaseError(
            "Refusing to stop: leased by a DIFFERENT live process (pid 999999)",
            serial=client.serial,
            owner_pid=999999,
            avd="their-avd",
        )

    monkeypatch.setattr(pkg, "_stop_emulator_impl", fake_stop_emulator_impl)

    tool = pkg.AndroidInspectorTool()
    tool._state = _state_with_fake_adb(tmp_path)

    result = await tool.execute(
        {"operation": "stop_emulator", "serial": "emulator-5554"}
    )

    assert result["success"] is False
    assert result["owner_pid"] == 999999
    assert result["avd"] == "their-avd"
    assert "999999" in result["error"]
