# Android Testing (android-tester)

This bundle drives Android applications on emulators and physical devices via the `android_inspector` tool — accessibility-tree-driven interaction with screenshot-based visual verification.

## The One Rule That Matters

**uiautomator is the sensor. The screenshot is for judgment, never for targeting.**

Coordinates come from `ui_dump`. Screenshots answer "does this look right / what state am I in / is anything clipped" — never "where do I tap". A VLM reading a PNG mislocated a real button by 61px horizontally and 93px vertically on this host; the derived tap center fell outside the button. **The miss is silent** — a tap that lands on nothing, or on a neighbouring field, raises no error. One recorded run typed a server URL into the API-key field and reported success.

## Available Agents

| Agent | Use For | Example Triggers |
|-------|---------|-----------------|
| `android-tester:android-operator` | Boot emulator, install APK, launch app, drive the UI, verify behaviour end-to-end | "Install the APK and check the settings screen saves", "Boot the emulator and walk every tab", "Verify the login flow on device" |
| `android-tester:android-visual-tester` | Screenshot sweeps, clipping/blank/overlap detection, before/after visual comparison | "Screenshot every tab and tell me what looks broken", "Did my layout fix actually land?", "Check the list doesn't clip at the bottom" |
| `android-tester:android-debugger` | Root-cause anomalies: why a tap did nothing, why a field didn't take, frame/logcat correlation | "Why doesn't the Save button do anything?", "The screen is blank after navigation", "Text I typed disappeared" |

## Division of Labour

This bundle covers **on-device and emulator Android UI only**. Route elsewhere for:

| Target | Bundle |
|---|---|
| Web UI, browsers, SPAs | `browser-tester` |
| TUI and CLI applications | `terminal-tester` |
| Amplifier ecosystem itself (bundles, agents, sessions) | `amplifier-tester` |
| **Android app running on an emulator or device** | **this bundle** |

## Delegate — Do Not Drive adb Yourself

**Do not run `adb`, `uiautomator`, or `emulator` commands directly from the root session.** Always delegate to an android-tester agent.

The agents hold the procedural safety that raw adb does not enforce: serial scoping on every call, dump-before-tap, focus assertion before typing, `KEYCODE_BACK` to commit fields, and the aarch64 host workarounds. Driving adb by hand is how the two source projects silently installed an APK onto the wrong emulator and typed a URL into the wrong field.

## Prerequisites

- Android SDK at `ANDROID_HOME` (default `~/android-sdk`) with `emulator` and a working `adb`
- A pre-existing AVD — this bundle does not provision AVDs
- `/dev/kvm` readable and writable
- On aarch64 Linux: native-arch `adb` (`platform-tools/adb` from Google is x86_64-only) and a linux-aarch64 emulator build (Google ships none)

If prerequisites are missing, the agents report the exact fix and stop — they do not improvise workarounds.
