"""Tool-level test for Defect 1 -- the uiautomator-collision structured error
surfaces through `AndroidInspectorTool.execute()` with its `.extra` fields
intact (not collapsed to a bare message), exactly like `LaunchError`/
`AvdError` already do. Faked at the `subprocess.run` boundary, same
technique as `test_logcat_tool.py` / `test_screenshot.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import amplifier_module_tool_android_inspector as pkg
import pytest
from amplifier_module_tool_android_inspector import (
    AndroidInspectorState,
    AndroidInspectorTool,
)

ADB_PATH = "/opt/sdk/platform-tools/adb"
SERIAL = "emulator-5554"


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """`get_state()` caches a module-level singleton -- reset it around each
    test so one test's config/state can't leak into the next."""
    pkg._state = None
    pkg._state_config = {}
    yield
    pkg._state = None
    pkg._state_config = {}


async def test_execute_ui_dump_surfaces_collision_error_with_structured_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `uiautomator dump` attempt collides -- retries exhaust, and the
    caller sees WHY (serial, attempts, max_retries, last stderr) rather than
    a bare string."""

    def fake_run(argv, capture_output, timeout, check, text=True):
        sub = argv[3:]
        if sub and sub[0] == "shell" and str(sub[1]).startswith("uiautomator dump"):
            return _FakeCompleted(
                137,
                "",
                "java.lang.IllegalStateException: UiAutomationService "
                "0198 already registered!",
            )
        return _FakeCompleted(0, "", "")

    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.adb.subprocess.run", fake_run
    )

    # Bypass resolve_adb_binary (which requires the path to exist on disk)
    # by seeding the cached singleton state directly with `_adb_path`
    # pre-resolved -- same technique as test_screenshot.py's `_fresh_state`.
    base_dir = tmp_path / "sessions"
    run_dir = base_dir / "_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    pkg._state = AndroidInspectorState(
        config={}, base_dir=base_dir, run_dir=run_dir, _adb_path=ADB_PATH
    )

    tool = AndroidInspectorTool()
    result = await tool.execute({"operation": "ui_dump", "serial": SERIAL})

    assert result["success"] is False
    assert "competing uiautomator instance" in result["error"]
    assert result["serial"] == SERIAL
    assert result["max_retries"] == 3  # DEFAULT_DUMP_COLLISION_RETRIES
    assert result["attempts"] == 4  # initial + 3 retries
    assert "already registered" in result["last_stderr"]
