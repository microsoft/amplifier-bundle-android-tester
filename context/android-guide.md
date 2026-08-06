# Android Inspector — Full Reference Guide

Complete reference for agents using the `android_inspector` tool to drive and verify Android applications. Read this before any testing session.

---

## Section 0: The Load-Bearing Constraint

### uiautomator is the sensor. The screenshot is for judgment, never for targeting.

This is not a style preference. It was measured live, on this host, against the same screenshot and the same element (a "Refresh" button):

| Source | Bounds | Center |
|---|---|---|
| `ui_dump` (uiautomator) | `[877,142][1006,195]` | **(941, 168)** |
| VLM reading the PNG | `[810,50][950,100]` | (880, 75) |

Delta: **dx −61px, dy −93px**. The VLM-derived center lands **outside the real button**. The tap misses.

### Why this is worse than it sounds

**The failure is silent.** Android's input system has no concept of "you tapped nothing". A miss produces:

- no error
- no exception
- no non-zero exit code
- a screenshot that often looks plausible

And the *dangerous* case is not landing on nothing — it is landing on a **neighbouring** element. In a recorded run from a source project, a tap intended for the Base-URL field landed on the API-key field. The subsequent `input text` typed a server URL into the API key. The run reported success. The bug surfaced days later when a human opened the app.

### The division of responsibility

| Question | Answer from |
|---|---|
| Where do I tap? | `ui_dump` / `find` — **always** |
| What is on screen right now? | `ui_dump` for structure, `screenshot` for appearance |
| Does this look right? | `screenshot` + vision |
| Is anything clipped, blank, overlapping, mis-coloured? | `screenshot` + vision |
| Did my tap do what I expected? | `ui_dump` before and after — compare |
| Is the data real, or an empty shell? | `logcat` + server logs + in-app status text |

### The rule, stated as a prohibition

**Never derive a tap target from a VLM reading of a screenshot.** Not as an estimate, not as a fallback, not "just to get unstuck". If a selector will not resolve, that is a finding — report it. It is not a licence to guess coordinates from a picture.

`tap_xy` exists for the rare legitimate case (a canvas surface with no accessibility nodes, a deliberate gesture target). It is named to be conspicuous and it emits a warning in its own result. If you find yourself reaching for it because `find` returned nothing, stop and report instead.

---

## Section 1: Tool Operations Reference

All operations take a `serial` (device serial). Every underlying `adb` call is serial-scoped — this is structural, not advisory. An APK was once installed onto the wrong project's emulator because a script picked the first serial in `adb devices`.

### Environment Setup

The bundle assists with setup. It does not install an SDK for you, and it does not download the community linux-aarch64 emulator build — see "The boundary" below.

#### `doctor` — Full host readiness report

```python
report = android_inspector(operation="doctor")
# report["ready"]    — bool: false if ANY check reported "fail"
# report["checks"]   — [{name, status: "ok"|"warn"|"fail", detail, remediation}, ...]
# report["summary"]  — names what to fix FIRST
```

**No required parameters. Never raises. Always returns a full report.** Ten checks:

| Check | What it establishes |
|---|---|
| `host_platform` | Host OS and arch, and the AVD ABI that follows from it |
| `android_home` | `ANDROID_HOME` / `ANDROID_SDK_ROOT` set, and the directory exists |
| `adb_binary` | An adb that actually *executes* — on aarch64 the failure text is the arm64-specific fix, not a generic message |
| `adb_server` | Server reachable; every attached device listed, with `offline` / `unauthorized` flagged |
| `emulator_binary` | Resolves *and* runs. On aarch64 Linux, a failure points at TROUBLESHOOTING.md rather than pretending a download exists |
| `kvm` | `/dev/kvm` present, readable, writable |
| `ptrace_scope` | `kernel.yama.ptrace_scope`. Non-zero is a `warn`, not a blocker — `start_emulator` applies the gdb wrapper itself |
| `gdb_present` | gdb on PATH. `fail` only when `ptrace_scope != 0` makes it load-bearing |
| `avds_available` | Which AVDs exist. None is a `warn` — see `create_avd` |
| `cmdline_tools` | `sdkmanager` and `avdmanager`, needed by `create_avd` |

