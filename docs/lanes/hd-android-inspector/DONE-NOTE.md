# Lean Android head surface — done note

Lane `hd-android-inspector` · item `model_performance-lk4i` · baseline `fbf8ce6` (origin/main).

Every number below is generated, not typed:
`docs/lanes/hd-android-inspector/scripts/measure_and_report.py` writes
`evidence/fidelity-table.md`, and the same measurements are asserted in
`modules/tool-android-inspector/tests/test_lean_head_surface.py` so the note cannot
drift from the tree.

## 1. What changed

Two always-on surfaces — text every session pays for whether or not Android work happens:

- `modules/tool-android-inspector/.../__init__.py` — the `android_inspector` tool
  description (hoisted to a module-level `_DESCRIPTION` so it can be byte-pinned) and
  7 of its 30 parameter descriptions.
- `context/android-awareness.md` — the injected awareness context.

Nothing else. `agents/*.md` were made trigger-first upstream in `863afa1` and are
**preserved byte-for-byte**, pinned by sha256 in the test file.

## 2. Sizes — before / after

| Surface | Stock (fbf8ce6) | Lean | Delta | % |
|---|---:|---:|---:|---:|
| tool description | 3,619 | 3,044 | −575 | −15.9% |
| 30 parameter descriptions | 3,398 | 3,293 | −105 | −3.1% |
| tool prose total (description + params) | 7,017 | 6,337 | −680 | −9.7% |
| serialized tool definition | 9,097 | 8,417 | −680 | −7.5% |
| `context/android-awareness.md` | 3,413 | 2,787 | −626 | −18.3% |
| **always-on total** | **12,510** | **11,204** | **−1,306** | **−10.4%** |

The item quotes 9,126 / 3,415 characters; measured here they are 9,097 / 3,413. The
difference is measurement, not scope: "serialized tool definition" is
`json.dumps({name, description, input_schema}, ensure_ascii=False)` over the exact
properties the provider is handed, and the awareness figure is the file's byte length.
Both methods are in the scripts; the deltas are unaffected.

Per-parameter sizes: `evidence/fidelity-table.md` §"Per-parameter description sizes".
**23 of 30 parameter descriptions are byte-for-byte stock.** The 7 reworded are
`avd`, `port`, `abi`, `accept_licenses`, `force`, `package`, `component` — each one a
parameter the description used to duplicate, now stated once in the parameter and
cross-referenced from the description ("see 'port'", "see 'force'").

`port` is the only description over 600 chars (710, down from 799) — a named parameter
contract, exempted explicitly and by name in
`test_only_a_named_parameter_contract_exceeds_the_ceiling`, which fails if any other
parameter crosses the ceiling or if `port` itself grows.

## 3. `<example>` / `<commentary>` counts

| Surface | Stock | Lean |
|---|---:|---:|
| tool prose (description + 30 params) | 0 | 0 |
| `context/android-awareness.md` | 0 | 0 |
| `agents/android-debugger.md` | 0 | 0 |
| `agents/android-operator.md` | 0 | 0 |
| `agents/android-visual-tester.md` | 0 | 0 |

Honest reading: **this lane removed no example blocks, because there were none left to
remove.** `863afa1` had already stripped them from the agent descriptions, and the tool
surface never carried any. The counts are reported because the item asks for them, and
`test_tool_surface_carries_no_example_blocks` plus the agent pins keep all five at zero.

## 4. Fidelity — every stock semantic accounted for

`tests/lean_head_inventory.py` enumerates every contract the stock surface stated, each
as literal probes that must still be findable in the lean text.

| Surface | Semantics | Expected absent | Observed absent |
|---|---:|---:|---:|
| tool (description + parameters) | 90 | 0 | **0** |
| awareness | 39 | 0 | **0** |
| **total** | **129** | **0** | **0** |

Full per-ID table with the asserted probes: `evidence/fidelity-table.md`.

