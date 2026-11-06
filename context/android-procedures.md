# Android Testing (android-tester)

Drives Android apps on emulators and physical devices via the `android_inspector` tool — accessibility-tree-driven interaction with screenshot-based visual verification.

**uiautomator is the sensor. The screenshot is for judgment, never for targeting.** Coordinates come from `ui_dump`. Screenshots answer "does this look right / what state am I in / is anything clipped" — never "where do I tap": a VLM reading a PNG mislocated a real button by 61px horizontally and 93px vertically on this host, and the derived tap center fell outside the button. **The miss is silent** — a tap that lands on nothing, or on a neighbouring field, raises no error; one recorded run typed a server URL into the API-key field and reported success.

**Delegate — do not run `adb`, `uiautomator` or `emulator` from the root session:**

- `android-tester:android-operator` — boot emulator, install APK, launch app, drive the UI, verify behaviour end-to-end. "Install the APK and check the settings screen saves", "verify the login flow on device".
- `android-tester:android-visual-tester` — screenshot sweeps, clipping/blank/overlap detection, before/after visual comparison. "Screenshot every tab and tell me what looks broken", "did my layout fix land?".
- `android-tester:android-debugger` — root-cause anomalies: why a tap did nothing, why a field didn't take, frame/logcat correlation. "The screen is blank after navigation", "text I typed disappeared".

They hold the procedural safety raw adb does not enforce: serial scoping on every call, dump-before-tap, focus assertion before typing, `KEYCODE_BACK` to commit fields, aarch64 host workarounds. Driving adb by hand is how the two source projects silently installed an APK onto the wrong emulator and typed a URL into the wrong field. Before any destructive operation (reinstall, `pm clear`, reboot) they also branch on serial *shape* — `emulator-*` vs `<ip>:<port>` — because a physical device is not recoverable by re-running the test.

Scope: on-device and emulator Android UI only. Web UI, browsers, SPAs → `browser-tester`; TUI and CLI apps → `terminal-tester`; the Amplifier ecosystem itself (bundles, agents, sessions) → `amplifier-tester`.

Prerequisites: Android SDK at `ANDROID_HOME` (default `~/android-sdk`) with `emulator` and a working `adb`; `/dev/kvm` readable and writable; on aarch64 Linux a native-arch adb (Google's `platform-tools/adb` is x86_64-only) and a linux-aarch64 emulator build (Google ships none). Missing prerequisites are a supported path, not a dead end: `doctor` reports every host problem at once, each with its fix, and `create_avd` provisions an AVD — but the bundle does not install the SDK, and the agents report the exact fix and stop rather than improvise a workaround.