**It does not stop at the first failure.** Every check runs regardless of what came before, because the point is to show everything wrong at once. Fixing a host one 60-second timeout at a time is precisely the experience this operation exists to delete. Measured **0.26s** on a healthy host.

**`success` is true whenever a report was produced.** A broken machine is a *successful diagnosis*, not a tool error. Read `ready`, not `success`, to decide whether to proceed — and when `ready` is false, report the failing checks with their `remediation` text and stop.

#### `create_avd` — Provision an AVD

```python
result = android_inspector(
    operation="create_avd",
    name="my-harness",       # required — the NEW AVD's name (not `avd`, which names one to boot)
    api_level=35,            # default 35
    tag="google_apis",       # default
    abi=None,                # default: detected from host arch
    device="pixel_6",        # default — avdmanager device profile
    accept_licenses=False,   # default
    force=False,             # default
)
# result["name"], ["package"], ["abi"], ["device"]
# result["downloaded"]           — was a system image fetched?
# result["verification_method"]  — how existence was confirmed afterwards
# result["verified"]             — always True on success; the operation errors otherwise
```

Wraps `sdkmanager` + `avdmanager`. It fails loud at every point where a convenience default would be a decision made on the user's behalf:

| Guard | Behaviour |
|---|---|
| **ABI** | Auto-detected from host arch — `arm64-v8a` on aarch64, `x86_64` otherwise. Detected, never hardcoded. Override only for a deliberately non-native ABI |
| **Existing AVD** | Refuses to clobber one without `force`, and names the AVDs that already exist |
| **SDK licences** | Will not accept them on the user's behalf. If the system image is not already local and `accept_licenses` is false, it stops and says so rather than starting a multi-minute download nobody asked for |
| **Verification** | Confirms via `emulator -list-avds` afterwards rather than trusting the exit code. `avdmanager` reporting success is not proof the AVD is there |

Measured **1.44s** when the system image was already local; the resulting AVD then booted successfully. Add several minutes if the image has to be downloaded.

#### The boundary

Two things are deliberately **not** automated:

- **Installing the Android SDK.** Out of scope.
- **Downloading the community linux-aarch64 emulator build.** It is an unsigned third-party binary; whether it goes on a machine is a human's trust decision, not a tool's. `doctor` detects the gap and points at `docs/TROUBLESHOOTING.md`, which carries the URL and sha256.

### Device & Emulator Lifecycle

#### `list_devices` — Enumerate attached devices

```python
result = android_inspector(operation="list_devices")
# {devices: [{serial, state, model, ...}], count: N}
```

**Offline entries and multi-device ambiguity are errors, not warnings.** If two devices are attached and you have not pinned a serial, the tool refuses rather than guessing. Resolve by naming the serial explicitly in every subsequent call.

#### `start_emulator` — Boot an AVD

```python
result = android_inspector(
    operation="start_emulator",
    avd="my-harness",     # must already exist — see `create_avd`
    port=5556,            # optional; must be even, in 5554–5682
)
serial = result["serial"]   # e.g. "emulator-5556"
```

Applies all host workarounds automatically (headless routing, tracer attachment on restricted-ptrace hosts, detached process launch), then performs the **two-stage readiness gate**:

1. `wait-for-device` (the adb transport is up)
2. poll `sys.boot_completed` until `1` (Android has actually finished booting)
3. `input keyevent 82` to clear the keyguard

Never treat stage 1 as "ready". Between stage 1 and stage 2 the device answers adb but has no launcher, no window manager surface, and an empty `ui_dump`.

Cold boot is ~60s wall clock on a native-arch KVM host. `boot_timeout_s` defaults to 240.

##### Preconditions — all checked before anything is spawned

A bad call costs hundredths of a second, not a timeout. Each of these used to be discovered the slow way.

