#!/usr/bin/env python3
"""Compare two rendered delegate agent catalogs and report the bytes saved.

    usage: measure_catalog.py <before.txt> <after.txt> [name-prefix]

Splits each catalog's `Available agents:` section into per-agent rows (a row
runs from one `  - <name>: ` line to the next), then reports total catalog
bytes, per-row bytes for rows matching the optional name prefix, and the
delta. Pure text arithmetic -- no model call.
"""

import sys
from pathlib import Path

MARKER = "Available agents:"
ROW = "  - "


def rows(catalog: str) -> dict[str, str]:
    lines = catalog.split("\n")
    if MARKER not in lines:
        return {}
    out: dict[str, str] = {}
    name: str | None = None
    buf: list[str] = []
    for line in lines[lines.index(MARKER) + 1 :]:
        if line.startswith(ROW):
            if name is not None:
                out[name] = "\n".join(buf)
            name = line[len(ROW) :].split(":", 2)[0:2]
            name = ":".join(name) if isinstance(name, list) else name
            buf = [line]
        elif name is not None:
            buf.append(line)
    if name is not None:
        out[name] = "\n".join(buf)
    return out


def nbytes(text: str) -> int:
    # +1 for the newline that joins this row to the next one in the catalog
    return len(text.encode("utf-8")) + 1


def main(before_path: str, after_path: str, prefix: str = "") -> int:
    before = Path(before_path).read_text(encoding="utf-8")
    after = Path(after_path).read_text(encoding="utf-8")
    b_rows, a_rows = rows(before), rows(after)

    tb, ta = len(before.encode("utf-8")), len(after.encode("utf-8"))
    print(f"WHOLE delegate tool description: {tb} B -> {ta} B  (saved {tb - ta} B, {100 * (tb - ta) / tb:.1f}%)")
    print(f"agent rows: before {len(b_rows)}, after {len(a_rows)}")
    print()

    names = sorted(set(b_rows) | set(a_rows))
    sel = [n for n in names if n.startswith(prefix)] if prefix else names
    sb = sa = 0
    print(f"{'agent':44s} {'before B':>9s} {'after B':>9s} {'saved B':>9s}")
    for n in sel:
        x, y = nbytes(b_rows.get(n, "")) if n in b_rows else 0, nbytes(a_rows.get(n, "")) if n in a_rows else 0
        sb += x
        sa += y
        print(f"{n:44s} {x:9d} {y:9d} {x - y:9d}")
    print(f"{'TOTAL (selected rows)':44s} {sb:9d} {sa:9d} {sb - sa:9d}")

    if prefix:
        others_b = sum(nbytes(v) for k, v in b_rows.items() if not k.startswith(prefix))
        others_a = sum(nbytes(v) for k, v in a_rows.items() if not k.startswith(prefix))
        print()
        print(f"rows NOT matching {prefix!r}: {others_b} B -> {others_a} B (delta {others_b - others_a} B; expect 0)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        print(__doc__, file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(main(*sys.argv[1:]))
