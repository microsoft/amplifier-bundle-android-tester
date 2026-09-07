# Lane kp79 — agent-description catalog hygiene, `amplifier-bundle-android-tester`

**Item:** `model_performance-kp79` · **Outcome branch: A (RESOLVED)** · **Spend: $0.00 of $0.00**

Stage 1 of the catalog-hygiene sweep applied to this repo: every agent frontmatter
`description` is now trigger-first, ≤600 chars, carries an explicit USE WHEN / DO NOT USE
WHEN, and contains **zero** `<example>` / `<commentary>` blocks. The one skill's
`description` is trigger-first, one paragraph, ≤400 chars.

---

## 1. The measurement that makes this real — delegate agent catalog, before vs after

Rendered from a **scratch session** on each tree (a real `AmplifierSession` built from
`bundle.md`; the live `delegate` tool's `description` property read straight off the
mounted tool). No prompt executed, so **no LLM call and no API cost**.

Reproduce:

```bash
python3 docs/lanes/kp79-catalog-android-tester/render_catalog.py  file://<checkout>/bundle.md  <out.txt>
python3 docs/lanes/kp79-catalog-android-tester/measure_catalog.py <before.txt> <after.txt> android-tester
```

```
WHOLE delegate tool description: 44922 B -> 40575 B  (saved 4347 B, 9.7%)
agent rows: before 43, after 43

agent                                         before B   after B   saved B
android-tester:android-debugger                   2160       636      1524
android-tester:android-operator                   2309       632      1677
android-tester:android-visual-tester              1789       643      1146
TOTAL (selected rows)                             6258      1911      4347

rows NOT matching 'android-tester': 37455 B -> 37455 B (delta 0 B; expect 0)
```

**4,347 bytes removed from every session's head, on every turn.** The three
android-tester rows fell **6,258 B → 1,911 B (−69.5 %)**. The 40 non-android rows are
byte-identical across the two renders (delta 0 B) — the control that proves nothing else
moved.

Artifacts: `evidence/delegate-catalog.before.txt`, `evidence/delegate-catalog.after.txt`,
`evidence/catalog-measurement.txt`.

### hooks-skills-visibility block — 0 bytes saved today, and why

This repo ships one skill (`skills/android-self-hosted-publishing/`), but **no bundle file
in this repo registers it**. Neither `bundle.md` nor `behaviors/android-tester.yaml`
declares a `skills:` key, and a scratch session built from `bundle.md` answers
`load_skill(search="android")` with `No skills matching 'android'`. So the skill's
description **is not on an always-on surface today** and its 78-char reduction currently
buys **0 catalog bytes**. It was still brought to standard so the saving is banked if and
when the bundle registers the skill. Stated plainly rather than folded into the headline —
the honest catalog number for this repo is **4,347 B, all of it from the agent rows**.

---

## 2. Before/after char counts

| File | stock | lean | delta | ≤ budget |
|---|---:|---:|---:|---|
| `agents/android-debugger.md` | 2,118 | 598 | −1,520 | ✅ ≤600 |
| `agents/android-operator.md` | 2,261 | 594 | −1,667 | ✅ ≤600 |
| `agents/android-visual-tester.md` | 1,738 | 600 | −1,138 | ✅ ≤600 |
| **agent subtotal** | **6,117** | **1,792** | **−4,325 (−70.7 %)** | |
| `skills/android-self-hosted-publishing/SKILL.md` | 463 | 385 | −78 | ✅ ≤400 |
| **repo total** | **6,580** | **2,177** | **−4,403 (−66.9 %)** | |

`<example>` blocks: **8 → 0**. `<commentary>` blocks: **8 → 0**.
(The item's pre-launch estimate said "3 agents × 3 examples"; measured, it is 3 + 3 + 2 = 8
across 3 files. Same three files, same order of magnitude.)

Nothing in this repo was already compliant — all three agents and the one skill needed
work, so nothing was left unedited on compliance grounds. No file was edited that did not
need it.

---