| Precondition | Behaviour | Measured |
|---|---|---|
| **AVD exists** | Checked from `<name>.ini` on disk, cross-checked against `emulator -list-avds`. On a miss the error carries the name you asked for, **the AVDs that do exist** (so a typo is instantly obvious), and the exact `avdmanager create avd ...` command — plus a pointer to `create_avd` | **0.02s** |
| **`port` is valid** | Must be even and within 5554–5682. The emulator uses `port` for its console and `port + 1` for adb, and adb only scans that range for consoles — an odd or out-of-range port leaves the adb port undiscoverable. Rejected with the constraint explained; never silently adjusted | **0.04s** |
| **`port` is free** | If a device is already attached on the requested port, it **refuses to launch** rather than adopting an instance it did not start | **0.04s** |

That last one is the same hazard as the recorded wrong-device install: operating on someone else's running emulator, silently, because the serial happened to answer. The refusal is the point — choose a different port, or stop whatever is using that one.

`port` is genuinely honoured now. It was previously accepted and discarded, which meant a run that carefully picked a free port got whatever port the emulator chose. When `port` is given, the expected serial is deterministic (`emulator-<port>`) and the tool waits on *that exact serial* rather than the "any new serial appeared" heuristic it must use without one.

##### Failures during boot name the cause, not the symptom

Every wait stage polls the launched process for liveness. If the emulator dies, the error arrives **the moment it dies** and names:

- the **exit code**, or the **signal** that killed it (by name)
- the **log path**
- a **diagnostic excerpt** from that log, inline — so you see the cause without a second round trip

Previously a dead emulator produced a 240s wait and then "adb timed out" — the symptom, reported long after the log had already named the cause. If you get one of these, read the excerpt first: `ptrace_scope` and the `libpcre2` display crash both have distinctive signatures documented in `docs/TROUBLESHOOTING.md`.

#### `stop_emulator` — Shut down

```python
android_inspector(operation="stop_emulator", serial=serial)
```

`adb emu kill` plus a process reap. Always call this on completion, including error paths. Verify no stray qemu processes remain.

### App Lifecycle

#### `install` — Install an APK

```python
android_inspector(
    operation="install",
    serial=serial,
    apk_path="/path/to/app.apk",
)
```

Uses `-r -g`: reinstall over an existing copy, and grant all runtime permissions up front. The `-g` matters — without it your first screen is a permission dialog, not your app.

#### `launch` — Start the app

```python
android_inspector(
    operation="launch",
    serial=serial,
    package="com.example.app",
    activity=".MainActivity",     # optional; omit for monkey -c LAUNCHER
)
```

Waits for `mCurrentFocus` to settle before returning. **Note:** `launch` re-foregrounds an already-running instance rather than relaunching it — the app reappears on whatever screen it was last showing, not its start screen. See "The already-on-this-tab quirk" in Section 5.

#### `stop_app` — Force stop

```python
android_inspector(operation="stop_app", serial=serial, package="com.example.app")
```

`am force-stop`. Use this when you need a genuine cold start rather than a re-foreground.

### Sensing

#### `screenshot` — Capture the screen as an image

```python
snap = android_inspector(operation="screenshot", serial=serial)
# snap["image_path"]  — file path to the PNG (never base64 inline)
# snap["width"], snap["height"]
# snap["bytes"]       — file size, use as a liveness check
```

Returns a **file path**, not inline image data. Pass the path to vision when you need visual judgment.

**Liveness check:** a suspiciously small byte count (a few KB for a full-resolution screen) usually means a solid-colour frame — a black screen, an unrendered surface, or a device that has gone to sleep. Treat it as a signal, not as a valid capture.

**A screenshot is never evidence of a coordinate.** See Section 0.

#### `ui_dump` — Capture the accessibility tree

```python
dump = android_inspector(operation="ui_dump", serial=serial)
# dump["nodes"] = [
#   {
#     "class": "android.widget.EditText",
#     "text": "http://localhost:9000",
#     "desc": "Base URL",             # content-desc
#     "res_id": "com.example:id/url",
#     "bounds": [[877,142],[1006,195]],
#     "center": [941, 168],
#     "focused": False,
#     "clickable": True,
#     "enabled": True,
#   },
#   ...
# ]
```

