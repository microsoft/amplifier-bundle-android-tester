"""Regression tests for Defect 2 — screenshot filenames must not collide
across independently-constructed state (simulating separate processes,
since `_screenshot_counters` is in-memory and resets to 0 in each new
process).

Exercises `AndroidInspectorTool._screenshot` directly (not through
`execute()`) against a real `AdbClient` whose `screencap_bytes()` is
monkeypatched at the `subprocess.run` boundary, same technique as
`test_adb.py`'s screencap tests.
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
    """A brand-new `AndroidInspectorState` with its own zeroed
    `_screenshot_counters` — exactly what a fresh process would construct."""
    base_dir = tmp_path / "sessions"
    run_dir = base_dir / "_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return AndroidInspectorState(
        config={},
        base_dir=base_dir,
        run_dir=run_dir,
        _adb_path=ADB_PATH,  # skip resolve_adb_binary — no subprocess needed
    )


def _fake_png(filler: bytes) -> bytes:
    header = b"\x89PNG\r\n\x1a\n"
    return header + filler * 2000  # comfortably over MIN_SCREENSHOT_BYTES (1024)


def _patch_screencap(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    class FakeCompleted:
        returncode = 0
        stdout = data
        stderr = b""

    def fake_run(argv, capture_output, timeout, check):
        return FakeCompleted()

    monkeypatch.setattr(
        "amplifier_module_tool_android_inspector.adb.subprocess.run", fake_run
    )


def test_screenshots_from_independently_constructed_states_never_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = AndroidInspectorTool()

    # --- "Process A": fresh state, screenshot #1 ---
    state_a = _fresh_state(tmp_path)
    _patch_screencap(monkeypatch, _fake_png(b"\x01"))
    result_a = tool._screenshot(state_a, {"serial": SERIAL})
    assert result_a["success"] is True

    # --- "Process B": a completely separate state object, ALSO its first
    # screenshot (counter also starts at 1) — this is exactly the scenario
    # that silently clobbered evidence before the fix.
    state_b = _fresh_state(tmp_path)
    _patch_screencap(monkeypatch, _fake_png(b"\x02"))
    result_b = tool._screenshot(state_b, {"serial": SERIAL})
    assert result_b["success"] is True

    path_a = Path(result_a["image_path"])
    path_b = Path(result_b["image_path"])

    assert path_a != path_b, "two independent processes' screenshots collided"
    assert path_a.exists()
    assert path_b.exists()
    # Neither call overwrote the other's evidence.
    assert path_a.read_bytes() == _fake_png(b"\x01")
    assert path_b.read_bytes() == _fake_png(b"\x02")


def test_screenshot_image_path_is_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = AndroidInspectorTool()
    state = _fresh_state(tmp_path)
    _patch_screencap(monkeypatch, _fake_png(b"\x03"))
    result = tool._screenshot(state, {"serial": SERIAL})
    assert Path(result["image_path"]).is_absolute()


def test_screenshot_never_overwrites_an_existing_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even within the SAME process/state, an existing file at the chosen
    path (e.g. a pre-existing collision) must never be overwritten."""
    tool = AndroidInspectorTool()
    state = _fresh_state(tmp_path)

    out_dir = state.base_dir / SERIAL
    out_dir.mkdir(parents=True, exist_ok=True)

    _patch_screencap(monkeypatch, _fake_png(b"\x04"))
    result = tool._screenshot(state, {"serial": SERIAL})
    first_path = Path(result["image_path"])
    original_bytes = first_path.read_bytes()

    # Simulate a collision: pre-create a file at a plausible next path by
    # writing garbage into the directory with a name that looks like a
    # legitimate evidence file, then take another screenshot and confirm the
    # earlier file is untouched.
    decoy = out_dir / "screenshot_00000101T000000_000000_dead.png"
    decoy.write_bytes(b"decoy, must survive")

    _patch_screencap(monkeypatch, _fake_png(b"\x05"))
    result2 = tool._screenshot(state, {"serial": SERIAL})
    second_path = Path(result2["image_path"])

    assert second_path != decoy
    assert decoy.read_bytes() == b"decoy, must survive"
    assert first_path.read_bytes() == original_bytes
    assert second_path.read_bytes() == _fake_png(b"\x05")