## 3. FIDELITY TABLE — every stock fact, checked against lean

Method: enumerate every WHAT / USE WHEN / "Authoritative on" fact in the stock
description, including facts that lived **only inside** `<example>`/`<commentary>` blocks,
and locate each in the lean description or in the agent body.

**Result: zero routing facts lost. Nothing to restore. No byte delta owed.**

### `android-operator` (2,261 → 594)

| Stock fact | In lean? |
|---|---|
| Drives Android apps on emulators **and physical devices** | ✅ "on an emulator or device" |
| Boots the emulator | ✅ "Owns emulator boot" |
| Installs the APK / launches the app | ✅ "install/launch an APK" |
| Interacts via the accessibility tree | ✅ "`ui_dump` selector resolution" |
| Verifies the resulting UI and data are real | ✅ "show a screen has real data, not an empty shell" |
| USE: install and launch an app on emulator/device | ✅ |
| USE: exercise a UI flow (navigation, forms, settings, tabs) and verify it works | ✅ verbatim parenthetical kept |
| USE: confirm a fix landed on the screen, not just in the tests | ✅ |
| USE: configure settings over adb and prove the values took | ✅ "write app settings over adb and prove they took" |
| USE: evidence a screen shows real data, not an empty shell | ✅ |
| AUTH: emulator boot lifecycle · APK install/launch · `ui_dump` selector resolution · verified field-write protocol · `wait_for` synchronisation · logcat correlation · adb serial safety | ✅ all seven present |
| *(example-only)* owns the **aarch64 host workarounds** | ✅ **RESCUED** out of a `<commentary>` into the description proper |
| *(example-only)* focus-assertion + readback catches silent wrong-field writes | ✅ "the verified field-write protocol"; full rationale in body §Step 6 |
| *(example-only)* a screenshot alone never proves data is live | ✅ body §Step 7 (independent-confirmation discipline) |
| DO NOT USE WHEN | ➕ **ADDED** — stock had none |

### `android-debugger` (2,118 → 598)

| Stock fact | In lean? |
|---|---|
| Investigates UI anomalies **to root cause** | ✅ "behaviour is wrong and **why is unknown**" |
| Frame diffing · accessibility-tree comparison · focus tracing · logcat correlation | ✅ "dump-to-dump frame/accessibility-tree diffing, focus tracing, … logcat correlation" |
| USE: a tap or interaction produces no visible effect | ✅ |
| USE: typed text does not persist or lands somewhere unexpected | ✅ "vanishes or lands in the wrong field" |
| USE: screen renders blank, partially, or with stale content | ✅ "blank, partial or stale" |
| USE: navigation silently fails or lands on the wrong screen | ✅ "silently fails or misroutes" |
| USE: an interaction that used to work has stopped | ✅ |
| USE: app "looks fine" but the underlying behaviour is wrong | ✅ |
| AUTH: dump-to-dump frame diffing · focus tracing · tap-target verification · logcat correlation · ANR and IME interference · Compose accessibility gaps | ✅ all six present |
| AUTH: distinguishing "tap missed" from "tap landed, **handler** did nothing" | ✅ compressed to `"tap missed"` vs `"tap landed but did nothing"`; the word *handler* is dropped from the description only — the full four-mode table naming it is at `agents/android-debugger.md:67,123,174-175`. **Distinction preserved; not a routing-fact loss.** |
| DO NOT USE WHEN | ➕ **ADDED** — stock had none |

### `android-visual-tester` (1,738 → 600)

| Stock fact | In lean? |
|---|---|
| Validates the visual quality of Android screens | ✅ "the question … is how it LOOKS" |
| Screenshot sweeps across screens **and device configurations** | ✅ "screenshots of every screen/tab" + "device-configuration sweeps" |
| Clipping · blank regions · overlap · misalignment | ✅ all four |
| Before/after comparison to confirm a layout fix landed | ✅ |
| USE: screenshots of every screen/tab reviewed for visual defects | ✅ |
| USE: confirm a layout or styling fix is visible on the device | ✅ |
| USE: clipped text, blank areas, overlapping elements, cut-off lists | ✅ |
| USE: before/after visual comparison of an Android UI change | ✅ |
| USE: visual regression sweep after a refactor | ✅ |
| AUTH: screenshot sweeps · clipping/blank-region detection · overlap and truncation · before/after comparison · dump-vs-render reconciliation · severity classification | ✅ all six |
| DO NOT USE WHEN | ➕ **ADDED** — stock had none |

