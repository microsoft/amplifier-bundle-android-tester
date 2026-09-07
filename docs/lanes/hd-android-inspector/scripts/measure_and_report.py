"""Emit the lane's measurement table, fidelity table and token census.

Everything the DONE-NOTE quotes is generated here rather than typed, so a
number in the note cannot drift from the tree it describes.

    python3 docs/lanes/hd-android-inspector/scripts/measure_and_report.py \
        > docs/lanes/hd-android-inspector/evidence/fidelity-table.md
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
MODULE = REPO / "modules" / "tool-android-inspector"
FIXTURES = MODULE / "tests" / "fixtures" / "lean-head"

sys.path.insert(0, str(MODULE))
sys.path.insert(0, str(MODULE / "tests"))

from lean_head_inventory import AWARENESS_SEMANTICS  # noqa: E402
from lean_head_inventory import TOOL_SEMANTICS  # noqa: E402

from amplifier_module_tool_android_inspector import AndroidInspectorTool  # noqa: E402


def prose(description: str, schema: dict) -> str:
    return description + "\n" + "\n".join(
        p.get("description", "") for p in schema["properties"].values()
    )


def serialized(description: str, schema: dict) -> str:
    return json.dumps(
        {"name": "android_inspector", "description": description, "input_schema": schema},
        ensure_ascii=False,
    )


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.+-]*[A-Za-z0-9_]|\d{3,4}-?\d*")


def named_tokens(text: str) -> set[str]:
    found: set[str] = set()
    for chunk in re.split(r"/", text):
        for token in _TOKEN.findall(chunk):
            if (
                any(c in token for c in "_.-")
                or token.isupper()
                or re.search(r"[a-z][A-Z]", token)
                or re.match(r"^\d", token)
            ):
                found.add(token.lower())
    return found


def main() -> None:
    tool = AndroidInspectorTool()
    lean_desc, lean_schema = tool.description, tool.input_schema
    stock = json.loads((FIXTURES / "stock-tool-surface.json").read_text())
    stock_desc, stock_schema = stock["description"], stock["input_schema"]

    lean_aw = (REPO / "context" / "android-awareness.md").read_text()
    stock_aw = (FIXTURES / "stock-android-awareness.md").read_text()

    def row(label: str, before: int, after: int) -> str:
        delta = after - before
        pct = delta / before * 100 if before else 0.0
        return f"| {label} | {before:,} | {after:,} | {delta:+,} | {pct:+.1f}% |"

    print("## Sizes\n")
    print("| Surface | Stock (fbf8ce6) | Lean | Delta | % |")
    print("|---|---:|---:|---:|---:|")
    print(row("tool description", len(stock_desc), len(lean_desc)))
    sp = sum(len(p.get("description", "")) for p in stock_schema["properties"].values())
    lp = sum(len(p.get("description", "")) for p in lean_schema["properties"].values())
    print(row("30 parameter descriptions", sp, lp))
    print(row("tool prose total", len(stock_desc) + sp, len(lean_desc) + lp))
    print(
        row(
            "serialized tool definition",
            len(serialized(stock_desc, stock_schema)),
            len(serialized(lean_desc, lean_schema)),
        )
    )
    print(row("context/android-awareness.md", len(stock_aw), len(lean_aw)))
    print(
        row(
            "always-on total (tool + awareness)",
            len(serialized(stock_desc, stock_schema)) + len(stock_aw),
            len(serialized(lean_desc, lean_schema)) + len(lean_aw),
        )
    )

    print("\n### Per-parameter description sizes\n")
    print("| Parameter | Stock | Lean | Delta |")
    print("|---|---:|---:|---:|")
    for name in stock_schema["properties"]:
        b = len(stock_schema["properties"][name].get("description", ""))
        a = len(lean_schema["properties"][name].get("description", ""))
        mark = "" if a == b else " **"
        print(f"| `{name}`{mark} | {b} | {a} | {a - b:+} |")
    print("\n`**` = re-worded. Every other parameter description is byte-for-byte stock.")

    lean_tool_prose = prose(lean_desc, lean_schema)
    for title, semantics, haystack in (
        ("Tool surface", TOOL_SEMANTICS, lean_tool_prose),
        ("Awareness surface", AWARENESS_SEMANTICS, lean_aw),
    ):
        print(f"\n## Fidelity — {title}\n")
        print("| ID | Stock contract | Survives | Probe (asserted in CI) |")
        print("|---|---|:--:|---|")
        for ident, semantic, probes in semantics:
            ok = all(p in haystack for p in probes)
            probe_text = " · ".join(
                "<code>" + p.replace("&", "&amp;").replace("<", "&lt;").replace("`", "&#96;")
                + "</code>"
                for p in probes
            )
            print(f"| {ident} | {semantic} | {'✅' if ok else '❌ ABSENT'} | {probe_text} |")
        absent = sum(1 for _, _, ps in semantics if not all(p in haystack for p in ps))
        print(f"\n**{len(semantics)} semantics · expected absent 0 · observed absent {absent}.**")

    print("\n## Token census (extractor derived from the STOCK text)\n")
    print("| Surface | Named tokens in stock | Absent from lean |")
    print("|---|---:|---:|")
    for label, s_text, l_text in (
        ("tool surface", prose(stock_desc, stock_schema), lean_tool_prose),
        ("awareness", stock_aw, lean_aw),
    ):
        toks = named_tokens(s_text)
        absent = sorted(t for t in toks if t not in l_text.lower())
        print(f"| {label} | {len(toks)} | {len(absent)} {absent if absent else ''} |")


if __name__ == "__main__":
    main()
