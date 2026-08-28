# Android Inspector — Troubleshooting Guide

Field knowledge from real sessions on an aarch64 Linux host (DGX Spark, Ubuntu 24.04, native `aarch64` + `/dev/kvm`). Roughly 57 bash calls of yak-shaving went into the emulator-boot section alone. It is written down here so nobody repeats it.

Most of these workarounds are **owned by the tool** — `start_emulator` applies them automatically. This document explains *why*, so you recognise the symptom when the tool's automation is not in the path (a manual `adb` invocation, a different host, a future SDK version that moves things again).

**See also:** [FIELD-NOTES-2026-08.md](FIELD-NOTES-2026-08.md) — wireless-debugging pairing, multi-lane emulator hygiene, `adb shell` data-extraction gotchas, self-hosted update rails, and evidence-provenance discipline, from a later extended real-device project.

---

## Quick Reference: Symptom → Cause → Fix

**Before working through this table by hand, run `doctor`** — one call, no parameters, ~0.26s, and it reports every host problem at once with its remediation instead of one per failed operation. Most rows below are things `doctor` names for you.

| Symptom | Cause | Fix |
|---|---|---|
| `Exec format error` running `adb` | Google's `platform-tools/adb` is x86_64-only; no aarch64 build is shipped | Use a native-arch adb (`platform-tools-arm64/adb`). See [Wrong-architecture adb](#wrong-architecture-adb) |
| `sdkmanager emulator` → `Failed to find package 'emulator'` | Google ships **no** linux-aarch64 emulator in the SDK repo | Community linux-aarch64 build + hand-written `package.xml`. See [No aarch64 emulator](#no-aarch64-emulator-from-google) |
| `ci.android.com` emulator artifact → `NoSuchKey` | Last linux_aarch64 build was 2025-01-16; artifacts purged by retention | Route is dead. Do not retry it. See [Dead ci.android.com route](#the-dead-ciandroidcom-route) |
| Emulator SIGSEGV immediately after `Setting display: 0 configuration` | Windowed qemu binary needs `libpcre2-16.so.0`, absent on host | `-no-window` (routes to the headless binary). See [The libpcre2 boot crash](#the-libpcre2-boot-crash) |
| Emulator dies at startup with no useful log, 100% reproducible | `kernel.yama.ptrace_scope=1` breaks the emulator's self-ptracing crash handler | Launch under `gdb -batch` with `handle all nostop noprint pass`. See [The ptrace_scope crash](#the-ptrace_scope-crash) |
| gdb kills a *healthy* booting emulator | `handle SIGSEGV ...` alone; gdb still stops on SIGUSR1/SIGUSR2/SIGCONT, and `confirm off` auto-answers "kill it?" | `handle all nostop noprint pass` — `all` is load-bearing. See [gdb kills a healthy emulator](#gdb-kills-a-healthy-emulator) |
| Emulator process vanishes when the tool call returns | Backgrounded processes are reaped with the tool call's process tree | `setsid` + `</dev/null` + full detachment, not `&`. See [Backgrounded processes die](#backgrounded-processes-die) |
| `Could not init 'oss' audio driver` | Host audio backend fallback warning | Harmless. See [Audio driver warning](#audio-driver-warning) |
| Device answers adb but `ui_dump` is empty | `wait-for-device` returned before Android finished booting | Two-stage gate. See [Readiness is two-stage](#readiness-is-two-stage) |
| Boot never completes / hangs past 4 minutes | Keyguard, missing KVM, or a genuine boot failure | See [Boot timing expectations](#boot-timing-expectations) |
| `start_emulator` error naming an exit code or signal, a log path, and log lines | The emulator process **died during boot**; the wait stages detected it immediately | Read the excerpt in the error. See [Emulator process died during boot](#emulator-process-died-during-boot) |
| `start_emulator` refuses to launch: port already attached | A device already answers on that console port | Choose another port, or stop what is using it. See [Port already in use](#port-already-in-use-or-invalid) |
| `start_emulator` rejects the `port` value outright | `port` must be even and within 5554–5682 | See [Port already in use](#port-already-in-use-or-invalid) |
| `start_emulator` fails instantly naming other AVDs | The requested AVD does not exist (checked before spawning) | Fix the typo, or `create_avd`. See [AVD does not exist](#avd-does-not-exist) |
| APK installed onto the wrong emulator | Script picked the first `emulator-NNNN` in `adb devices` | Always pin `serial`. See [Wrong device targeted](#wrong-device-targeted) |
| Interactions silently stop working mid-run | Spontaneous ANR dialog stealing focus | `dismiss_anr`. See [Spontaneous ANR dialogs](#spontaneous-anr-dialogs) |
| Typed text landed in the wrong field | Tap missed; no focus assertion before typing | See [Text went into the wrong field](#text-went-into-the-wrong-field) |
| Tap "succeeded" but nothing happened | Coordinates derived from a screenshot, not from `ui_dump` | See [The tap missed and nothing told you](#the-tap-missed-and-nothing-told-you) |
| Screen renders but data may be fake | No independent confirmation | See [Screenshot shows plausible but fake data](#screenshot-shows-plausible-but-fake-data) |

---

## Host Setup Problems (aarch64 Linux)

### Wrong-architecture adb

**Symptom:** `adb: Exec format error`, or `adb` silently fails to run at all.

**Cause:** Google's `platform-tools` package ships an **x86_64-only** `adb`. There is no aarch64 build in the SDK repo. On an aarch64 host, `~/android-sdk/platform-tools/adb` cannot execute.

**Fix:** Obtain a native aarch64 `adb` and point at it explicitly. Ubuntu ships one; it can be extracted without root:

```bash
mkdir -p ~/android-sdk/platform-tools-arm64 && cd /tmp
apt-get download adb android-libbase android-libboringssl \
                 android-libcutils android-liblog android-libziparchive
for d in *.deb; do dpkg -x "$d" /tmp/adb-extract; done
# copy the binary + its libs into platform-tools-arm64/, with a wrapper that
# sets LD_LIBRARY_PATH to the extracted libs
```

**Do not overwrite `platform-tools/adb` in place.** Keep the original and put the working binary in a sibling directory (`platform-tools-arm64/`), then put *that* on `PATH` ahead of `platform-tools`. An in-place overwrite is silently undone the next time `sdkmanager` updates platform-tools.

The tool probes for a working `adb` at both locations and **fails loudly with this fix** if neither works. It does not guess.

**Verify:** `adb version` should print a version, not an exec-format error.

### No aarch64 emulator from Google

**Symptom:** `sdkmanager emulator` → `Failed to find package 'emulator'`. The failure cascades: system-image installs are also blocked until a directory with a valid `package.xml` exists at `$ANDROID_HOME/emulator`.

**Cause:** Verified in the SDK repo's `repository2-3.xml` — emulator archives exist only for `linux/x64`, `macosx/x64`, `macosx/aarch64`, and `windows/x64`. **There is no `linux_aarch64` emulator in Google's SDK repo**, in either the stable or canary channel.

**Fix:** Use a community linux-aarch64 build. The one proven on this host:

- Source: `github.com/Changqing-JING/android-emulator-aarch64-linux`
- Release: `v2.12.0-19097-g85fa07f04ef`, asset `sdk-repo-linux_aarch64-emulator-standalone-0.zip` (built 2026-01-22)
- Emulator version 35.6.3 (`build_id standalone-0`)
- sha256 `67ce4f576687b067d9b36668405b86b9749745d67d31037616aac12c49c63a65`

Unzip into `$ANDROID_HOME/emulator` and hand-write a `package.xml` (template from chromium.googlesource `android_tools`, revision edited to `35.6.3`) so `sdkmanager` treats the emulator as installed and stops blocking system-image installs.

> **Trust caveat:** this is an unofficial binary. Acceptable for a scratch test rig. Revisit provenance — or build from AOSP `emu-master-dev` — before using it for anything sensitive.

**Verified working:** KVM acceleration, `arm64-v8a` system image, gRPC control plane on the configured port.

### The dead ci.android.com route

**Symptom:** Following guidance to fetch an emulator build from `ci.android.com` returns `NoSuchKey` from GCS, and `dl.google.com/.../emulator-linux_aarch64-<bid>.zip` guesses return 404.

**Cause:** Branch `aosp-emu-master-dev`, target `emulator-linux_aarch64`, last built **2025-01-16** (build id 12929531). `BUILD_INFO` still lists `sdk-repo-linux_aarch64-emulator-12929531.zip`, but the artifacts were purged by retention policy.

**Fix:** None. **The route is dead — do not spend time on it.** Documented here specifically so the next person recognises the dead end in under a minute instead of an hour. Use the community build above, or build from AOSP source.

### `/dev/kvm` exists but you're not in the group

**Symptom:** `doctor`'s `kvm` check reports unavailable even though `/dev/kvm` is clearly present on the host, and adding yourself to the group and logging back in is not an option inside a running automation session.

**Cause:** `/dev/kvm` is typically `0660 root:kvm`. Group membership is fixed at login — `id -nG` shows no `kvm` for the current session even on a host where KVM works fine for other logged-in users, and `getent group kvm` can show an empty member list regardless.

**Fix:** launch under `sg`, which starts a process with an additional group without a new login session:

```bash
sg kvm -c 'test -r /dev/kvm && test -w /dev/kvm && echo KVM-OK'
```

If that prints `KVM-OK`, wrap the emulator launch itself the same way — only the emulator process needs the group; adb, uiautomator, and the tool calls stay outside the wrapper. `sg` takes a single command string, so build the whole launch (including any environment the emulator needs, set *inside* the string) in a variable and echo it once before running, rather than nesting quotes live.

**If KVM genuinely cannot be reached,** an arm64 image on an arm64 host still runs unaccelerated (`-accel off`), but boot may take many minutes or never complete within a reasonable timeout — report this honestly as a last resort. Fixing group access is the real fix.

---

## Known Gaps — Manual `adb shell` Fallback Required

These capabilities have no wrapped tool operation yet. Each is real signal that was needed in a live session; when you need it, drop to the manual invocation and treat every rule above (serial scoping, CRLF stripping, delegate-don't-drive-adb-yourself) as still binding.

| Gap | Manual fallback |
|---|---|
| Notification content and count (`dumpsys notification`), `dumpsys` generally | [FIELD-NOTES-2026-08.md §3, "Reading notifications without a screenshot"](FIELD-NOTES-2026-08.md#3-shell-and-data-extraction-gotchas) |
| Foreground/focus ground truth (`mCurrentFocus`) | [FIELD-NOTES-2026-08.md §3, "Ground truth for \"did navigation actually land\""](FIELD-NOTES-2026-08.md#3-shell-and-data-extraction-gotchas) |
| APK identity (`aapt2 dump badging`), fresh-state install (`pm clear`) | [FIELD-NOTES-2026-08.md §3, "Reading identity out of an APK, and fresh-state installs"](FIELD-NOTES-2026-08.md#3-shell-and-data-extraction-gotchas) |
| Self-hosted publish / update-rail verification | [FIELD-NOTES-2026-08.md §5, "App publishing and self-hosted update rails"](FIELD-NOTES-2026-08.md#5-app-publishing-and-self-hosted-update-rails), or load the `android-self-hosted-publishing` skill for the condensed rule set |
| On-device sub-second UI sampling | [FIELD-NOTES-2026-08.md §3, "Catching a sub-second UI transition: sample on the device"](FIELD-NOTES-2026-08.md#3-shell-and-data-extraction-gotchas) |
| Installed-vs-built hash comparison | [FIELD-NOTES-2026-08.md §5, "Prove the installed build is the build you just made"](FIELD-NOTES-2026-08.md#5-app-publishing-and-self-hosted-update-rails) |

---

## Emulator Boot Problems

Two *distinct* crashes were found on this host. They compound, which is what made the original investigation take 13 elimination-table tests before either was understood. Diagnose them separately.

### The libpcre2 boot crash

**Symptom:** 100% deterministic SIGSEGV, immediately after these lines in the emulator log:

```
INFO  | Setting display: 0 configuration to: 1080x2400, dpi: 420x420
INFO  | setDisplayActiveConfig 0
ERROR | Unable to spawn process  due to:, No such file or directory
```

followed by process death — regardless of GPU backend, audio backend, metrics settings, device profile, or AVD data freshness.

**Cause:** The **windowed** qemu binary is a Qt build that depends on `libpcre2-16.so.0`. That library is not present on this host. The process segfaults during display setup, before Android boot activity even starts. (`emulator -version` also crashes, for the same reason — which is a fast way to confirm this diagnosis.)

**Fix:** `-no-window`. This routes to `qemu-system-aarch64-headless`, a **separate binary with no Qt dependency**.

```bash
cd $ANDROID_HOME/emulator && ./emulator -avd my-harness -no-window -no-snapshot \
  -no-boot-anim -gpu swiftshader_indirect -port 5556
```

> **`-no-window` is a crash workaround, not a performance optimisation.** It is defaulted on for exactly this reason. Do not "helpfully" remove it to get a visible window — you will get a segfault, and the log will point at display configuration rather than at the missing library.

**Alternative fix** (if you need the windowed binary): deb-extract `libpcre2-16.so.0` (10.42 from ports.ubuntu.com) into `$ANDROID_HOME/emulator/lib64/qt/lib/`. Both paths then work.

**Why this hid for so long:** a second AVD on the same host had always been launched headless, so it never hit the windowed binary. The crash looked AVD-specific when it was actually launch-flag-specific.

### The ptrace_scope crash

**Symptom:** Emulator dies at startup. Reproducible, no useful diagnostic in the log. Persists after `-no-window` fixes the libpcre2 crash — this is a *separate* failure.

**Cause:** `kernel.yama.ptrace_scope=1` on the host. The emulator's in-process crash handler performs a **self-ptrace** as part of its own startup exception-handling self-test. YAMA blocks it, and the handler does not recover cleanly.

**Fix:** Keep a tracer permanently attached. Running under `gdb -batch` lets startup proceed normally — consistent with crash-handling code that detects an attached tracer and skips the self-test that would otherwise fail under restricted ptrace.

```
# gdb-launch.txt
handle all nostop noprint pass
run
```

```bash
gdb -batch -x gdb-launch.txt --args ./emulator -avd my-harness -no-window ...
```

**Why not just change the sysctl?** Flipping a system-wide security knob from inside a test harness is not appropriate, and on most hosts you will not have `sudo` anyway. The tool detects `ptrace_scope != 0` and applies the gdb wrapper automatically.

**Verify the host setting:** `sysctl kernel.yama.ptrace_scope` — `0` means no wrapper needed.

### gdb kills a healthy emulator

**Symptom:** With the gdb wrapper in place, the emulator boots partway and then dies — *more* reliably than without it.

**Cause:** `handle SIGSEGV nostop noprint pass` alone is **not enough**. QEMU uses `SIGUSR1`, `SIGUSR2`, and `SIGCONT` internally as part of normal operation. gdb's default disposition for those is still "stop and report". When gdb stops and the batch script has no further commands, `set confirm off` auto-answers gdb's "kill the program?" prompt — **silently killing a perfectly healthy booting emulator.**

**Fix:** `handle all nostop noprint pass`. The `all` is load-bearing. Silencing only SIGSEGV reproduces this bug.

### Backgrounded processes die

**Symptom:** The emulator launches, the launch command returns success, and then the emulator is gone by the next tool call.

**Cause:** A plain `cmd &` (even with `disown`) inside a normal, non-backgrounded tool call does not survive — the whole process tree is cleaned up when the tool call returns.

**Fix:** Full detachment. `setsid`, stdin from `/dev/null`, stdout and stderr to a log file:

```bash
cd $ANDROID_HOME/emulator && setsid ./emulator -avd my-harness -no-window -no-snapshot \
  -no-boot-anim -gpu swiftshader_indirect -port 5556 -grpc 8556 \
  </dev/null > /tmp/emu.log 2>&1 &
```

Alternatively, use the calling tool's own background mode (`run_in_background=true`), which keeps the process outside the tool-call lifetime.

`start_emulator` handles this. This entry exists for when you are debugging a launch by hand and wondering where your emulator went.

### Readiness is two-stage

**Symptom:** `adb` responds, but `ui_dump` returns an empty tree, `screenshot` returns a black frame, or taps do nothing.

**Cause:** `adb wait-for-device` returns as soon as the **adb transport** is up. At that point Android itself has not booted: no launcher, no window manager surface, no accessibility tree.

**Fix:** Three gates, in order, never one without the others:

```bash
adb -s "$SERIAL" wait-for-device
until [ "$(adb -s "$SERIAL" shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 2; done
adb -s "$SERIAL" shell input keyevent 82     # dismiss the keyguard
```

Skipping the keyevent leaves the lock screen swallowing every subsequent tap — which looks exactly like "the app didn't launch".

### Boot timing expectations

Measured on this host (native aarch64, KVM, 16 vCPU, 2GB guest RAM):

| Measurement | Value |
|---|---|
| Guest boot (emulator's own `Boot completed in ...`) | **25.6 s** |
| Wall clock, launch → `sys.boot_completed=1` detected | **~60 s** (5 s poll granularity) |
| `boot_timeout_s` default | 240 s |

No x86-translation penalty on an arm64-on-arm64 host — this is arguably a *better* emulator host than an x86 box running arm64 images.

**Every boot is cold.** Snapshots are explicitly deferred in this bundle (as they were in both source projects). Budget ~60 s per boot and boot once per session, not once per test.

**If boot exceeds 4 minutes:** check `/dev/kvm` is readable and writable (`[ -r /dev/kvm ] && [ -w /dev/kvm ]`), and confirm acceleration with `emulator -accel-check`. Without KVM an arm64 image on an arm64 host still runs, but slowly enough to blow any reasonable timeout.

### Audio driver warning

**Symptom:** `Could not init 'oss' audio driver` at launch.

**Cause:** Host audio backend fallback. The host has no OSS audio device.

**Fix:** **None needed — this is harmless.** It is a warning, not an error. The guest's virtual audio device works regardless, and the emulator's gRPC audio control plane (`injectAudio` / `streamAudio`) talks to the guest device, not to host audio output.

Listed here because it appears prominently in the launch log and looks alarming next to a genuine boot failure. It is not the cause of your boot failure.

### AVD does not exist

**Symptom:** `start_emulator` fails **immediately** (measured 0.02s), naming the AVD you asked for and listing the AVDs that actually exist.

**Cause:** A typo, or an AVD that was never provisioned on this host.

**Fix:** The error carries everything you need — the requested name, the existing names (so a typo is obvious on sight), and the exact `avdmanager create avd ...` command with the ABI already resolved for your host. Or provision it through the tool:

```python
android_inspector(operation="create_avd", name="my-harness")
```

**Why it is worth a section:** this check used to not exist. A typo'd AVD name burned the full **60s** device-appear timeout and then reported "no new adb device serial appeared" — a symptom four abstraction layers away from the cause. A missing AVD is detectable instantly from `<name>.ini` files under the AVD home, so the check now runs *before* any process is spawned.

### Port already in use, or invalid

**Symptom:** `start_emulator` refuses to launch (measured 0.04s), reporting that the requested port is already attached to a device.

**Cause:** Something is already answering on `emulator-<port>` — another project's emulator, or a previous run that was never shut down.

**Fix:** Choose a different even port, or stop whatever is using that one (`stop_emulator`, or `adb -s emulator-<port> emu kill`).

**Why it refuses instead of continuing:** adopting a serial that already answers means operating on an instance this call did not start. That is precisely the [wrong device targeted](#wrong-device-targeted) failure — the one where an APK was installed onto a different project's emulator and driven for several steps before anyone noticed. A refusal is a two-second inconvenience; adoption is a silently wrong test run.

**Related — invalid port:** `port` must be **even** and within **5554–5682**. The emulator uses `port` for its own console and `port + 1` for adb, and adb only scans that range looking for emulator consoles; an odd or out-of-range value leaves the adb port undiscoverable. Invalid values are rejected in 0.04s with the constraint explained — never silently adjusted, and never silently ignored (which is what happened before `port` was honoured at all).

### Emulator process died during boot

**Symptom:** `start_emulator` returns an error naming an **exit code or signal**, the **log path**, and a **diagnostic excerpt** from that log.

**Cause:** The emulator process died. The error is telling you which of the crashes above you hit — read the excerpt first:

| Excerpt shows | You are in |
|---|---|
| `Setting display: 0 configuration` then death | [The libpcre2 boot crash](#the-libpcre2-boot-crash) — the windowed binary. Ensure `-no-window` |
| Death at startup with little else | [The ptrace_scope crash](#the-ptrace_scope-crash). Check `sysctl kernel.yama.ptrace_scope`; if non-zero, gdb must be installed for the wrapper |
| Signal-terminated shortly after launch, gdb in the path | [gdb kills a healthy emulator](#gdb-kills-a-healthy-emulator) — `handle all` is load-bearing |
| KVM / acceleration complaints | `/dev/kvm` not readable+writable. `doctor` reports this directly |

**Fix:** Whatever the excerpt names. Run `doctor` if you want the host's full state rather than just this one failure.

**Why the error looks like this now:** every wait stage polls the launched process for liveness, so a death is detected the moment it happens. Previously the process died silently and the run waited out the full 240s adb timeout before reporting "adb timed out" — the *symptom*, long after the emulator's own log had already named the *cause*. The excerpt is inlined into the error specifically so diagnosing it does not need a second round trip.

---

## Device Targeting Problems

### Wrong device targeted

**Symptom:** An APK appears to install successfully but the app under test does not change. Taps land on an app you are not testing. Screenshots show a different project's UI.

**Cause:** A script picked the **first** `emulator-NNNN` entry from `adb devices` instead of the one it just booted. Two AVDs on one host is the normal case when several projects share a machine.

**This actually happened:** `attend.apk` was installed onto `vcos-rig` — a different project's emulator — and launched and tapped before the mistake was caught.

**Fix:**

1. **Snapshot `adb devices` before launching**, and treat only a genuinely *new* serial as yours.
2. **Pin the serial explicitly** in every call thereafter. `list_devices` treats multi-device ambiguity as an **error**, not a warning, for exactly this reason.
3. When in doubt: `adb devices -l` and check the model by hand before running anything that installs or types.

Never let a script guess a serial when more than one device may be attached.

---

## Interaction Problems

### The tap missed and nothing told you

**Symptom:** A tap reports success. Nothing on screen changed. No error anywhere.

**Cause:** The coordinates were wrong — most often because they were derived from a **VLM reading of a screenshot** rather than from `ui_dump`.

Measured on this host, same screenshot, same "Refresh" button:

| Source | Bounds | Center |
|---|---|---|
| `ui_dump` | `[877,142][1006,195]` | **(941, 168)** |
| VLM reading the PNG | `[810,50][950,100]` | (880, 75) |

Delta **dx −61px, dy −93px** — the VLM-derived center lands **outside the real button**.

**Fix:** Coordinates always come from `ui_dump` or `find`. Never from vision. `tap` enforces this by resolving a selector against a fresh dump before touching the screen.

**If a selector will not resolve,** work the list in Section 2 of the guide (wait, dismiss ANR, scroll, Compose quirks) and then **report it as a finding**. A zero-match is information. It is not a licence to estimate coordinates from a picture.

### Text went into the wrong field

**Symptom:** A value saves to the wrong setting. The API key is correct but the URL is not (or vice versa). No error at any point.

**Cause:** The tap that was supposed to focus field A actually landed on field B — or on nothing, leaving focus wherever it already was. The subsequent `input text` typed into whatever *did* have focus.

**This actually happened:** an attempt to retype a Base-URL field landed in the API-key field. The API key saved correctly; the URL did not. The run reported success.

**Contributing cause:** Compose `EditText` nodes frequently do **not** report `clickable="true"` in the accessibility tree, so heuristics that filter for clickable nodes miss text fields entirely.

**Fix:** The verified field-write protocol, enforced by `type_text`:

1. resolve selector from a **fresh** dump
2. tap the resolved center
3. **assert `focused="true"` on the intended node** — stop here if false
4. `KEYCODE_MOVE_END` + N × `KEYCODE_DEL` to clear (`input text` appends, it does not replace)
5. `input text`
6. `KEYCODE_BACK` to commit
7. re-dump and **assert readback**

If `verified` comes back false, **stop the run**. Every assertion after a bad config write is untrustworthy.

### Committing a field breaks the next three interactions

**Symptom:** After typing into a field, subsequent taps on the bottom nav bar do nothing.

**Cause:** The field was committed by "tapping elsewhere". That tap landed on a `TextView` label which sits directly above a *different* `EditText` — focusing that field instead of dismissing the keyboard. The IME stays up, overlapping the bottom nav, and swallows every tap aimed at it.

**Fix:** Commit with `KEYCODE_BACK` (`input keyevent 4`). In a single-Activity Compose app it dismisses the IME without navigating anywhere, and the field value is unaffected — Compose has already committed it character-by-character via `onValueChange`.

### Spontaneous ANR dialogs

**Symptom:** A run that was working suddenly stops responding to input. Several interactions in a row produce no change.

**Cause:** An "Application Not Responding" dialog. On headless emulators these appear **spontaneously**, with no provoking action — reproduced live during this bundle's design. The dialog steals focus and absorbs every tap aimed at the app beneath it.

**Fix:** `dismiss_anr` (taps "Wait"). Make it your **first hypothesis** when a run mysteriously goes unresponsive — it is cheap to rule out and it is a common cause.

### Navigation to a tab "fails" but the tab is right there

**Symptom:** `{"desc": "Settings"}` returns zero matches, but the screenshot clearly shows the Settings screen.

**Cause:** The **currently-selected** bottom-nav tab loses its `content-desc` in the accessibility tree. Every other tab keeps one. Combined with `launch` re-foregrounding a running app onto its last-shown screen, this is the *common* case, not an edge case.

**Fix:** Treat "tab not found by desc" as *possibly already there*. Confirm with a screen-specific text marker (e.g. `{"text": "Base URL"}` for Settings) before reporting a navigation failure.

### Screenshot shows plausible but fake data

**Symptom:** A screen renders convincingly. The data is stale, cached, seeded, or simply absent.

**Cause:** An empty shell, a cached render, and live data are visually indistinguishable — especially to a VLM, which will describe an empty list as "rendering correctly".

**Fix:** Never report "verified" on a screenshot alone. Require at least one independent confirmation:

- **Server-log correlation** — the specific endpoints this screen needs, arriving during the interaction window. Via `adb reverse tcp:PORT tcp:PORT` requests appear from `127.0.0.1`; via the emulator host alias they appear from that address.
- **In-app status line** — assert on it from `ui_dump`, not from the image. `Last successful poll: never` alongside plausible-looking rows means the data is not live.
- **logcat** — an app that catches network exceptions and renders an empty state gives you a clean screen and a loud log. Read the log before declaring a pass.

With the screenshot and nothing else, the honest report is **"the screen renders; I could not confirm the data is live"**.

---

## Emulator gRPC Control Plane

The emulator exposes a gRPC control plane (`-grpc <port>`), useful for capabilities `adb` does not cover — audio injection, sensor simulation, virtual scene control, snapshots.

```bash
grpcurl -plaintext localhost:8554 list
grpcurl -plaintext -d '{}' localhost:8554 \
  android.emulation.control.EmulatorController/getStatus
```

`getStatus` is a useful liveness probe — it reports `booted`, emulator version, and `vmConfig.hypervisorType` (confirm `KVM`, not a software fallback).

> **Security note:** the gRPC server binds to `[::]:<port>` — **all interfaces** — with `security: Insecure, auth: none`. For anything beyond an ad-hoc local run, launch with `-grpc-use-jwt` or restrict the port to loopback. Shut the emulator down when you are finished.

---

## Deferred by Design

Named here so they are not rediscovered as gaps:

| Deferred | Why | Consequence |
|---|---|---|
| **Snapshots** | Deferred in both source projects | Every boot is cold (~60 s). Boot once per session |
| **Android SDK installation** | Out of scope — the bundle assists with setup, it does not bootstrap a machine | `doctor` reports precisely what is missing and how to fix it |
| **Downloading the community linux-aarch64 emulator** | It is an **unsigned third-party binary**. Whether it goes on a machine is a human's trust decision, not a tool's | `doctor`'s `emulator_binary` check detects the gap and points here — see [No aarch64 emulator from Google](#no-aarch64-emulator-from-google) for the URL and sha256 |
| **Physical devices over Tailscale ADB** | Works today via the same serial contract, but the port changes on every re-pair | Device *discovery* is out of scope; pin the serial manually |
| **Containerised emulators (DTU)** | Compose `digital-twin-universe` later if needed | Emulators run on the host for now |

**No longer deferred:** AVD provisioning. The `create_avd` operation wraps `sdkmanager` + `avdmanager`, auto-detects the ABI from the host arch, refuses to clobber an existing AVD without `force`, refuses to accept SDK licences without `accept_licenses`, and verifies via `emulator -list-avds` rather than trusting an exit code.