### `android-self-hosted-publishing` (skill, 463 → 385)

| Stock fact | In lean? |
|---|---|
| Signed APK + signed manifest sidecar | ✅ |
| `versionCode` monotonicity enforcement | ✅ |
| HTTPS-first with a TLS-only HTTP fallback | ✅ |
| Proving the installed build is the one you just made | ✅ "installed-build verification" |
| Trigger: building/reviewing/debugging a self-hosted update rail | ✅ (now the **first** clause) |
| Trigger: a device bug report's first question — is the fix on the device? | ✅ |
| "…for an Android app **under test**" | scoping words dropped from the description; the body's opening paragraph and §versionCode both frame it as a tester-facing rail. Not a trigger condition. |
| "Load-bearing rules for…" | framing, not a fact — dropped deliberately (it was the reason the description was not trigger-first) |

**Facts present in stock and absent from lean AND body: none.** Nothing restored; byte
delta owed: 0.

---

## 4. `validate-agents` recipe — run on the branch, verdict quoted

Recipe `validate-agents` **v1.7.0**, run against this worktree on
`lane/kp79-catalog-android-tester`, run id `run-143a5fa95268`.

> **Overall Verdict**: ⚠️ **PASS WITH WARNINGS**
> **Agents Found**: 3 total across 1 location
> **Quality Breakdown**: 0 good, 0 polish, **3 needs_work**, 0 critical
> **Issues**: **0 errors**, **3 warnings** (all the same warning: `NO_TOOLS_SECTION`), 0 blocking suggestions

**Discovered agent count for this repo: 3** (`agents/` ×3; `candidates_scanned` 3,
`non_agent_count` 0).

### The deliverable says "it must stay PASS". It was never PASS — it was FAIL.

Establishing the stock baseline with the recipe's **own deterministic phases 0–3**
(`environment-check`, `agent-discovery`, `structural-validation`,
`quality-classification`), executed verbatim out of the recipe YAML by
`run_validate_phases.py` — no LLM, no second paid run:

```
########## STOCK (origin/main @ 443e393) ##########
agents discovered: 3
structural summary: {'total': 3, 'passed': 0, 'errors': 6, 'warnings': 3}
quality_level: critical
  android-debugger       chars= 2118 examples=3 commentary=3 errors=['COMMENTARY_TAG_PRESENT', 'EXAMPLE_BLOCK_PRESENT']
  android-operator       chars= 2261 examples=3 commentary=3 errors=['COMMENTARY_TAG_PRESENT', 'EXAMPLE_BLOCK_PRESENT']
  android-visual-tester  chars= 1738 examples=2 commentary=2 errors=['COMMENTARY_TAG_PRESENT', 'EXAMPLE_BLOCK_PRESENT']

########## BRANCH (lane/kp79-catalog-android-tester) ##########
agents discovered: 3
structural summary: {'total': 3, 'passed': 3, 'errors': 0, 'warnings': 3}
quality_level: needs_work
  android-debugger       chars=  598 examples=0 commentary=0 errors=[]
  android-operator       chars=  594 examples=0 commentary=0 errors=[]
  android-visual-tester  chars=  600 examples=0 commentary=0 errors=[]
```

`quality_level: critical` maps to **❌ FAIL** by the recipe's own verdict table. So the
direction of travel is **❌ FAIL → ⚠️ PASS WITH WARNINGS**, and **6 structural errors → 0**.
The gate as literally worded ("stay PASS") could not be met by any branch, because the
premise was false for this repo. This is recorded as a **deviation with its reason**, not
absorbed silently.