A hand-written inventory can be gamed by writing it from the *new* text, so a second,
independent check is derived from the **stock** text: every identifier-ish token
(anything containing `_ . -`, ALL-CAPS, camelCase, or numeric) is extracted from stock
and must still appear in lean.

| Surface | Named tokens in stock | Absent from lean |
|---|---:|---:|
| tool surface | 64 | **0** |
| awareness | 33 | **0** |

That covers exactly the classes the item names — selector keys, device/emulator/AVD/adb
names, accessibility and synchronisation operations, ANR, raw-coordinate warnings,
parameter names, failure modes, safety contracts (`5554-5682`, `port + 1`,
`port_allocation_fallback`, `KEYCODE_BACK`, `pm clear`, `mCurrentFocus`, `pidof`,
`--pid`, `ptrace_scope`, `61px`/`93px`, `emulator-*`, `<ip>:<port>`, …).

Structure is pinned separately: `test_schema_structure_is_unchanged_apart_from_wording`
asserts every parameter name, type, default, enum value and `required` key is identical
to stock. No parameter was leaned away.

**Known limitation, stated plainly:** 2 of the 129 probes (T-12, A-14) are phrased
against the lean wording, so they fail when the guardrail is run against stock (visible
in `evidence/guardrail-red-stock.txt`). The other 127 match both. The stock-derived
token census is the check that cannot be written to fit the new text, and it is at zero.

## 5. Byte pins

The reviewed text itself is vendored, so a later paraphrase shows up as a diff rather
than sliding through:

- `tests/fixtures/lean-head/tool-description.txt` — asserted `==` `tool.description`
- `tests/fixtures/lean-head/android-awareness.md` — asserted `==` the live file
- `tests/fixtures/lean-head/stock-tool-surface.json` — the fbf8ce6 baseline
- `tests/fixtures/lean-head/stock-android-awareness.md` — the fbf8ce6 baseline
- `agents/*.md` description blocks — sha256 + char count, 3 files (`598` / `594` / `600`)

## 6. The guardrail is a ratchet, not decoration

`tests/test_lean_head_surface.py` — 14 tests, three kinds: budgets (ceilings on each
surface), fidelity (§4), byte pins (§5). A budget without fidelity is met by deleting
contracts; fidelity without a budget is met by leaning nothing.

- **GREEN on this branch** — `evidence/guardrail-green-branch.txt`: 14 passed;
  full module suite 346 passed.
- **RED against stock** — `evidence/guardrail-red-stock.txt`: **9 of 14 fail**, with real
  numbers (`3619 <= 3044`, `3398 <= 3293`, `9097 <= 8417`, `3413 <= 2787`,
  `port grew to 799 (> 710)`), plus both fidelity probes and both byte pins.

## 7. Rendered tool surface — measured STATICALLY

**Method (this is the corrected part).** `scripts/static_tool_surface.py` reads
`name` / `description` / `input_schema` straight off `AndroidInspectorTool` by importing
the package out of a source tree — before from `git archive fbf8ce6` into a temp dir,
after from the working tree. The module declares `dependencies = []`, so this needs
nothing but stdlib.

**No `amplifier` process is invoked, against any `AMPLIFIER_HOME`, scratch or otherwise.**

```
$ python3 docs/lanes/hd-android-inspector/scripts/static_tool_surface.py --rev fbf8ce6 --out before.json
serialized_tool_definition_chars=9097 description_chars=3619 param_description_chars=3398 param_count=30 prose_total_chars=7017

$ python3 docs/lanes/hd-android-inspector/scripts/static_tool_surface.py --out after.json
serialized_tool_definition_chars=8417 description_chars=3044 param_description_chars=3293 param_count=30 prose_total_chars=6337
```

Artifacts: `evidence/tool-surface-{before,after}.json` and `.sizes.txt`.

