"""Collision-proof filename generation for per-invocation evidence files.

The bug this exists to fix: an in-process counter (e.g. "screenshot_0001.png",
"screenshot_0002.png") is only unique *within one process's lifetime*. It
resets to zero every time a fresh process starts, so two separate tool
invocations (two separate `amplifier` sessions, two separate test runs, two
separate agent processes against the same device) silently overwrite each
other's evidence the moment they land on the same index. For a tool whose
entire purpose is producing visual/textual evidence another process reasons
over, that is a correctness bug, not a cosmetic one.

The fix: derive the filename from a UTC timestamp (second resolution) plus
microseconds plus a short random suffix, so names sort chronologically and
are vanishingly unlikely to collide even across independent processes racing
at the same instant -- and then verify that with an explicit existence check
before ever handing back a path, retrying on the rare collision rather than
overwriting. An optional in-process index may be appended purely for
human-readable ordering; it plays no role in collision prevention (that is
exactly the property this module fixes).

This is a best-effort check-then-return, not an atomic filesystem
reservation (no O_CREAT|O_EXCL). That is a deliberate simplicity trade-off:
this tool issues one blocking adb command at a time within a single process,
so genuine concurrent writers racing on the literal same path are not a
realistic threat here -- the timestamp+random naming is what makes collision
vanishingly unlikely in the first place; the existence check is the
belt-and-suspenders backstop, not the primary defense.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["unique_evidence_path"]


def unique_evidence_path(
    out_dir: Path,
    prefix: str,
    ext: str,
    *,
    index: int | None = None,
    max_attempts: int = 20,
) -> Path:
    """Build a path under `out_dir` guaranteed not to overwrite an existing file.

    Filename shape: `{prefix}_{YYYYMMDDTHHMMSS}_{microseconds:06d}_{4 hex
    chars}[_{index:04d}].{ext}` -- e.g.
    `screenshot_20260806T081530_123456_a1b2.png`. Sorts chronologically as
    plain strings.

    `index`, if given, is appended purely for human-readable ordering within
    a single process -- it is NOT relied on for collision prevention (an
    in-process counter resets every new process, which is exactly the bug
    this function fixes).

    Does not create `out_dir` -- callers are expected to have already
    ensured it exists.

    Raises:
        RuntimeError: if `max_attempts` consecutive candidates all already
            exist (should be statistically impossible with real entropy).
    """
    idx_part = f"_{index:04d}" if index is not None else ""
    for _ in range(max_attempts):
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%dT%H%M%S")
        micros = f"{now.microsecond:06d}"
        suffix = secrets.token_hex(2)
        candidate = out_dir / f"{prefix}_{ts}_{micros}_{suffix}{idx_part}.{ext}"
        if not candidate.exists():
            return candidate

    raise RuntimeError(
        f"Could not construct a unique evidence path under {out_dir} for "
        f"prefix {prefix!r} after {max_attempts} attempts -- this should be "
        "statistically impossible."
    )
