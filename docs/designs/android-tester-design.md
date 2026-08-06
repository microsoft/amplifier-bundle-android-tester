# android-tester — Design

**Status:** design, with core primitives proven live on aarch64 Linux + KVM (2026-08-06).

## The gap this closes

From the operator who commissioned the harness this bundle generalizes:

> Four consecutive UI fixes to the same screen shipped "verified" and the user found every one
> broken by opening the app. Every one passed unit tests and server-side curl.
> **The failure is always at the render/interaction layer, and no agent here has ever seen the screen.**

Two real projects (`better-attention/voice-chief-of-staff`, `amplifier-attention-manager/attend`)
each independently built a bespoke adb harness to close it. Neither is reusable: both hardcode one
app's package name, resource ids, tab layout and log tags. 939 android bash calls across 46 sessions
went into rediscovering the same lessons twice.

This bundle is that harness, generalized — and with the one step neither project ever automated:
**an agent actually seeing the screen.**

## The load-bearing constraint (measured, not quoted)

**uiautomator is the sensor. The screenshot is for judgment, never for targeting.**

Measured on a live emulator, same screenshot, same element:

| Source | Bounds for the "Refresh" button | Center |
|---|---|---|
| `uiautomator dump` | `[877,142][1006,195]` | (941, 168) |
| VLM reading the PNG | `[810,50][950,100]` | (880, 75) |

Delta: **dx −61px, dy −93px**. The VLM-derived center lands **outside** the real button — the tap
misses. In the source projects this failure was silent: a tap that lands on nothing, or worse on a
*neighbouring* field, produces no error. One recorded run typed a server URL into the API-key field
and reported success.

Every design decision below follows from this. Vision is for *"does this screen look right, is
anything clipped or blank, what state am I in"*. Coordinates come from the accessibility tree.

## Decision: tool module, not bash scripts

Follow the `terminal-tester` shape (Python module + `mount()`), not the `browser-tester` shape
(agent prose wrapping an external CLI).

The rationale is that the hard-won knowledge here is *procedural safety*, and prose decays under
pressure. These four rules were each learned by breaking something real:

1. Every adb call carries `-s <serial>` — an APK was once installed onto the wrong project's emulator.
2. Dump *before* every tap; never reuse coordinates across screens.
3. After tapping a text field, assert `focused="true"` *before* typing.
4. Commit a field with `KEYCODE_BACK`, never by "tapping elsewhere".

An agent instructed to follow these will skip them when a run gets long. A tool that *cannot* tap
without resolving a selector first makes them structural. Fix the mechanism, not the reminder.

## Tool surface — `tool-android-inspector`

Single verb-dispatch tool, `_ok`/`_err` envelope, session state keyed by serial.
The safe path is the default path; raw coordinates require explicit opt-in.

**Device & emulator lifecycle**
| op | notes |
|---|---|
| `list_devices` | enumerates; **flags offline entries and multi-device ambiguity as an error, not a warning** |
| `start_emulator` | `avd`, `port`; applies host workarounds (below), polls `sys.boot_completed`, dismisses keyguard, returns serial |
| `stop_emulator` | `adb emu kill` + process reap |

**App lifecycle**
| op | notes |
|---|---|
| `install` | `-r -g` (reinstall, grant all runtime perms) |
| `launch` | component or `monkey -c LAUNCHER`; waits for `mCurrentFocus` to settle |
| `stop_app` | `am force-stop` |

**Sensing**
| op | returns |
|---|---|
| `screenshot` | `image_path` (a file path, for the VLM — never base64 inline), geometry, byte size as liveness check |
| `ui_dump` | parsed node list — class, text, content-desc, resource-id, bounds, center, focused, clickable. Not raw XML. |
| `find` | nodes matching a selector, with resolved centers |
| `logcat` | tail/filter, for the "is the data real" assertion |

**Interacting** — all selector-first
| op | contract |
|---|---|
| `tap` | dump → resolve selector → tap center → re-dump → report what changed |
| `type_text` | tap → **assert focus** → `KEYCODE_MOVE_END` + N×`KEYCODE_DEL` → `input text` → `KEYCODE_BACK` → re-dump → **assert readback** |
| `key` | keyevent by name or code |
| `swipe` | explicit coords permitted; gestures have no selector analogue |
| `tap_xy` | raw coordinates. Named to be conspicuous. Emits a warning in its result. |

**Synchronising**
| op | contract |
|---|---|
| `wait_for` | polls `ui_dump` until a selector appears/disappears, or timeout. **No bare sleeps anywhere.** |

Selector: `{"text": "Save"}` · `{"res_id": "com.foo:id/save"}` · `{"desc": "Home"}` ·
`{"class": "EditText", "index": 0}` · `{"text_contains": "poll"}`.

## Host workarounds the tool must own

