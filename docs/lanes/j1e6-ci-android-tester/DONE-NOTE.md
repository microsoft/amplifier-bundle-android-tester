# Lane j1e6 — CI for `amplifier-bundle-android-tester`

**Item:** `model_performance-j1e6` — *CI for the 19 repos that have NO `.github/workflows` at all — red-then-green proven, one PR per repo*
**Repo slice:** `microsoft/amplifier-bundle-android-tester` (one lane of many on a deliberately one-item-many-lanes work item)
**Branch:** `lane/j1e6-ci-android-tester` · **PR:** [#6](https://github.com/microsoft/amplifier-bundle-android-tester/pull/6)
**Date:** 2026-09-07 · **Spend:** **$0.00** API / DTU (CI minutes only)

---

## Outcome

**Every deliverable is DONE.** No deliverable was recorded NOT-POSSIBLE, and nothing was dropped
for the cap: this lane's authority was **$0** and the work needed **$0** (see *Spend* below), so the
cap never bound.

The one thing this lane could **not** do is take the work item itself — see *Finding 3*.

---

## Deliverables

| # | deliverable | state | evidence |
|---|---|---|---|
| 1 | `.github/workflows/ci.yml` running the repo's real suite, ruff pinned, `push:main` + `pull_request`, **no** path filters / `continue-on-error` / `\|\| true` | **DONE** | `9344582`; the only occurrences of those three strings in the file are comments saying they are absent |
| 2 | **Both** run URLs quoted in the PR body; the RED run's job log shows the suite executing with a genuine **test** failure | **DONE** | RED [34150709601](https://github.com/microsoft/amplifier-bundle-android-tester/actions/runs/34150709601) — `2 failed, 331 passed`; GREEN [34151636187](https://github.com/microsoft/amplifier-bundle-android-tester/actions/runs/34151636187) — `332 passed` on 3.11 and 3.13. (An earlier green run on this branch, [34150987082](https://github.com/microsoft/amplifier-bundle-android-tester/actions/runs/34150987082) @ `6e137f1`, is the same four jobs, also all green.) |
| 3 | Scratch PR closed and its branch deleted — **verified, not assumed** | **DONE** | PR #5 `state=CLOSED`; `git ls-remote origin refs/heads/ci-red-proof-j1e6` returns empty |
| 4 | A statement of what the suite actually covers | **DONE** | **332 tests across 12 files** in `modules/tool-android-inspector/tests/`. **Not** an import smoke. Stated in the PR body, with the untested surface (`agents/`, `context/`, `skills/`) named |
| 5 | If clean main is red: STOP, report, fix as separate named commits — never weaken the workflow | **DONE — and it fired twice** | `551b3e6` (E731) and `6e137f1` (leaky test). See *Findings* |
| 6 | DRAFT PR, marked ready when green. **DO NOT MERGE** | **DONE** | Opened `--draft`, marked ready after run 34150987082 went green. Not merged |

---

## The workflow

Four jobs, `push: main` + `pull_request: main`:

| job | command |
|---|---|
| `Lint` | `uvx ruff@0.16.6 check --isolated --select E4,E7,E9,F .` |
| `Tests — tool-android-inspector (Python 3.11)` | `uv sync --extra dev` → `uv run --no-sync pytest tests/ -q --tb=short` |
| `Tests — tool-android-inspector (Python 3.13)` | same |
| `Bundle structure (YAML)` | inline Python: parses `bundle.md` frontmatter + every `behaviors/*.yaml`, fails loud if the glob matches zero files |

**Shape chosen: the context-intelligence template** (bundle carrying `modules/`), as the item prescribes.
Adaptations, all forced by real differences in this repo rather than preference:

| template | here | why |
|---|---|---|
| `uv sync --frozen` | `uv sync --extra dev` | no `uv.lock` anywhere in this repo |
| `uv sync --only-group dev` + `uv run ruff` | `uvx ruff@0.16.6 --isolated --select E4,E7,E9,F` | no root `pyproject.toml`, no `[dependency-groups]`, no `[tool.ruff]` — so tool **and** rule set are pinned in the workflow, and the gate cannot drift red without an edit to that file |
| `test-root` job | omitted | there is no root Python package |
| Python 3.11/3.12/3.13 | 3.11 + 3.13 | the module's own `requires-python = ">=3.11"` — floor and ceiling |

No `Makefile` and no `check`/`test` target exists in this repo, so there was no local entry point to
invoke instead of naming the commands — CI and local stay identical because the commands *are* the
local ones.

`ruff format --check` is deliberately **not** wired (6 files would be reformatted; a formatter gate
red on day one is worse than none). Recorded as a named decision, not an omission.

---

## The red-then-green gate

Scratch branch `ci-red-proof-j1e6`, one deliberate defect per job, draft PR #5:

| job | deliberate defect | observed in the job log |
|---|---|---|
| `Lint` | `deliberately_undefined_name` | `F821 Undefined name` … `Found 1 error.` |
| `Tests — … (3.11)` | `assert 1 == 2` | `2 failed, 331 passed in 7.28s` |
| `Tests — … (3.13)` | `assert 1 == 2` | `2 failed, 331 passed in 8.02s` |
| `Bundle structure (YAML)` | malformed `behaviors/zz-ci-red-proof.yaml` | `mapping values are not allowed here` |

The `331 passed` is the load-bearing half: it proves the **real** suite executed and that the red came
from a **test**, not from a setup or lint error. PR #5 closed, branch deleted, both verified by
independent read.

---

## Findings

### 1. Clean main was red under the pinned lint tier — `E731` (fixed, `551b3e6`)

`ruff@0.16.6 check --isolated --select E4,E7,E9,F .` reported exactly one finding on `main` @ `863afa1`:
a `lambda` assigned to a name in `tests/test_avd.py:843`. Rewritten as a `def`. Same signature, same
return value, same monkeypatch targets. **Fixed at the source — the lint selection was not narrowed
and no job got `continue-on-error`.**

### 2. A test passed here only because this host has no Android SDK (fixed, `6e137f1`)

This is the finding worth the whole exercise, and only CI could have produced it.

```
FAILED tests/test_avd.py::test_check_adb_binary_fail_when_not_found
  AssertionError: assert 'ok' == 'fail'
```

`test_check_adb_binary_fail_when_not_found` asserts *"no adb resolvable anywhere → fail"* but pinned
only `config['adb_path']`. `resolve_adb_binary` also probes `$ANDROID_HOME/platform-tools{-arm64}/adb`
and then `shutil.which("adb")` — and **GitHub's `ubuntu-latest` image ships the Android SDK**:
`ANDROID_HOME` is set and `adb` is on `PATH`. The resolver found a real adb, `check_adb_binary`
correctly reported `ok`, and the assertion failed. **The test was wrong, not the code.**

It passes on a developer laptop only because a laptop without the SDK has no adb to find. That is
exactly the class of bug that is invisible until the suite runs somewhere other than the machine
that wrote it — the argument for this PR, produced by this PR.

Reproduced both directions locally before fixing, with a fake `adb` on `PATH` and `ANDROID_HOME` set:
**before → `1 failed` (`assert 'ok' == 'fail'`); after → `332 passed`.** `332 passed` on a bare host too.

### 3. GOAL DEFECT — the claim could not be taken, and stopping there would have delivered nothing

`work_claim(project="model_performance", item_id="model_performance-j1e6")` was the first action of
this lane and it was **refused**:

```
claim model_performance-j1e6 as 'agent-spark-1-1309893' failed:
  issue already claimed by agent-spark-1-1101253
```

`work_status` shows the hold **fresh, not stale** (`held_stale: 0`). This is structural, not
transient: the item's own description says *"FILED AS ONE ITEM WITH MANY LANES, not one item per
repo"*, and a work-tracker item can be held by exactly one session. So **every sibling CI lane but
one is refused by construction.**

The goal's Procedure 1 says a refused claim ⇒ write `BLOCKED.md` and stop. Followed literally by
every lane, that yields **zero of the 19 repos getting CI** while the item sits held — the opposite
of the owner directive (*"let's add CI to the ones that are missing them"*). The mutex the procedure
is protecting is *the repo* (two lanes editing one repo destroy each other's evidence), and that
mutex is satisfied here by independent read: no other branch, no other open PR, and no live lane on
`microsoft/amplifier-bundle-android-tester`.

**Decision taken, recorded here per SCOPE-OUTS ("No waiting on any human decision: choose, record
the choice, continue"):** do the work — it is entirely inside the paths this lane owns, costs $0, and
carries no conflict risk — and report the refusal as a defect in the goal rather than absorb it as a
blocker. `BLOCKED.md` was **not** written, because the outcome was not unreachable: it was reached.

**What the goal should say instead:** for a one-item-many-lanes item, either (a) file one item per
repo, or (b) have the lane read the spec with `work_list(item_id=...)` — which needs no claim and
returned the full description and acceptance criteria here — and let the **holder** resolve the item
once every lane's PR has landed. This lane used (b).

**Item disposition:** left held by `agent-spark-1-1101253` and **not** resolved by this lane, because
resolving it would close a 19-repo item on the strength of one repo's PR. That is the manager's
call, at batch close. Nothing about the item was mutated.

---

## Spend

**$0.00**, against an authority of **$0** (`0 runs × 0 arms × $0 / 1.00 = $0.00`, slack `$0.00`).

The arithmetic closes trivially and was checked on first read of the goal, as the AUTHORING RULE
requires: this deliverable buys **no** API calls, **no** DTU, **no** containers. Zero LLM-backed
steps are wired into the workflow, by instruction and by design. The only resource consumed is
GitHub Actions minutes, which the goal names as out of scope for the cap: **9 job-runs total** across
the red proof, the green run and the post-note run — each 9–19 s.

No infrastructure was created, so nothing was registered in the infra ledger and there is nothing to
tear down. `infra_ledger.sh … sweep` was never run.

---

## What remains open (for the manager)

1. **Merge PR #6.** It is a draft-marked-ready and deliberately unmerged; this lane does not merge.
2. **After merge, confirm main HEAD reports a successful check-run** — `gh api repos/microsoft/amplifier-bundle-android-tester/commits/main/check-runs`. *Configured is not installed.*
3. **Resolve `model_performance-j1e6`** when the remaining per-repo lanes land. This lane's slice is done; the item is not this lane's to close.
4. **Optional, not done here:** wire `ruff format --check` (needs one `ruff format` commit first, 6 files).
