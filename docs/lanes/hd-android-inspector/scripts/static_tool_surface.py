"""Read the android_inspector tool surface STATICALLY, from module source.

The surface a provider is handed is exactly ``name`` + ``description`` +
``input_schema`` off ``AndroidInspectorTool``. This module has **zero runtime
dependencies** (``pyproject.toml``: ``dependencies = []``), so those three
properties can be read by importing the package directly out of a source tree
-- no Amplifier process, no bundle mount, no ``AMPLIFIER_HOME``.

That restriction is not stylistic. Rendering via a scratch ``AMPLIFIER_HOME``
on the host is FORBIDDEN in this repo: the CLI's first run editable-installs
into the *shared* uv tool venv regardless of ``AMPLIFIER_HOME``, so a scratch
home's first render silently rewrites the real install's ``_editable_impl_*.pth``
files. On 2026-09-07 that rewrote 61 of 63 of them and had to be repaired by
hand. If a live-mount render is ever genuinely required, it runs inside a DTU
-- never on a developer host.

Usage:

    # after: the working tree
    python3 static_tool_surface.py --out after.json

    # before: any git revision, extracted read-only into a temp dir
    python3 static_tool_surface.py --rev fbf8ce6 --out before.json

Writes ``{"name", "description", "input_schema"}`` as JSON to ``--out`` and a
one-line size census to stderr.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
PACKAGE_ROOT = REPO / "modules" / "tool-android-inspector"
PACKAGE = "amplifier_module_tool_android_inspector"


def surface_from(package_root: Path) -> dict:
    """Import the tool out of ``package_root`` and read its three properties."""
    sys.path.insert(0, str(package_root))
    module = __import__(PACKAGE)
    tool = module.AndroidInspectorTool()
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def surface_at_revision(rev: str) -> dict:
    """Extract ``package_root`` at ``rev`` into a temp dir and read it there.

    A separate interpreter, because the historical and current copies share a
    package name and only one can own that name per process.
    """
    with tempfile.TemporaryDirectory(prefix="android-surface-") as tmp:
        archive = subprocess.run(
            ["git", "archive", rev, f"modules/tool-android-inspector/{PACKAGE}"],
            cwd=REPO,
            check=True,
            capture_output=True,
        ).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
        extracted = Path(tmp) / "modules" / "tool-android-inspector"
        out = subprocess.run(
            [sys.executable, __file__, "--package-root", str(extracted), "--out", "-"],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(out.stdout)


def census(surface: dict) -> str:
    description = surface["description"]
    params = surface["input_schema"]["properties"]
    param_chars = sum(len(p.get("description", "")) for p in params.values())
    serialized = json.dumps(surface, ensure_ascii=False)
    return (
        f"serialized_tool_definition_chars={len(serialized)} "
        f"description_chars={len(description)} "
        f"param_description_chars={param_chars} "
        f"param_count={len(params)} "
        f"prose_total_chars={len(description) + param_chars}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rev", help="git revision to read (default: the working tree)")
    ap.add_argument("--package-root", help="explicit source root to import from")
    ap.add_argument("--out", required=True, help="output path, or '-' for stdout")
    args = ap.parse_args()

    if args.rev:
        surface = surface_at_revision(args.rev)
    else:
        surface = surface_from(Path(args.package_root or PACKAGE_ROOT))

    text = json.dumps(surface, indent=2, ensure_ascii=False) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
        print(census(surface), file=sys.stderr)


if __name__ == "__main__":
    main()
