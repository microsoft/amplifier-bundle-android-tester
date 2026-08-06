"""Unit tests for evidence.py — collision-proof per-invocation file naming
(Defect 2). No adb/device dependency; pure filesystem + monkeypatched
datetime/secrets.
"""

from __future__ import annotations

import datetime as dt_module
from pathlib import Path

from amplifier_module_tool_android_inspector import evidence
from amplifier_module_tool_android_inspector.evidence import unique_evidence_path


def test_unique_evidence_path_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    existing = tmp_path / "screenshot_20260101T000000_000000_aaaa.png"
    existing.write_bytes(b"already here")

    path = unique_evidence_path(tmp_path, "screenshot", "png")

    assert path != existing
    assert not path.exists()
    path.write_bytes(b"new content")

    # The pre-existing file was never touched.
    assert existing.read_bytes() == b"already here"


def test_unique_evidence_path_retries_on_forced_collision(
    monkeypatch, tmp_path: Path
) -> None:
    """Force the first candidate name to already exist; the function must
    retry with a different suffix rather than ever returning (or letting a
    caller write to) an existing path."""

    class _FrozenDatetime(dt_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt_module.datetime(2026, 8, 6, 8, 15, 30, 123456, tzinfo=tz)

    monkeypatch.setattr(evidence, "datetime", _FrozenDatetime)

    suffixes = iter(["aaaa", "aaaa", "bbbb"])
    monkeypatch.setattr(evidence.secrets, "token_hex", lambda n: next(suffixes))

    colliding = tmp_path / "screenshot_20260806T081530_123456_aaaa.png"
    colliding.write_bytes(b"pre-existing evidence, must not be overwritten")

    path = unique_evidence_path(tmp_path, "screenshot", "png")

    assert path.name == "screenshot_20260806T081530_123456_bbbb.png"
    assert colliding.read_bytes() == b"pre-existing evidence, must not be overwritten"


def test_unique_evidence_path_exhausts_attempts_raises(
    monkeypatch, tmp_path: Path
) -> None:
    class _FrozenDatetime(dt_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt_module.datetime(2026, 8, 6, 8, 15, 30, 123456, tzinfo=tz)

    monkeypatch.setattr(evidence, "datetime", _FrozenDatetime)
    monkeypatch.setattr(evidence.secrets, "token_hex", lambda n: "aaaa")

    (tmp_path / "screenshot_20260806T081530_123456_aaaa.png").write_bytes(b"x")

    import pytest

    with pytest.raises(RuntimeError, match="Could not construct a unique"):
        unique_evidence_path(tmp_path, "screenshot", "png", max_attempts=3)


def test_unique_evidence_path_filenames_sort_chronologically(
    monkeypatch, tmp_path: Path
) -> None:
    times = iter(
        [
            dt_module.datetime(
                2026, 8, 6, 8, 15, 30, 100000, tzinfo=dt_module.timezone.utc
            ),
            dt_module.datetime(
                2026, 8, 6, 8, 15, 30, 200000, tzinfo=dt_module.timezone.utc
            ),
            dt_module.datetime(2026, 8, 6, 8, 15, 31, 0, tzinfo=dt_module.timezone.utc),
        ]
    )

    class _SeqDatetime(dt_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times)

    monkeypatch.setattr(evidence, "datetime", _SeqDatetime)

    names = [unique_evidence_path(tmp_path, "screenshot", "png").name for _ in range(3)]
    # Write a file each time so consecutive calls don't collide with an
    # earlier candidate under the frozen/sequential clock.
    for name in names:
        (tmp_path / name).write_bytes(b"x")

    assert names == sorted(names)


def test_unique_evidence_path_is_absolute_when_out_dir_absolute(tmp_path: Path) -> None:
    path = unique_evidence_path(tmp_path, "screenshot", "png")
    assert path.is_absolute()


def test_unique_evidence_path_includes_index_for_readability_only(
    tmp_path: Path,
) -> None:
    path = unique_evidence_path(tmp_path, "screenshot", "png", index=7)
    assert "_0007.png" in path.name


def test_unique_evidence_path_two_independent_calls_never_collide(
    tmp_path: Path,
) -> None:
    """The core Defect 2 regression: two calls with a *fresh* in-process
    index each time (simulating two separate processes, each starting its
    counter at 1) must not produce the same path."""
    path_a = unique_evidence_path(tmp_path, "screenshot", "png", index=1)
    path_a.write_bytes(b"process A's screenshot")

    path_b = unique_evidence_path(tmp_path, "screenshot", "png", index=1)
    path_b.write_bytes(b"process B's screenshot")

    assert path_a != path_b
    assert path_a.read_bytes() == b"process A's screenshot"
    assert path_b.read_bytes() == b"process B's screenshot"