Returns a **parsed node list**, not raw XML. Centers are pre-resolved — you do not compute them.

This is the source of truth for everything positional. Dump before every tap; never reuse coordinates across screens. A dump is cheap; a silent mis-tap is not.

#### `find` — Resolve a selector to matching nodes

```python
hits = android_inspector(
    operation="find",
    serial=serial,
    selector={"text": "Save"},
)
# hits["nodes"] = [...]  same node shape as ui_dump, with resolved centers
# hits["count"] = N
```

`count == 0` is a finding. `count > 1` means your selector is ambiguous — narrow it before acting.

#### `logcat` — Read the device log

```python
log = android_inspector(
    operation="logcat",
    serial=serial,
    tag="MyAppNetwork",     # optional filter
    lines=200,              # tail length
)
```

The primary instrument for the "is the data real" assertion (Section 6) and for correlating a UI anomaly with what the app actually did.

### Interacting — All Selector-First

#### `tap` — Resolve and tap

```python
result = android_inspector(
    operation="tap",
    serial=serial,
    selector={"res_id": "com.example:id/save"},
)
# result["tapped"]  — the node that was actually hit (class, text, bounds, center)
# result["changed"] — what differs in the tree after the tap
```

The contract, enforced by the tool:

1. `ui_dump` (fresh — not cached)
2. resolve the selector; fail if zero or ambiguous matches
3. tap the resolved center
4. `ui_dump` again
5. report what changed

**You cannot tap without resolving a selector first.** That is the point. An agent instructed to "always dump before tapping" skips it when a run gets long; a tool that structurally cannot do otherwise does not.

Read `result["changed"]`. An empty change set after a tap on a button is a finding — the tap landed but nothing happened, which is different from the tap missing.

#### `type_text` — The verified field-write protocol

```python
result = android_inspector(
    operation="type_text",
    serial=serial,
    selector={"res_id": "com.example:id/url"},
    text="http://localhost:9000",
)
# result["readback"] — what the field actually contains afterwards
# result["verified"] — bool: does readback match the intended text?
```

This is the single most failure-prone operation in Android automation, and the protocol exists because every step of it was learned by breaking something real:

| Step | Why |
|---|---|
| 1. Resolve selector from a fresh dump | Stale coordinates land on the wrong field |
| 2. Tap the resolved center | — |
| 3. **Assert `focused="true"` on the intended node** | A tap can silently land on a *neighbouring* field. Without this assertion your keystrokes go wherever focus already was — no error, just wrong data in the wrong field |
| 4. `KEYCODE_MOVE_END` then N × `KEYCODE_DEL` | Clears existing content deterministically. `input text` *appends* — it does not replace |
| 5. `input text <escaped>` | — |
| 6. **`KEYCODE_BACK` to commit** | Never commit by "tapping elsewhere" — see below |
| 7. Re-dump and **assert readback** | The only proof the value actually landed |

**Step 3 is not optional.** Compose `EditText` nodes frequently do *not* report `clickable="true"` in the accessibility tree, so a naive "find the clickable thing" heuristic misses them entirely and the tap goes somewhere unintended.

**Step 6, on why `KEYCODE_BACK`:** tapping a `TextView` label that visually sits near a *different* `EditText` (the "Poll interval" label sitting directly above the numeric poll-interval field, say) focuses *that other field* instead of dismissing the keyboard. Worse, the resulting IME then overlaps and swallows taps aimed at the bottom nav bar — so your next three interactions also silently fail. `KEYCODE_BACK` dismisses the IME without navigating anywhere in a single-Activity Compose app, and the field value is unaffected because Compose commits character-by-character via `onValueChange`.

If `result["verified"]` is false, **stop**. Do not proceed on the assumption that the field is set. A wrong value in a config field poisons every subsequent assertion in the run.

#### `key` — Send a key event

```python
android_inspector(operation="key", serial=serial, keycode="KEYCODE_BACK")
android_inspector(operation="key", serial=serial, keycode=4)   # numeric also accepted
```

