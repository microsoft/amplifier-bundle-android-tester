---
bundle:
  name: android-tester
  version: 0.1.0
  description: Android app testing on emulators and devices — accessibility-tree-driven interaction with visual verification

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: android-tester:behaviors/android-tester

---

# Android Tester

Test, drive, and debug Android applications on emulators and physical devices from within Amplifier sessions.

Closes the gap that unit tests and server-side `curl` cannot: **the failure is always at the render/interaction layer, and no agent has ever seen the screen.**

## The Load-Bearing Rule

**uiautomator is the sensor. The screenshot is for judgment, never for targeting.**

Tap coordinates come from `ui_dump` (the accessibility tree). Screenshots answer *"does this look right, is anything clipped, what state am I in"* — they never produce coordinates. A VLM reading a PNG is off by tens of pixels, and a tap that misses raises no error: it silently lands on nothing, or on a neighbouring field. Measured on this exact host, a VLM-derived tap center for a "Refresh" button landed 61px left and 93px above the real button — outside it entirely.

## How It Works

The `android_inspector` tool wraps `adb` and `uiautomator` with a selector-first contract. Every interaction resolves a selector against a live accessibility dump before touching the screen — the safe path is the default path, and raw coordinates (`tap_xy`) require conspicuous opt-in.

Three specialist agents drive it: an **operator** (boot → install → launch → interact → verify), a **visual tester** (screenshot sweeps, clipping and regression detection), and a **debugger** (root-cause analysis for anomalies).

## Prerequisites

- An Android SDK at `ANDROID_HOME` (default `~/android-sdk`) with an `emulator` binary and a working `adb`
- A pre-existing AVD (this bundle does not provision AVDs — it fails loudly with the `avdmanager` command if none exists)
- `/dev/kvm` readable and writable for hardware acceleration
- On aarch64 Linux hosts: a native-arch `adb` and a linux-aarch64 emulator build — see `android-tester:docs/TROUBLESHOOTING.md`

@foundation:context/shared/common-system-base.md
