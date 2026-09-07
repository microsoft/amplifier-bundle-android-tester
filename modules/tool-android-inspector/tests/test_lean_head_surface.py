"""Guardrail for the always-on Android head surface.

Three things are pinned here, and they only mean something together:

1. **Budgets** (char ceilings on the tool description, the parameter
   descriptions, the serialized tool definition and the awareness context).
   These fail against the pre-change text -- they are what stops the surface
   quietly growing back.
2. **Fidelity** -- every contract the stock surface stated is still findable
   (`lean_head_inventory`), plus a stock-DERIVED token census so the check
   cannot be satisfied by an inventory written from the new text alone.
3. **Byte pins** -- the reviewed text itself, so any later edit shows up as a
   diff against a vendored copy rather than as a silent paraphrase.

A budget without fidelity would be met by deleting contracts; fidelity without
a budget would be met by never leaning anything. Both, plus the pins, is the
whole point.

Baseline: origin/main @ fbf8ce6 (`fixtures/lean-head/stock-*`).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from amplifier_module_tool_android_inspector import AndroidInspectorTool

from .lean_head_inventory import AWARENESS_SEMANTICS
from .lean_head_inventory import TOOL_SEMANTICS
from .lean_head_inventory import missing

FIXTURES = Path(__file__).parent / "fixtures" / "lean-head"
REPO_ROOT = Path(__file__).resolve().parents[3]
AWARENESS_PATH = REPO_ROOT / "context" / "android-awareness.md"
AGENTS_DIR = REPO_ROOT / "agents"

# Measured on this branch; every one of these fails against stock.
DESCRIPTION_BUDGET = 3044          # stock 3,619
PARAM_DESCRIPTION_BUDGET = 3293    # stock 3,398
SERIALIZED_BUDGET = 8417           # stock 9,097
AWARENESS_BUDGET = 2787            # stock 3,413
PARAM_CEILING = 600                # per parameter...
PARAM_CONTRACT_EXEMPTIONS = {"port": 710}  # ...except a named parameter contract

# `agents/` is NOT this lane's target: those descriptions were already made
# trigger-first (0 <example>, <=600 chars) upstream in 863afa1 and must be
# preserved byte-for-byte. sha256 of the raw frontmatter description block.
# Char counts are of the raw indented block (validate-agents reports the
# dedented body: 598 / 594 / 600, all <= its 600 ceiling).
AGENT_DESCRIPTION_PINS = {
    "android-debugger.md": ("09009b7ac45501c1", 631),
    "android-operator.md": ("389d838a789d4ebb", 627),
    "android-visual-tester.md": ("fec03757593edbb9", 633),
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def tool() -> AndroidInspectorTool:
    return AndroidInspectorTool()


def tool_prose(description: str, schema: dict) -> str:
    """Everything a session actually reads: description + every param doc."""
    params = "\n".join(
        p.get("description", "") for p in schema["properties"].values()
    )
    return description + "\n" + params


def serialized(description: str, schema: dict) -> str:
    return json.dumps(
        {
            "name": "android_inspector",
            "description": description,
            "input_schema": schema,
        },
        ensure_ascii=False,
    )


def stock_surface() -> dict:
    return json.loads((FIXTURES / "stock-tool-surface.json").read_text())


def agent_description(path: Path) -> str:
    text = path.read_text()
    frontmatter = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert frontmatter, f"{path.name}: no YAML frontmatter"
    block = re.search(
        r"^  description: \|\n((?:    .*\n|\n)+)", frontmatter.group(1), re.M
    )
    assert block, f"{path.name}: no block-scalar description"
    return block.group(1)


# --------------------------------------------------------------------------
# 1. budgets -- the ratchet
# --------------------------------------------------------------------------


def test_description_within_budget(tool: AndroidInspectorTool) -> None:
    actual = len(tool.description)
    assert actual <= DESCRIPTION_BUDGET, (
        f"tool description is {actual} chars, over its pinned lean budget of "
        f"{DESCRIPTION_BUDGET} (+{actual - DESCRIPTION_BUDGET})."
    )


def test_param_descriptions_within_budget(tool: AndroidInspectorTool) -> None:
    props = tool.input_schema["properties"]
    actual = sum(len(p.get("description", "")) for p in props.values())
    assert actual <= PARAM_DESCRIPTION_BUDGET, (
        f"parameter descriptions total {actual} chars, over "
        f"{PARAM_DESCRIPTION_BUDGET}."
    )


def test_serialized_surface_within_budget(tool: AndroidInspectorTool) -> None:
    actual = len(serialized(tool.description, tool.input_schema))
    assert actual <= SERIALIZED_BUDGET, (
        f"serialized tool definition is {actual} chars, over {SERIALIZED_BUDGET}."
    )


def test_awareness_within_budget() -> None:
    actual = len(AWARENESS_PATH.read_text())
    assert actual <= AWARENESS_BUDGET, (
        f"{AWARENESS_PATH.name} is {actual} chars, over {AWARENESS_BUDGET}."
    )


def test_only_a_named_parameter_contract_exceeds_the_ceiling(
    tool: AndroidInspectorTool,
) -> None:
    """>600 chars is allowed only for a named parameter contract ('port')."""
    over = {
        name: len(prop.get("description", ""))
        for name, prop in tool.input_schema["properties"].items()
        if len(prop.get("description", "")) > PARAM_CEILING
    }
    assert set(over) == set(PARAM_CONTRACT_EXEMPTIONS), (
        f"parameters over {PARAM_CEILING} chars changed: {over}"
    )
    for name, ceiling in PARAM_CONTRACT_EXEMPTIONS.items():
        assert over[name] <= ceiling, f"{name} grew to {over[name]} (> {ceiling})"


def test_tool_surface_carries_no_example_blocks(tool: AndroidInspectorTool) -> None:
    prose = tool_prose(tool.description, tool.input_schema)
    assert "<example>" not in prose and "<commentary>" not in prose


# --------------------------------------------------------------------------
# 2. fidelity -- nothing was traded for bytes
# --------------------------------------------------------------------------


def test_every_stock_tool_contract_survives(tool: AndroidInspectorTool) -> None:
    absent = missing(TOOL_SEMANTICS, tool_prose(tool.description, tool.input_schema))
    assert absent == [], "tool contracts lost from the lean surface:\n  " + "\n  ".join(
        absent
    )


def test_every_stock_awareness_contract_survives() -> None:
    absent = missing(AWARENESS_SEMANTICS, AWARENESS_PATH.read_text())
    assert absent == [], "awareness contracts lost:\n  " + "\n  ".join(absent)


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.+-]*[A-Za-z0-9_]|\d{3,4}-?\d*")


def named_tokens(text: str) -> set[str]:
    """Identifier-ish tokens: anything with _ . - , ALL-CAPS, camelCase, or numeric.

    Slash compounds are split ('pidof/ps' -> 'pidof', 'ps') and everything is
    lowercased, so a pure re-wording is not reported as a loss -- only a
    genuinely dropped name is.
    """
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


def test_tool_token_census_from_stock_has_zero_absences(
    tool: AndroidInspectorTool,
) -> None:
    """Derived from the STOCK text, not from the new one -- so it cannot be
    satisfied by an inventory written to match whatever was shipped."""
    stock = stock_surface()
    stock_text = tool_prose(stock["description"], stock["input_schema"])
    lean = tool_prose(tool.description, tool.input_schema).lower()
    absent = sorted(t for t in named_tokens(stock_text) if t not in lean)
    assert absent == [], f"named tokens dropped from the tool surface: {absent}"


def test_awareness_token_census_from_stock_has_zero_absences() -> None:
    stock_text = (FIXTURES / "stock-android-awareness.md").read_text()
    lean = AWARENESS_PATH.read_text().lower()
    absent = sorted(t for t in named_tokens(stock_text) if t not in lean)
    assert absent == [], f"named tokens dropped from the awareness file: {absent}"


def test_schema_structure_is_unchanged_apart_from_wording(
    tool: AndroidInspectorTool,
) -> None:
    """No parameter, type, default, enum value or required key was leaned away."""
    stock = stock_surface()["input_schema"]
    lean = tool.input_schema

    def structure(schema: dict) -> dict:
        return {
            "required": schema["required"],
            "type": schema["type"],
            "properties": {
                name: {k: v for k, v in prop.items() if k != "description"}
                for name, prop in schema["properties"].items()
            },
        }

    assert structure(lean) == structure(stock)


# --------------------------------------------------------------------------
# 3. byte pins -- the reviewed text, verbatim
# --------------------------------------------------------------------------


def test_description_is_byte_identical_to_the_pinned_text(
    tool: AndroidInspectorTool,
) -> None:
    assert tool.description == (FIXTURES / "tool-description.txt").read_text()


def test_awareness_is_byte_identical_to_the_pinned_text() -> None:
    assert AWARENESS_PATH.read_text() == (FIXTURES / "android-awareness.md").read_text()


def test_agent_descriptions_are_preserved_byte_for_byte() -> None:
    """Already compliant upstream (863afa1) -- this lane must not touch them."""
    for filename, (digest, chars) in AGENT_DESCRIPTION_PINS.items():
        description = agent_description(AGENTS_DIR / filename)
        assert len(description) == chars, f"{filename}: {len(description)} chars"
        actual = hashlib.sha256(description.encode()).hexdigest()[:16]
        assert actual == digest, f"{filename}: sha256 {actual} != pinned {digest}"
        assert "<example>" not in description
        assert "<commentary>" not in description