### The 3 residual warnings are pre-existing and were deliberately not touched

`NO_TOOLS_SECTION` fires identically on all three agents **in stock and on the branch** —
this lane neither introduced nor removed it. Not fixed here, for two reasons:

1. **It renders zero catalog bytes.** A `tools:` block does not appear in the delegate
   catalog, so it cannot advance the measurement deliverable — the definition of an edit
   that exists to produce a diff.
2. **It carries a real behavioural risk out of scope for a description sweep.** The tool
   is already declared once at the bundle layer (`behaviors/android-tester.yaml:10`,
   which also includes exactly these three agents at lines 20–23). Adding a per-agent
   `tools:` list would create a second source of truth and could *narrow* the agents'
   currently-inherited toolset — their bodies read bundle docs and write report tables,
   not just call `android_inspector`.

**Follow-up worth filing (not done here):** the warning is a genuine *portability* signal.
Composed into another bundle by `agents.include` alone, or spawned standalone, these
agents lose `android_inspector` — and all three bodies open with
`android_inspector(operation="doctor")` as call one.

**Adjacent finding, also not acted on:** `behaviors/android-tester.yaml` passes
`screenshot_dir` and `default_timeout_s`, which the module never reads (the real key is
`work_dir`). Currently benign — the passed value equals the built-in default — but it is
silently ineffective config.

---

## 5. Tests and CI

- **Test suite: 332 passed in 6.81 s.**
  `cd modules/tool-android-inspector && uv run --extra dev python -m pytest -q`
- **CI: this repo has none.** There is no `.github/` directory at all — no workflow, no
  green run to point at. Stated plainly rather than implying one exists. The 332-test suite
  above is the whole automated signal this repo has.
- **No runtime code was touched.** The diff is four description blocks plus this lane's own
  artifacts under `docs/lanes/kp79-catalog-android-tester/`. `docs/` is inside the
  recipe's own `excluded_parts`, so these artifacts cannot perturb a future validation run.

---

## 6. Spend

**$0.00 of $0.00 authorised.** Arithmetic as stated in the goal: `0 runs × 0 arms × $0 /
1.00 = $0.00`, slack `$0.00`. No API measurement was purchased, no DTU launched, no
infrastructure registered, nothing to tear down.

The two paid-surface actions were the ones the authority names explicitly: **one**
`validate-agents` recipe run on the branch (its LLM phases), and the catalog render — which
was implemented so it makes **no** LLM call at all (a session is constructed and torn down
without executing a prompt). The stock-baseline verdict was obtained from the recipe's
deterministic phases only, precisely so a second paid recipe run was not needed.

## 7. Deliverable disposition

| Deliverable | State |
|---|---|
| Every agent `description`: trigger-first, ≤600, USE WHEN / DO NOT USE WHEN, zero `<example>` | **DONE** — 598 / 594 / 600 chars, 0 examples, 0 commentary |
| Skill `description`: trigger-first, one paragraph, ≤400 | **DONE** — 385 chars |
| Fidelity table, per agent and per skill | **DONE** — §3; zero facts lost, nothing to restore |
| Before/after char counts per agent/skill + repo total | **DONE** — §2 |
| Delegate catalog rendered before/after from a scratch session, bytes saved quoted | **DONE** — §1; **4,347 B saved** |
| hooks-skills-visibility block before/after | **DONE, measured at 0 B** — §1; the skill is not registered by any bundle file in this repo, so it never reaches that surface today |
| `validate-agents` run on the branch, verdict quoted, stays PASS | **DONE with a named deviation** — §4; ⚠️ PASS WITH WARNINGS, 0 errors, 3 agents. It could not "stay" PASS: stock was ❌ FAIL (`critical`, 6 errors) |
| CI green where the repo has CI | **N/A, stated plainly** — §5; this repo has no CI |
| Anything already compliant left unedited and named | **N/A** — nothing in this repo was compliant; all four files needed work |
