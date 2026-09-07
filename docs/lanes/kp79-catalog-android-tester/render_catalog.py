#!/usr/bin/env python3
"""Render the delegate agent catalog from a scratch session.

Loads a scratch bundle (tool-delegate + this repo's android-tester behavior,
from a caller-supplied checkout), builds a real AmplifierSession, and writes
out the delegate tool's live `description` -- exactly the bytes injected into
every session's head on every turn.

No prompt is executed, so no LLM call is made. Cost: $0.

    usage: render_catalog.py <scratch-bundle-uri-or-path> <out-path>
"""

import asyncio
import sys
from pathlib import Path


def _tool_candidates(session):
    """Every mounted tool instance, from the coordinator's `tools` mount point."""
    mounted = session.coordinator.get("tools")
    if mounted is None:
        return []
    if hasattr(mounted, "values"):
        return list(mounted.values())
    return list(mounted)


async def main(bundle_uri: str, out_path: str) -> int:
    from amplifier_app_cli.paths import create_session_from_bundle

    session = await create_session_from_bundle(bundle_uri, install_deps=False)
    async with session:
        candidates = _tool_candidates(session)
        if not candidates:
            print("ERROR: could not locate tool registry on session", file=sys.stderr)
            print("session attrs: " + ", ".join(dir(session)), file=sys.stderr)
            return 2

        delegate = next(
            (t for t in candidates if getattr(t, "name", None) == "delegate"), None
        )
        if delegate is None:
            names = ", ".join(str(getattr(t, "name", t)) for t in candidates)
            print(f"ERROR: delegate tool not found. Tools: {names}", file=sys.stderr)
            return 3

        desc = delegate.description
        Path(out_path).write_text(desc, encoding="utf-8")
        print(f"WROTE {out_path} bytes={len(desc.encode('utf-8'))} chars={len(desc)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: render_catalog.py <bundle-uri> <out-path>", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(asyncio.run(main(sys.argv[1], sys.argv[2])))