Common codes:

| Name | Code | Use |
|---|---|---|
| `KEYCODE_BACK` | 4 | Dismiss IME, navigate back |
| `KEYCODE_HOME` | 3 | Return to launcher |
| `KEYCODE_MENU` | 82 | Clear the keyguard after boot |
| `KEYCODE_ENTER` | 66 | Submit |
| `KEYCODE_DEL` | 67 | Backspace |
| `KEYCODE_MOVE_END` | 123 | Cursor to end of field |
| `KEYCODE_TAB` | 61 | Focus next |

#### `swipe` — Gesture

```python
android_inspector(
    operation="swipe",
    serial=serial,
    x1=540, y1=1600, x2=540, y2=600,
    duration_ms=300,
)
```

Explicit coordinates are permitted here — gestures have no selector analogue. Derive the endpoints from `ui_dump` bounds (scroll from inside the list container's bounds), not from eyeballing a screenshot.

Scroll-to-find pattern: swipe, `ui_dump`, check for the target selector, repeat with a bounded attempt count. Never assume a fixed number of swipes reaches an element.

#### `tap_xy` — Raw coordinates (conspicuous by design)

```python
android_inspector(operation="tap_xy", serial=serial, x=941, y=168)
# result["warning"] — always present
```

Named to be uncomfortable. Emits a warning in its own result. Legitimate uses are narrow:

- a `SurfaceView` / canvas / game surface with no accessibility nodes
- a deliberate gesture target (a specific point in a map or drawing area)
- reproducing a coordinate that came from `ui_dump` in a prior step within the same screen

**Not a legitimate use:** a selector did not resolve and you want to keep going. That is a finding to report, not a hole to route around.

### Synchronising

#### `wait_for` — Poll until a selector appears or disappears

```python
found = android_inspector(
    operation="wait_for",
    serial=serial,
    selector={"text": "Base URL"},
    timeout_s=15,
    absent=False,       # True = wait for it to *disappear*
)
# {found: bool, elapsed_s: float, node: {...} | None}
```

**No bare sleeps anywhere.** A sleep is either too short (flaky) or too long (slow), and it is never evidence of anything. `wait_for` polls `ui_dump` and returns the elapsed time — which is itself useful data when you are characterising a slow screen.

Wait on the *last* thing to render, not the first. Gating on a header that appears before the list has populated tells you nothing about the list.

#### `dismiss_anr` — Clear an ANR dialog

```python
android_inspector(operation="dismiss_anr", serial=serial)
```

"Application Not Responding" dialogs appear **spontaneously** on headless emulators — reproduced live during this bundle's design, with no provoking action. They steal focus and absorb every subsequent tap.

Call this whenever a run mysteriously stops responding to input, and consider it as the first hypothesis when several interactions in a row produce no change.

---

## Section 2: Selector Syntax

| Selector | Matches | Notes |
|---|---|---|
| `{"text": "Save"}` | exact visible text | Most readable; breaks on localisation and on text that includes dynamic values |
| `{"text_contains": "poll"}` | substring of visible text | Use for text with dynamic parts ("Last poll: 3s ago") |
| `{"res_id": "com.example:id/save"}` | resource id | **Most stable.** Prefer when the app exposes ids |
| `{"desc": "Home"}` | content-desc | The accessibility label. Good for icon-only controls |
| `{"class": "EditText", "index": 0}` | Nth node of a class | Positional and brittle — a last resort. Suffix matching on class is supported (`EditText` matches `android.widget.EditText`) |

Selectors may combine keys — all specified keys must match:

```python
{"class": "EditText", "res_id": "com.example:id/url"}
{"text_contains": "Refresh", "clickable": True}
```

### Choosing a selector — order of preference

1. `res_id` — survives text changes, layout changes, and localisation
2. `desc` — stable for icon controls, and its absence is meaningful (see the tab quirk in Section 5)
3. `text` / `text_contains` — readable but coupled to copy
4. `class` + `index` — brittle; document why you had to

### When a selector does not resolve

`count == 0` has a small set of causes. Check them in this order:

1. **The screen has not finished rendering** → `wait_for` with a real timeout
2. **An ANR or system dialog is on top** → `dismiss_anr`, then re-dump
3. **The node is off-screen** → swipe within the list bounds, re-dump
4. **Compose accessibility gap** → see Section 3
5. **The element genuinely is not there** → **this is your finding.** Report it.

Never resolve a zero-match by estimating coordinates from a screenshot.

---

## Section 3: Compose Accessibility Quirks

Jetpack Compose does not populate the accessibility tree the way the classic View system does. These are the gaps that have actually cost time:

### `clickable` is unreliable

Compose `EditText`-equivalent nodes frequently report `clickable="false"` even though they accept taps. **Do not filter candidate nodes on `clickable=True`** when looking for a text field — you will find nothing and conclude the field is absent.

Filter on `class` containing `EditText`, or on `res_id`, and verify by asserting `focused` after the tap.

### The selected item loses its `content-desc`

The currently-selected bottom-nav tab drops its `content-desc` from the tree. Every *other* tab keeps one. On a Settings screen, `desc="Settings"` is absent while `desc="Items"`, `desc="Chat"` and the rest are all present.

Consequence: a naive `{"desc": "Settings"}` lookup **falsely fails whenever you are already on Settings**. And because `launch` re-foregrounds rather than relaunches, the app frequently reopens onto the tab it last showed — so this is the common case, not the edge case.

**The correct pattern:** treat "tab not found by desc" as *possibly already there*, and confirm with a screen-specific text marker before calling it an error.

```python
hits = android_inspector(operation="find", serial=serial, selector={"desc": "Settings"})
if hits["count"] == 0:
    # Might already be on Settings — check for a marker unique to that screen
    marker = android_inspector(operation="find", serial=serial,
                               selector={"text": "Base URL"})
    if marker["count"] == 0:
        report("Cannot reach Settings tab: no desc match and no screen marker")
    # else: already on Settings, proceed
else:
    android_inspector(operation="tap", serial=serial, selector={"desc": "Settings"})
```

### Text nodes may merge or split unpredictably

Compose semantics can merge a composable's children into a single node, or leave them separate, depending on `mergeDescendants`. A label and its value may appear as one node with concatenated text, or as two. Prefer `text_contains` over `text` when asserting on a label-plus-value string.

### Bounds are correct even when semantics are odd

Whatever the tree's quirks about `clickable` and `desc`, **the `bounds` are accurate**. This is precisely why `ui_dump` is the sensor and the screenshot is not.

---

## Section 4: Synchronisation Patterns

### The golden rule: gate on state, never on time

Bad — brittle and slow at the same time:

```python
android_inspector(operation="tap", serial=serial, selector={"text": "Refresh"})
time.sleep(3)                                       # arbitrary; still flaky
android_inspector(operation="screenshot", serial=serial)
```

Good:

```python
android_inspector(operation="tap", serial=serial, selector={"text": "Refresh"})
android_inspector(operation="wait_for", serial=serial,
                  selector={"text_contains": "Last updated"}, timeout_s=10)
android_inspector(operation="screenshot", serial=serial)
```

### Boot readiness is two-stage — never one

```python
r = android_inspector(operation="start_emulator", avd="my-harness", port=5556)
serial = r["serial"]
# start_emulator already did: wait-for-device → poll sys.boot_completed → keyevent 82
# Now gate on YOUR app, separately:
android_inspector(operation="launch", serial=serial, package="com.example.app")
android_inspector(operation="wait_for", serial=serial,
                  selector={"res_id": "com.example:id/root"}, timeout_s=30)
```

`wait-for-device` alone means "adb can talk to it" — the device has no launcher and an empty UI tree at that point. Boot-completed alone means Android is up but the keyguard may still be swallowing input.

### Waiting for something to disappear

```python
android_inspector(operation="wait_for", serial=serial,
                  selector={"text": "Loading"}, absent=True, timeout_s=20)
```

Often more reliable than waiting for content to appear, because a spinner has a definite identity while "the list has data" is a fuzzy predicate.

### Bounded scroll-to-find

```python
for attempt in range(8):
    hits = android_inspector(operation="find", serial=serial,
                             selector={"text": "Advanced"})
    if hits["count"] > 0:
        break
    android_inspector(operation="swipe", serial=serial,
                      x1=540, y1=1600, x2=540, y2=800, duration_ms=300)
else:
    report("'Advanced' not reachable after 8 scroll attempts")
```

Always bounded. An unbounded scroll loop against a list that never contains the target is indistinguishable from a hang.

---

## Section 5: Common Workflow Patterns

### Pattern 1 — Boot, install, launch, verify

```python
r = android_inspector(operation="start_emulator", avd="my-harness", port=5556)
serial = r["serial"]

android_inspector(operation="install", serial=serial, apk_path="/tmp/app.apk")
android_inspector(operation="launch", serial=serial, package="com.example.app")
android_inspector(operation="wait_for", serial=serial,
                  selector={"res_id": "com.example:id/root"}, timeout_s=30)

dump = android_inspector(operation="ui_dump", serial=serial)
snap = android_inspector(operation="screenshot", serial=serial)
# dump → structural assertions.  snap → visual judgment.

android_inspector(operation="stop_emulator", serial=serial)
```

### Pattern 2 — Configure a settings screen (the verified field write)

```python
# Reach the screen (handling the desc quirk from Section 3)
android_inspector(operation="wait_for", serial=serial,
                  selector={"text": "Base URL"}, timeout_s=10)

# Write the field — the tool enforces focus assertion and readback
r = android_inspector(operation="type_text", serial=serial,
                      selector={"res_id": "com.example:id/url"},
                      text="http://localhost:9000")
if not r["verified"]:
    report(f"Base URL did not take. Readback: {r['readback']!r}")
    return    # do NOT continue — every later assertion is now untrustworthy

r = android_inspector(operation="type_text", serial=serial,
                      selector={"res_id": "com.example:id/api_key"},
                      text=api_key)
if not r["verified"]:
    report(f"API key did not take. Readback: {r['readback']!r}")
    return

android_inspector(operation="tap", serial=serial, selector={"text": "Test connection"})
android_inspector(operation="wait_for", serial=serial,
                  selector={"text_contains": "Connected"}, timeout_s=15)
```

**Idempotence:** read the field's current value from `ui_dump` first and skip the write if it already matches. Re-running a configure step should be a no-op, not a re-type.

### Pattern 3 — Walk every tab and capture

```python
tabs = ["Items", "Home", "Chat", "Settings"]
captures = {}
for tab in tabs:
    hits = android_inspector(operation="find", serial=serial, selector={"desc": tab})
    if hits["count"] == 0:
        # Selected tab loses its desc — verify with a screen marker (Section 3)
        marker = android_inspector(operation="find", serial=serial,
                                   selector={"text": MARKERS[tab]})
        if marker["count"] == 0:
            report(f"Cannot reach tab {tab}")
            continue
    else:
        android_inspector(operation="tap", serial=serial, selector={"desc": tab})
        android_inspector(operation="wait_for", serial=serial,
                          selector={"text": MARKERS[tab]}, timeout_s=10)
    captures[tab] = android_inspector(operation="screenshot", serial=serial)
```

### Pattern 4 — Before/after comparison of a fix

```python
snap_before = android_inspector(operation="screenshot", serial=serial)
dump_before = android_inspector(operation="ui_dump", serial=serial)

android_inspector(operation="tap", serial=serial, selector={"text": "Refresh"})
android_inspector(operation="wait_for", serial=serial,
                  selector={"text_contains": "Last updated"}, timeout_s=10)

snap_after = android_inspector(operation="screenshot", serial=serial)
dump_after = android_inspector(operation="ui_dump", serial=serial)

# Structural diff from the dumps (which nodes appeared/disappeared/changed text)
# Visual diff from the screenshots (what a human would notice)
```

Use **both**. The dump tells you what changed in the tree; the screenshot tells you whether the result looks acceptable. Neither alone is sufficient.

### The "already on this tab" quirk (restated because it bites)

`launch` re-foregrounds a running app rather than relaunching it, so the app reappears on the screen it last showed. Combined with the selected-tab-loses-its-desc behaviour, a naive tab-navigation loop fails on its first iteration whenever the app happens to already be on that tab. Always confirm with a screen marker before reporting a navigation failure.

---

## Section 6: The "Data Is Real" Assertion

**A screenshot alone does not prove the data is real.**

A screen showing an empty list, a screen showing stale cached data, and a screen showing live data from a healthy backend can be visually indistinguishable — especially to a VLM, which will happily describe an empty shell as "the items list rendering correctly".

This is the failure mode the whole bundle exists to catch. Every capture that claims to show working data needs a second, independent source of evidence.

### The three instruments

#### 1. Correlate with the server log

If the app talks to a server you control, the server's access log is direct proof that the app made the request:

```python
android_inspector(operation="logcat", serial=serial, tag="OkHttp", lines=100)
```

and on the host side, tail the server log and confirm requests arrived from the device (via `adb reverse tcp:PORT tcp:PORT`, requests appear from `127.0.0.1`; via the emulator's host alias `10.2.2.2`/`10.0.2.2`, from that address).

Assert the *specific endpoints* you expect for the screen under test — `/api/items/open` for the items tab, `/api/status` for the home tab. "Some requests happened" is weaker evidence than "the request this screen needs happened, just now".

#### 2. Read the in-app status line

Well-built apps expose their own liveness. Look for and assert on it in the dump, not the screenshot:

```python
hits = android_inspector(operation="find", serial=serial,
                         selector={"text_contains": "Last successful poll"})
# "Last successful poll: just now"  → the app reached the server
# "Last successful poll: never"     → it did NOT, regardless of what the screen shows
```

`Last successful poll: never` combined with a screen full of plausible-looking rows means you are looking at seeded, cached, or placeholder data.

#### 3. Check logcat for swallowed errors

```python
log = android_inspector(operation="logcat", serial=serial, lines=300)
```

An app that catches its network exceptions and renders an empty state produces a *clean-looking screen* and a *loud logcat*. Read the log before declaring a pass.

### The assertion, as a checklist

Before reporting that a screen shows working data, you must have:

- [ ] a `ui_dump` showing the expected content nodes (not just a container)
- [ ] at least one independent confirmation: server-log correlation, in-app status line, or logcat evidence of a successful request
- [ ] no error-level logcat entries for the app's tag during the interaction window

If you have the screenshot and nothing else, the honest report is **"the screen renders; I could not confirm the data is live"** — not "verified".

---

## Section 7: Safety Invariants

These four rules were each learned by breaking something real. The tool enforces them structurally; this section explains *why*, so you recognise the failure when you see it in the wild.

### 1. Every adb call carries `-s <serial>`

An APK was installed onto a different project's running emulator because a script took the first entry from `adb devices`. Two AVDs on one host is the normal case, not the exotic one.

If `list_devices` reports more than one device and you have not pinned a serial, **stop and ask** — do not pick one.

### 2. Dump before every tap; never reuse coordinates across screens

Coordinates are valid for exactly one rendered state. A list that scrolled by one row, a snackbar that appeared, a keyboard that opened — any of these invalidates every coordinate you were holding.

### 3. After tapping a text field, assert `focused="true"` before typing

Without it, keystrokes go wherever focus already was. Silent, and the resulting data corruption is discovered by a human, later, in production.

### 4. Commit a field with `KEYCODE_BACK`, never by "tapping elsewhere"

"Elsewhere" is another element. Tapping a label focuses the field beneath it and leaves the IME up, overlapping the bottom nav so your next taps are swallowed too.

### Why these are in the tool and not just in prose

Prose decays under pressure. An agent instructed to follow four rules will skip them at turn 40 of a long run, exactly when the run is hardest to debug. A tool that *cannot* tap without resolving a selector first makes the discipline structural. When you notice yourself wanting to route around one of these, that impulse is the signal the rule is working.