**Why the method changed, and what it cost.** An earlier session in this lane rendered
the same surface through a live mount under a scratch `AMPLIFIER_HOME`. That is
forbidden: the CLI's first run editable-installs into the *shared* uv tool venv
regardless of `AMPLIFIER_HOME`, so the scratch home's first render rewrote 61 of 63
`_editable_impl_*.pth` files in the owner's real install to point at
`/tmp/hd-android-scratch-before-*`, and the owner's CLI began disagreeing with itself
about whose code runs. It was repaired by hand from a backup. The two scripts that did
it (`render-surface.sh`, `render_tool_surface.py`) and their scratch-home artifacts are
**deleted** in this PR; `static_tool_surface.py` carries that incident in its module
docstring so the next person does not re-derive it the same way.

**Cross-check.** The static read reproduces the earlier live render **byte-for-byte** —
`diff` is empty for both before and after. So the correction cost no fidelity: the
numbers this PR reports are the same numbers a real mount produces, obtained without
touching the host.

**Contamination check, run at the end of this session as the item requires:**

```
$ grep -l /tmp/ ~/.local/share/uv/tools/amplifier/lib/python3.13/site-packages/*.pth
(no output — exit 1)
```

Clean. No `.pth` file points into `/tmp`. Nothing was touched or repaired by this
session.

## 8. validate-agents

`validate-agents` is a foundation *recipe*: four deterministic steps
(environment-check → agent-discovery → structural-validation → quality-classification)
then four LLM steps. This lane's budget is $0 with no API calls, so
`scripts/validate_agents_structural.py` runs **only the deterministic four** — the ones
that decide structural errors, the `<example>`/`<commentary>` ban and the description
length gate. It reads the step bodies out of the installed recipe YAML and executes them
verbatim rather than copying them, so it cannot drift from the validator it claims to run
(recipe v1.8.0).

Both trees, identical results — `evidence/validate-agents-structural-phases.txt`:

```
agents discovered: 3
structural summary: {'total': 3, 'passed': 3, 'errors': 0, 'warnings': 3}
  android-debugger       chars=598 examples=0 commentary=0 errors=[] warnings=['NO_TOOLS_SECTION']
  android-operator       chars=594 examples=0 commentary=0 errors=[] warnings=['NO_TOOLS_SECTION']
  android-visual-tester  chars=600 examples=0 commentary=0 errors=[] warnings=['NO_TOOLS_SECTION']
VERDICT: structural errors=0 -> PASS
```

**PASS on both stock and branch, unchanged.** The 3 `NO_TOOLS_SECTION` warnings are
pre-existing on stock and out of this lane's scope. `quality_level: needs_work` is the
deterministic classifier's verdict driven by those same warnings — also identical on
both trees, also unchanged by this lane.

## 9. CI

This repo *does* have CI (`.github/workflows/ci.yml`, added in `fbf8ce6`). All three
jobs run locally, green — `evidence/ci-*.txt`:

| Job | Command | Result |
|---|---|---|
| Lint | `uvx ruff@0.16.6 check --isolated --select E4,E7,E9,F .` | All checks passed |
| Tests (3.11) | `uv sync --extra dev --python 3.11 && uv run --no-sync pytest tests/ -q` | 346 passed |
| Tests (3.13) | `uv sync --extra dev --python 3.13 && uv run --no-sync pytest tests/ -q` | 346 passed |
| Bundle structure | parse `bundle.md` frontmatter + `behaviors/*.yaml` | 2 documents, OK |

346 = 332 pre-existing + 14 new guardrail tests. No pre-existing test was modified.

## 10. Spend

**$0. Zero API calls.** Every measurement is static file reading, `git archive`, stdlib
imports, pytest and the recipe's own deterministic python steps. No model was invoked by
any script in this lane.

## 11. Reproducing all of it

```bash
S=docs/lanes/hd-android-inspector/scripts
python3 $S/static_tool_surface.py --rev fbf8ce6 --out /tmp/before.json   # stock surface
python3 $S/static_tool_surface.py --out /tmp/after.json                  # lean surface
python3 $S/measure_and_report.py                                         # sizes + fidelity + census
python3 $S/validate_agents_structural.py . --label BRANCH                # validate-agents, $0
cd modules/tool-android-inspector && uv run --extra dev pytest tests/ -q  # 346 passed
```