These cost ~57 bash calls of yak-shaving in the source sessions. Encoding them here means nobody
repeats it. All confirmed present on this host (`aarch64`, `ptrace_scope=1`).

- **`-no-window` is a crash workaround, not an optimisation.** The windowed qemu binary needs
  `libpcre2-16.so.0`; headless routes to a different binary with no Qt dependency. Default it on.
- **`kernel.yama.ptrace_scope=1` kills the emulator's own crash handler** (it self-ptraces at
  startup). Detect it and launch under `gdb -batch` with `handle all nostop noprint pass`.
  `handle all` is load-bearing — QEMU uses SIGUSR1/SIGUSR2/SIGCONT internally, and silencing only
  SIGSEGV lets gdb kill a perfectly healthy booting emulator.
- **`platform-tools/adb` is x86_64-only on aarch64 hosts.** Probe for `platform-tools-arm64/adb`
  and fail loudly with the fix if neither works.
- **Backgrounded processes die when a tool call returns.** Emulator launch needs `setsid` +
  `</dev/null` + detachment, not `&`.
- **Readiness is two-stage:** `wait-for-device` *then* poll `sys.boot_completed`, *then*
  `input keyevent 82` to clear the keyguard. Never one without the others.
- **ANR dialogs appear spontaneously on headless emulators.** Auto-dismiss "Wait" — reproduced live
  during this design.

## Agents

| agent | role | model_role |
|---|---|---|
| `android-operator` | the driver: boot → install → launch → interact → verify → report | `[coding, general]` |
| `android-visual-tester` | screenshot sweeps across densities/sizes, visual regression, clipping | `[vision, critique, general]` |
| `android-debugger` | anomaly root-cause: frame diffing, logcat correlation, focus tracing | `[coding, reasoning, general]` |

Each follows the family convention: Prerequisites Self-Check → numbered workflow with wait-gates →
3-attempt Failure Budget → structured report table.

## Context split

- `context/android-awareness.md` — thin (~60 lines), root session: when to use, agent roster,
  what this bundle is *not* for (web → browser-tester, TUI → terminal-tester).
- `context/android-guide.md` — fat, agent-only: full operation reference, selector patterns,
  the verified field-write protocol, Compose accessibility quirks.
- `docs/TROUBLESHOOTING.md` — the aarch64/gdb/ptrace/adb-arch saga.

## Layout

```
amplifier-bundle-android-tester/
├── bundle.md                      includes: [foundation@main, android-tester:behaviors/android-tester]
├── behaviors/android-tester.yaml  tools: [tool-android-inspector] · agents · context
├── context/{android-awareness,android-guide}.md
├── docs/TROUBLESHOOTING.md
├── agents/{android-operator,android-visual-tester,android-debugger}.md
└── modules/tool-android-inspector/
    ├── pyproject.toml
    └── amplifier_module_tool_android_inspector/
        ├── __init__.py          AndroidInspectorTool + async mount(coordinator, config)
        ├── adb.py               serial-scoped adb wrapper — every call carries -s
        ├── emulator.py          host-workaround-aware lifecycle
        └── ui.py                dump parsing, selectors, verified interaction protocol
```

## Environment setup

The bundle assists with setup; it does not install an SDK for you.

| Operation | Does |
|---|---|
| `doctor` | 10 executable checks (arch, `ANDROID_HOME`, adb binary + server, emulator binary, KVM, `ptrace_scope`, gdb, AVDs, cmdline-tools). Never stops at the first failure — returns every finding with its remediation, plus `ready` and a "fix this first" summary. |
| `create_avd` | Wraps `sdkmanager` + `avdmanager`. ABI auto-detected from host arch. Won't clobber an existing AVD without `force`, won't accept SDK licences on your behalf, verifies via `emulator -list-avds` rather than trusting an exit code. |

The prerequisite checklist is a **mechanism, not a prose reminder** — the same reason selectors are
resolved in the tool rather than described in an agent file. A checklist an agent is told to follow
gets skipped when a run gets long; a `doctor` call does not.

**Not automated, deliberately:** installing the Android SDK, and downloading the community
linux-aarch64 emulator build. The latter is an unsigned third-party binary; whether to put it on a
machine is a human's trust decision, not a tool's. `doctor` detects the gap and points at
`docs/TROUBLESHOOTING.md`, which carries the URL and sha256.

## Explicitly deferred

Named so they don't get rediscovered as gaps:

- **Snapshots.** Every boot is cold (~35–60s). Deferred in both source projects; still deferred.
- **DTU integration.** Compose `digital-twin-universe` later if emulators need containerising.
- **reality-check integration.** A future `type: mobile` acceptance test routing here.
- **Physical devices over Tailscale ADB.** Works today via the same serial contract; port changes on every re-pair, so discovery is out of scope.
