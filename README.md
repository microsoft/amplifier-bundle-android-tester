# amplifier-bundle-android-tester

**Let an agent actually see and drive the Android screen** — so UI fixes stop shipping "verified" and arriving broken.

Unit tests pass. Server-side `curl` passes. The user opens the app and every one of the last four fixes is broken. The failure is always at the render/interaction layer, and nothing in the loop has ever looked at the screen. This bundle closes that gap.

## The Load-Bearing Rule

**uiautomator is the sensor. The screenshot is for judgment, never for targeting.**

Measured live on an aarch64 host — same screenshot, same "Refresh" button:

| Source | Bounds | Center |
|---|---|---|
| `uiautomator dump` | `[877,142][1006,195]` | **(941, 168)** |
| VLM reading the PNG | `[810,50][950,100]` | (880, 75) |

Delta **dx −61px, dy −93px** — the VLM-derived center lands **outside the real button**.

And the miss is **silent**. Android has no concept of "you tapped nothing": no error, no exception, and often a screenshot that still looks right. The dangerous case is landing on a *neighbouring* element — in a recorded run, a tap meant for the Base-URL field hit the API-key field, and the run typed a server URL into the API key and reported success.

So: vision answers *"does this look right / what state am I in / is anything clipped"*. **Coordinates always come from `ui_dump`.**

## How It Works

A single `android_inspector` tool wraps `adb` and `uiautomator` with a **selector-first** contract — the safe path is the default path:

- `tap` cannot fire without resolving a selector against a **fresh** accessibility dump
- `type_text` asserts `focused="true"` before typing and asserts readback after
- `wait_for` polls the tree; there are no bare sleeps anywhere
- `tap_xy` (raw coordinates) is named to be conspicuous and warns in its own result

These four rules were each learned by breaking something real. An agent *told* to follow them skips them at turn 40 of a long run. A tool that structurally *cannot* skip them does not.

## Quick Start

### Installation

**Add as an app bundle (recommended):**
```bash
amplifier bundle add git+https://github.com/microsoft/amplifier-bundle-android-tester@main#subdirectory=behaviors/android-tester.yaml --app
```

**Compose into another bundle:**
```yaml
includes:
  - bundle: git+https://github.com/microsoft/amplifier-bundle-android-tester@main#subdirectory=behaviors/android-tester.yaml
    as: android-tester
```

### Prerequisites

**Start by asking the bundle.** `doctor` takes no parameters, never errors, and tells you everything that is wrong in one call:

```python
report = android_inspector(operation="doctor")
# report["ready"]   — false if any check failed
# report["checks"]  — [{name, status: ok|warn|fail, detail, remediation}, ...]
# report["summary"] — what to fix first
```

Ten checks — host arch/OS, `ANDROID_HOME`, adb binary, adb server and attached device states, emulator binary, KVM, `ptrace_scope`, gdb, AVDs available, cmdline-tools. It deliberately **does not stop at the first failure**: you get the whole picture and fix the host in one pass, instead of discovering its problems one 60-second timeout at a time. Measured 0.26s on a healthy host. A broken machine is a *successful diagnosis*, not a tool error — read `ready`, not `success`.

No AVD? `create_avd` provisions one. ABI is auto-detected from the host arch, it will not clobber an existing AVD without `force`, it will not accept SDK licences on your behalf without `accept_licenses`, and it verifies with `emulator -list-avds` afterwards rather than trusting an exit code:

```python
android_inspector(operation="create_avd", name="my-harness")   # 1.44s with the image already local
```

**What the bundle will not do for you**, and why:

| Not automated | Why |
|---|---|
| Installing the Android SDK | Out of scope |
| Downloading the community linux-aarch64 emulator build | It is an **unsigned third-party binary**. Whether it goes on a machine is a human's trust decision, not a tool's. `doctor` detects the gap and points you at [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md), which carries the URL and sha256 |

So the underlying requirements remain: an Android SDK at `ANDROID_HOME` (default `~/android-sdk`), a working `adb`, an `emulator` binary, and `/dev/kvm` readable and writable.

**On aarch64 Linux hosts, two things Google does not ship** — `doctor` reports both by name:

| Need | Problem | Fix |
|---|---|---|
| `adb` | `platform-tools/adb` is x86_64-only → `Exec format error` | Native-arch adb (e.g. extracted from Ubuntu's arm64 debs) at `platform-tools-arm64/` |
| `emulator` | No linux-aarch64 emulator exists in Google's SDK repo | A community linux-aarch64 build + hand-written `package.xml` |

Full detail, including the `libpcre2` boot crash and the `ptrace_scope` workaround, in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

### Basic Usage

Delegate to an agent — do not drive `adb` from the root session.

```python
# Boot (applies host workarounds + the two-stage readiness gate)
r = android_inspector(operation="start_emulator", avd="my-harness", port=5556)
serial = r["serial"]

android_inspector(operation="install", serial=serial, apk_path="/tmp/app.apk")
android_inspector(operation="launch", serial=serial, package="com.example.app")

# Gate on state, never on time
android_inspector(operation="wait_for", serial=serial,
                  selector={"res_id": "com.example:id/root"}, timeout_s=30)

# Coordinates come from the tree — this resolves, taps, re-dumps, reports what changed
android_inspector(operation="tap", serial=serial, selector={"text": "Settings"})

# The verified field write: focus assertion + readback
r = android_inspector(operation="type_text", serial=serial,
                      selector={"res_id": "com.example:id/url"},
                      text="http://localhost:9000")
assert r["verified"], f"field write failed, readback: {r['readback']!r}"

# Screenshot for judgment — never for coordinates
snap = android_inspector(operation="screenshot", serial=serial)

android_inspector(operation="stop_emulator", serial=serial)
```

## Agents

### `android-operator` (primary) — `[coding, general]`

The driver: boot → install → launch → interact → verify → report. Owns the emulator lifecycle, the verified field-write protocol, and the "is this data real" assertion. Prerequisites self-check, numbered workflow with wait-gates, 3-attempt failure budget.

Use for: installing and exercising an app, verifying a fix landed on device, configuring settings over adb, end-to-end flow testing.

### `android-visual-tester` — `[vision, critique, general]`

Visual quality judgment: screenshot sweeps, clipping / blank-region / overlap / truncation detection, before-and-after comparison. Its sharpest instrument is **dump-vs-render reconciliation** — the tree says where a node is and what it says; the image says what actually got drawn. Disagreement between them is a defect with exact geometry attached.

Use for: "screenshot every tab and tell me what looks broken", layout regression sweeps, confirming a styling fix is visible.

### `android-debugger` — `[coding, reasoning, general]`

Anomaly root-cause via frame diffing, focus tracing, and logcat correlation. Its core job is separating four failures that all present as "I tapped it and nothing happened": tap missed · tap landed but the handler did nothing · something intercepted it · handler ran but the UI did not update. Different causes, different fixes.

Use for: non-responsive controls, text that lands in the wrong field, blank screens, interactions that used to work.

## Tool Operations Reference

| Operation | Description | Key params |
|-----------|-------------|-----------|
| `doctor` | 10-check host readiness report; runs every check, never errors | — |
| `create_avd` | Provision an AVD (ABI auto-detected; won't clobber or auto-accept licences) | `name`, `api_level`, `tag`, `abi`, `device` |
| `list_devices` | Enumerate devices; ambiguity is an **error** | — |
| `start_emulator` | Boot AVD with host workarounds + readiness gate. Missing AVD, invalid port, and occupied port all fail **before** anything spawns | `avd`, `port` |
| `stop_emulator` | `adb emu kill` + process reap | `serial` |
| `install` | Install APK with `-r -g` | `serial`, `apk_path` |
| `launch` | Start app, wait for focus to settle | `serial`, `package` |
| `stop_app` | `am force-stop` | `serial`, `package` |
| `screenshot` | PNG **file path**, geometry, byte size | `serial` |
| `ui_dump` | Parsed node list with resolved centers | `serial` |
| `find` | Nodes matching a selector | `serial`, `selector` |
| `logcat` | Tail / filter the device log | `serial`, `tag`, `lines` |
| `tap` | dump → resolve → tap → re-dump → report change | `serial`, `selector` |
| `type_text` | The verified field-write protocol | `serial`, `selector`, `text` |
| `key` | Key event by name or code | `serial`, `keycode` |
| `swipe` | Gesture (explicit coords permitted) | `serial`, `x1,y1,x2,y2` |
| `tap_xy` | **Raw coordinates — conspicuous by design** | `serial`, `x`, `y` |
| `wait_for` | Poll the tree until a selector appears/disappears | `serial`, `selector`, `timeout_s` |
| `dismiss_anr` | Clear a spontaneous ANR dialog | `serial` |

## Selector Syntax

```python
{"res_id": "com.example:id/save"}        # most stable — prefer this
{"desc": "Home"}                          # content-desc; good for icon controls
{"text": "Save"}                          # exact visible text
{"text_contains": "poll"}                 # substring — for dynamic text
{"class": "EditText", "index": 0}         # positional — last resort
```

Keys combine; all specified keys must match.

## Key Design Decisions

### Why a tool module instead of agent prose

The hard-won knowledge here is **procedural safety**, and prose decays under pressure. Four rules — serial-scope every adb call, dump before every tap, assert focus before typing, commit with `KEYCODE_BACK` — were each learned by breaking something real. Encoding them in a tool that *cannot* do otherwise fixes the mechanism instead of restating the reminder.

### Why the accessibility tree and not vision

See the measurement at the top. This is the single decision every other one follows from.

### Why `-no-window` is on by default

It is a **crash workaround, not an optimisation.** On this class of host, the windowed qemu binary needs `libpcre2-16.so.0` and segfaults during display setup without it; headless routes to a different binary with no Qt dependency. Removing the flag to "get a visible window" reintroduces a deterministic segfault whose log points at display configuration rather than at the missing library.

### Why setup is a tool call, not a checklist

The prerequisite checklist used to live as prose in the agent files. That is the same mistake as describing selector resolution instead of enforcing it: a checklist an agent is told to follow gets skipped when a run gets long. `doctor` is the checklist made structural — one call, every finding, with remediation attached.

### Explicitly deferred

Named so they are not rediscovered as gaps: **snapshots** (every boot is cold, ~60s), **SDK installation and the community aarch64 emulator download** (`doctor` detects and explains both; installing them is the human's call), **containerised emulators** (compose `digital-twin-universe` later), **physical-device discovery over Tailscale ADB** (works via the same serial contract; the port changes on every re-pair).

## Related Bundles

| Target | Bundle |
|---|---|
| Web UI, browsers, SPAs | `browser-tester` |
| TUI and CLI applications | `terminal-tester` |
| Amplifier ecosystem itself | `amplifier-tester` |
| **Android app on emulator or device** | **this bundle** |

## Getting Help

For emulator boot failures, aarch64 host setup, and interaction anomalies, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

For general Amplifier questions:

- [Amplifier GitHub](https://github.com/microsoft/amplifier)
- [Amplifier Documentation](https://github.com/microsoft/amplifier/tree/main/docs)

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
