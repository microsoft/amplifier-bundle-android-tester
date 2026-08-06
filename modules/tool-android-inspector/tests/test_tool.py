"""Sanity tests for the AndroidInspectorTool dispatch surface itself — name,
schema shape, and error handling for missing/unknown operations. No device
or emulator dependency (these never reach adb resolution)."""

from __future__ import annotations

import pytest
from amplifier_module_tool_android_inspector import AndroidInspectorTool


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
