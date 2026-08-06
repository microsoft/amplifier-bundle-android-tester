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
