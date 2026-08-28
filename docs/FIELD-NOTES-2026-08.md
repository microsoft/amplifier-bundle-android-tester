# Field Notes — 2026-08

A second tranche of field knowledge, from several weeks of driving one app under test across a
**headless emulator fleet and a physical phone over wireless debugging**, on an aarch64 Linux dev
box, with multiple automation sessions sharing the same host.

Everything here was paid for. Each entry is a measured failure or a command that was actually run —
none of it is theorised. Where a note only sharpens something the bundle already documents, it says
so and links rather than restating it.

**Already covered elsewhere, deliberately not repeated here:** the selector-first contract and the
screenshot-is-never-a-coordinate measurement (`README.md`, `context/android-guide.md` §0), aarch64
adb and emulator provisioning, the `libpcre2` and `ptrace_scope` boot crashes, ANR dismissal,
serial scoping, and AVD leases / console-port rules ([`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)).

**Contents**

1. [Wireless debugging and physical devices](#1-wireless-debugging-and-physical-devices)
2. [Headless emulator operations on a shared host](#2-headless-emulator-operations-on-a-shared-host)
3. [Shell and data-extraction gotchas](#3-shell-and-data-extraction-gotchas)
4. [Rendering traps that only appear on a real device](#4-rendering-traps-that-only-appear-on-a-real-device)
5. [App publishing and self-hosted update rails](#5-app-publishing-and-self-hosted-update-rails)
6. [Verification discipline](#6-verification-discipline)

---

## 1. Wireless debugging and physical devices

The bundle currently defers physical-device *discovery* with the note that "the port changes on
every re-pair". That is true and it undersells the problem. Here is the whole dance.

### Pairing is two ports, and the code expires in about a minute

**Need:** attach a phone over wireless debugging so the same serial-scoped tooling drives it.

**The trap:** there are **two different ports**, and the one displayed most prominently is the one
you need *first and only once*.

| Port | Where it comes from | Used by | Lifetime |
|---|---|---|---|
| **Pairing port** | Shown in the phone's *Pair device with pairing code* dialog, next to a 6-digit code | `adb pair` | The dialog is open |
| **Connect port** | Shown on the *Wireless debugging* screen itself, under the device's IP | `adb connect` | Until wireless debugging is toggled or the network changes |

```bash
# 1. On the phone: Developer options -> Wireless debugging -> Pair device with pairing code
adb pair <phone-ip>:<PAIRPORT> <6-digit-code>     # both from the dialog
# 2. Then, from the Wireless debugging screen (a DIFFERENT port):
adb connect <phone-ip>:<CONNECTPORT>
adb devices                                        # serial is now <phone-ip>:<CONNECTPORT>
```

**Gotchas, each of which cost a retry:**

- **The pairing code expires in roughly a minute.** Have the `adb pair` command typed and ready
  *before* opening the dialog. One code was burned mid-setup simply by reading the screen too
  slowly; the remedy is to close the dialog and reopen it for a fresh code.
- **Both ports change** every time wireless debugging is toggled off and on, and whenever the phone
  rejoins the network. A connect port recorded in a script is stale by the next session.
- **Pairing survives; the connection does not.** After a successful pair you normally only need
  `adb connect` again — re-pairing is required only if the phone forgets the host.
- **The phone going to sleep silently drops the connection.** The serial simply disappears from
  `adb devices`, mid-run, with no error attributed to it. Treat "device vanished" as the first
  hypothesis, not a tooling bug, and re-`connect` before re-diagnosing anything.

### Tell a real device from an emulator by the shape of its serial

**Need:** never run a destructive operation against the tester's phone by accident.

The two serial families are unambiguous:

| Shape | What it is |
|---|---|
| `emulator-5554` | An emulator console port |
| `<phone-ip>:37xxx` | A physical device over wireless debugging |

Anything doing a reinstall, a `pm clear`, a reboot, or a process kill should branch on that shape
first. Pinning the serial (already bundle doctrine) prevents targeting the wrong device; checking
the *shape* prevents doing something to a real phone that was only ever safe on a throwaway
emulator. `pm clear` on the phone that a human is actually carrying is not recoverable by
re-running the test.

### Reaching a service on the dev box differs between emulator and phone

**Need:** point the app under test at a server running on the host.

| Target | Host address from inside the app |
|---|---|
| Emulator | `10.0.2.2` — the emulator's alias for the host loopback |
| Physical device over wireless | `10.0.2.2` does **not** exist. Use the host's actual routable address on whatever network the phone is on |
| Either | `adb -s "$SERIAL" reverse tcp:PORT tcp:PORT`, after which the app uses `127.0.0.1:PORT` |

`adb reverse` is the portable option and it is the one worth defaulting to: it works identically on
both device families, and it makes server-log correlation unambiguous because every request arrives
from `127.0.0.1`.

**Gotcha:** if the host service is HTTPS with a self-signed or private CA, changing the address
changes the certificate's expected hostname. A config that works on the emulator can fail TLS on the
phone for reasons that have nothing to do with the code under test.

### The device has to be awake and unlocked for UI verification

adb tolerates a locked screen for installs, `dumpsys`, and logcat. `uiautomator` does not usefully:
you get the keyguard's tree, not the app's, and every tap lands on the lock screen. For unattended
overnight device runs this has to be planned for — the phone left unlocked, screen timeout raised —
otherwise the run produces a full evidence set that describes the lock screen.

---

## 2. Headless emulator operations on a shared host

### `/dev/kvm` exists, is 0660 root:kvm, and you are not in the group

**Symptom:** `doctor` reports KVM unavailable. `/dev/kvm` is clearly present. Adding yourself to the
group and re-logging in is not an option inside an automation session.

**Cause:** group membership is fixed at login. `id -nG` shows no `kvm`, and `getent group kvm` can
show an **empty member list** even on a host where KVM works for other users.

**Technique:** launch under `sg`, which starts a process with an additional group without a new
login session:

```bash
sg kvm -c 'test -r /dev/kvm && test -w /dev/kvm && echo KVM-OK'
```

If that prints `KVM-OK`, wrap the emulator launch the same way. Everything else — adb, uiautomator,
the tool calls — stays outside the wrapper; only the emulator process needs the group.

**Gotchas:** `sg` takes a single command string, so the whole launch has to be quoted as one
argument, and any environment the emulator needs (`ANDROID_HOME`) must be set *inside* that string
rather than exported outside it. Nested quoting is where this usually goes wrong; build the string
in a variable and echo it once before running it.

**If KVM genuinely cannot be reached,** an arm64 image on an arm64 host still runs without
acceleration:

```bash
emulator -avd <name> -no-window -no-snapshot -no-boot-anim -no-audio \
         -accel off -gpu swiftshader_indirect -qemu -machine gic-version=2
```

Report this honestly as a last resort: boot may take many minutes or never complete within any
reasonable timeout. Fixing group access is the real fix.

### Never `pkill qemu` — it takes out other lanes *and* the phone

**Symptom:** a cleanup step ends several unrelated automation sessions, and the physical device
disappears from `adb devices` at the same time.

**Cause:** blanket process kills match every emulator on the host, not just yours. And killing the
adb server (or a broad enough pattern) tears down the wireless device's transport too — the phone
is not a process you own, but its connection dies with the server.

**Technique:** kill only the pid you started, matched by *your* AVD name:

```bash
pgrep -f "qemu-system-aarch64.*-avd <your-avd>"    # identify — print it before killing anything
adb -s "$SERIAL" emu avd name                       # which AVD is this serial, really?
```

`emu avd name` is the reliable direction of that lookup: given a serial, it tells you the AVD, so a
kill decision can be justified rather than pattern-matched. Prefer `stop_emulator`, which does the
`adb emu kill` and reaps only its own process.

**Gotcha:** this is exactly the hazard the AVD leases already guard for *starting* an emulator. The
symmetric rule for *stopping* one is worth stating separately, because a cleanup path written in a
hurry is where blanket kills appear.

### Launching so the emulator outlives the tool call

[`TROUBLESHOOTING.md` § Backgrounded processes die](TROUBLESHOOTING.md#backgrounded-processes-die)
covers `setsid` and the detachment requirement. Two additions from running several lanes at once:

```bash
setsid nohup sg kvm -c "ANDROID_HOME=$HOME/android-sdk \
  $HOME/android-sdk/emulator/emulator -avd <name> -no-window -no-snapshot \
  -no-boot-anim -gpu swiftshader_indirect -port 5558" \
  </dev/null > /tmp/emu-5558.log 2>&1 & disown
```

- **One AVD and one console port per session**, and name the log file after the port. When four
  emulators are up, `/tmp/emu.log` belonging to whichever lane wrote it last is worse than no log.
- `nohup` and `disown` alongside `setsid` cost nothing and cover the hang-up path that `setsid`
  alone does not.

---

## 3. Shell and data-extraction gotchas

### `adb shell` returns CRLF — strip it on every captured value

**Symptom:** a captured value looks correct when printed but fails as a command argument. A pid
becomes `--pid=12345<CR>`; a path comparison that should match returns false; a numeric comparison
errors out.

**Cause:** `adb shell` translates line endings, so every captured line carries a trailing `\r`.

**Technique:** `tr -d '\r'` on **every** `adb shell` capture, without exception:

```bash
PID=$(adb -s "$SERIAL" shell pidof com.example.app | tr -d '\r')
BOOTED=$(adb -s "$SERIAL" shell getprop sys.boot_completed | tr -d '\r')
APK=$(adb -s "$SERIAL" shell pm path com.example.app | sed 's/package://' | tr -d '\r')
```

**Gotcha:** the failure is invisible in most output. `echo "$PID"` looks fine because the carriage
return returns the cursor to a column you cannot see. Make it a reflex, not a debugging step —
this is the general form of the rule the boot-readiness gate already applies to `sys.boot_completed`.

### Deep links: `&` is eaten by the host shell, not the device

**Symptom:** `am start` with a URL containing query parameters launches the app but the parameters
after the first `&` are missing — or the host shell forks a background job and the command looks
truncated.

**Cause:** the whole `adb shell ...` argument list is parsed by *your* shell first. `&`, `;`, `?`
and `*` never reach the device.

**Technique:** quote the entire remote command as one string:

```bash
adb -s "$SERIAL" shell "am start -a android.intent.action.VIEW \
  -d 'myapp://pair?host=127.0.0.1&port=9000&token=abc' com.example.app"
```

Outer double quotes hold the remote command together; inner single quotes protect the URL.

**Gotcha:** it "works" with a single-parameter URL, so this passes every simple test and breaks on
the first realistic link.

### logcat: clear, timestamp, and correlate to the device clock

Package scoping via `--pid` is already the tool's behaviour. Three additions for reading a log
around a specific interaction:

```bash
adb -s "$SERIAL" logcat -c                        # clear BEFORE the interaction
# ... drive the UI ...
adb -s "$SERIAL" shell date +%s%3N | tr -d '\r'   # device clock, ms — for correlating to host logs
adb -s "$SERIAL" logcat -d -v time --pid="$PID"
```

**Matching your app's tags without false positives:** logcat lines carry a one-letter priority
prefix before the tag (`D/MyTag`, `E/AndroidRuntime`). Grepping for a bare tag name also matches the
tag appearing *inside* other messages. Anchor on the prefix:

```bash
grep -E ' [A-Z]/(MyAppNet|MyAppCall)' logcat.txt
```

**Gotcha:** the device clock and the host clock drift. Capture `date +%s%3N` from the device inside
the interaction window if you intend to line device events up against a server log.

### Catching a sub-second UI transition: sample on the device

**Need:** observe a screen that exists for a few hundred milliseconds — a transient connecting
state, a toast, an intermediate route.

**Cause of the miss:** every host-driven dump is a round trip. By the time the dump lands, the
transition is over, and polling faster from the host does not close the gap.

**Technique:** push a small sampler to `/data/local/tmp/` and loop it **on the device**, stamping
each dump, then pull the results afterwards:

```bash
# on-device loop, roughly:
#   for i in $(seq 1 40); do
#     date +%s%3N
#     uiautomator dump --compressed /sdcard/dump-$i.xml
#   done
adb -s "$SERIAL" push sampler.sh /data/local/tmp/
adb -s "$SERIAL" shell sh /data/local/tmp/sampler.sh
adb -s "$SERIAL" pull /sdcard/ ./dumps/
```

`--compressed` is meaningfully faster **and still keeps resource-ids**, which is the property that
makes it usable — verified, not assumed.

**Gotchas:** measure your own dump cost with three timed trials before designing the loop; it varies
by device and by tree size. And this bypasses the tool's dump serialisation, so do not run it
concurrently with tool-driven `ui_dump` calls on the same serial. It is a diagnostic instrument, not
a replacement sensor: coordinates still come from a dump taken immediately before the tap.

### Reading notifications without a screenshot

**Need:** assert on notification content — a hard thing to verify visually and an easy thing to
misread from an image.

```bash
adb -s "$SERIAL" shell "dumpsys notification --noredact" | grep -A20 "pkg=com.example.app"
adb -s "$SERIAL" shell "dumpsys notification --noredact" | grep -c "pkg=com.example.app"   # count
adb -s "$SERIAL" shell cmd statusbar expand-notifications                                  # drive the shade
adb -s "$SERIAL" shell cmd statusbar collapse
```

`--noredact` is what exposes `android.title`, `android.text`, `android.bigText` and `android.subText`
as readable strings. Without it the interesting fields are elided.

**Gotcha:** the count is the assertion that actually catches bugs. A fresh pairing flow that posts
one notification per historical item produces a backlog flood — visible as a count of 14 where the
expectation was 1, and completely invisible in a screenshot of the top of the shade.

### Ground truth for "did navigation actually land"

```bash
adb -s "$SERIAL" shell "dumpsys window | grep mCurrentFocus"     # focused window/activity
adb -s "$SERIAL" shell "dumpsys activity services com.example.app"  # foreground services running
```

`mCurrentFocus` settles the "am I even in the app" question that a plausible-looking screenshot
cannot. It is also how a *sibling automation session stealing the foreground* is caught — the focus
names another package, and every "tap did nothing" symptom in the run is explained at once.

### Reading identity out of an APK, and fresh-state installs

```bash
"$ANDROID_HOME"/build-tools/*/aapt2 dump badging app-debug.apk   # applicationId, versionCode, label
adb -s "$SERIAL" shell pm clear com.example.app                   # wipe app data — genuine fresh state
```

**Gotcha worth internalising:** `install -r -g` (the tool's default) **preserves app data**. That is
usually what you want, but it means a "reinstall" leaves the app still paired, still configured, and
still holding whatever state the last run left behind. First-launch and onboarding paths are not
exercised by a reinstall — only `pm clear`, or an uninstall, gets you there. A first-launch ANR
found in a real session had survived dozens of reinstalls precisely because it needed a cleared
data directory to reproduce.

---

## 4. Rendering traps that only appear on a real device

The visual tester's checklist already flags "content hidden behind the status bar or nav bar
insets". These are the two causes behind that symptom that took the longest to find, plus the way to
reproduce them on an emulator.

### `MaterialButton` reads `android:inset*`, not `app:inset*`

**Symptom:** a button has unexplained vertical padding that no style change removes; taps near its
visual edge land outside the node's bounds.

**Cause:** `MaterialButton` applies a default vertical inset, and it reads `insetTop` / `insetBottom`
from the **`android`** namespace. Setting `app:insetTop` silently does nothing — no warning, no lint
error, no visible effect.

**Fix:** `android:insetTop="0dp"` / `android:insetBottom="0dp"`.

**Why it matters to a tester:** the dump's `bounds` are correct throughout. The button's *touchable*
area is genuinely smaller than it looks, so this presents as "the tap coordinates were right and
nothing happened" — a failure mode worth recognising before starting a tap-targeting investigation.

### Edge-to-edge on API 35 clips content under the status bar and cutout

**Symptom:** top and bottom controls sit under the status bar, the display cutout, or the gesture
nav bar. Reported from a real phone; the default emulator looked fine.

**Cause:** API 35 enforces edge-to-edge, so a layout that does not apply `WindowInsets` padding to
its top- and bottom-anchored views draws underneath the system bars.

**Reproduce it on an emulator** rather than waiting for a report from a physical device:

```bash
adb -s "$SERIAL" shell cmd overlay enable com.android.internal.display.cutout.emulation.tall
adb -s "$SERIAL" shell cmd overlay list | grep cutout        # confirm it took
adb -s "$SERIAL" shell dumpsys window displays               # the real inset values
adb -s "$SERIAL" shell wm size ; adb -s "$SERIAL" shell wm density
```

**Gotcha:** a stock emulator profile has no cutout and generous system bars, so this class of defect
is *systematically invisible* on the default rig. If the app targets API 35 and ships to real
hardware, enable a cutout overlay for at least one screenshot sweep.

---

## 5. App publishing and self-hosted update rails

Getting a build onto a tester's phone repeatedly, without a store, turns out to have a small number
of load-bearing rules. All of these were validated by a rail that ran daily.

### `versionCode` must strictly increase, and the pipeline should refuse otherwise

**The rule:** a publish step that would serve changed bytes under an existing `versionCode` must
**refuse**, naming the collision.

This is not theoretical tidiness. The refusal fired on the rail's first real use, catching a genuine
attempt to republish changed bytes under the same version. Without it, that build reaches a device
whose updater correctly decides it is already up to date — producing the worst possible failure
shape: a tester debugging a bug that was already fixed, on a build that silently never arrived.

### Signed APK plus a signed manifest sidecar

The shape that worked:

| Artifact | Contents |
|---|---|
| `app-release.apk` | The signed release build |
| `app-release.json` (sidecar, signed) | `version`, `versionCode`, `sha256` of the APK |

The in-app updater verifies **both** the signature on the manifest **and** the `sha256` of the
downloaded APK against the manifest before installing. Two independent checks: the signature says
who published it, the hash says the bytes are intact.

Two health checks are worth building in, because both failures are silent:

- **published-build-is-signed** — a debug-signed artifact served from the release channel installs
  fine and then fails to update over the previously-installed release build.
- **served-build-is-current** — the thing the server is *actually serving* matches the latest build
  produced. A publish step that succeeded while writing to the wrong path passes every other check.

### HTTPS first, fall back to HTTP only on a TLS failure — never on a 404

**The rule:** the update client tries HTTPS first, reusing **the app's own already-pinned CA and
trust manager** rather than constructing a new permissive one. It falls back to plain HTTP only when
the failure is TLS or connect-level.

**A 404 is an answer.** Falling back to HTTP on a 404 converts "the server does not have this file"
into "silently retry the same missing file with less security". The distinction is the whole point:
a transport failure means *try another transport*; an HTTP status means *the server responded, and
this is what it said*.

**Gotcha:** reuse the existing trust manager. An updater that builds its own TLS context is the
classic place a permissive trust-all implementation gets introduced "temporarily" and stays.

### Prove the installed build is the build you just made

**Symptom:** a fix that is definitely in the code is definitely not in the app. Hours disappear into
debugging code that is not running.

**Technique:** compare hashes, not version strings:

```bash
LOCAL=$(sha256sum app-debug.apk | cut -d' ' -f1)
DEV_PATH=$(adb -s "$SERIAL" shell pm path com.example.app | sed 's/package://' | tr -d '\r')
DEV=$(adb -s "$SERIAL" shell sha256sum "$DEV_PATH" | cut -d' ' -f1 | tr -d '\r')
[ "$LOCAL" = "$DEV" ] && echo "installed build IS the local build" || echo "MISMATCH"
```

**Make this the first question of any device bug report**, before reproducing anything: what build
is on the device, and is it the one containing the fix? A version string can be stale (it is only
correct if someone remembered to bump it); a hash cannot.

---

## 6. Verification discipline

These are the habits that decide whether a run's output is evidence or decoration. They are host-side
rather than device-side, but every one of them was learned by nearly shipping a false result from a
device session.

### Establish that an artifact is *new* before reading it as evidence

**Symptom:** a run's evidence directory is full of convincing files. Several of them were not
produced by that run.

**Cause:** work done on a branch inherits every artifact committed to the branch it forked from.
An inherited transcript from an earlier emulator session reads exactly like a fresh device
transcript, because it is the same file format describing the same app.

**Technique:** two checks, both cheap:

```bash
git diff --stat $(git merge-base main HEAD)..HEAD -- EVIDENCE/   # what did THIS work actually add?
md5sum EVIDENCE/transcript.jsonl                                  # differs from the inherited copy?
```

**Gotcha:** `git diff main..HEAD` **lies** once `main` moves ahead of your branch point — it reports
other people's commits as yours and, worse, can make genuinely new work look unchanged. Always
compute the merge-base.

This was the single most common false signal across the project: three separate "we proved it"
moments, all three inherited files, all three falsified in under a minute by a hash comparison.

### A monitor's verdict is not a result

**Symptom:** a delegated fast-model monitor watching a long device run reports a confident PASS,
quoting plausible log lines. The artifacts say otherwise.

**Cause:** asked to judge, a model judges — from whatever fragment it has. Measured twice: both
verdicts were confidently wrong, and both were falsified in about 30 seconds by hashing the file the
verdict claimed to be about.

**Technique:** instruct monitors to report **raw observations only** — "line X appeared at time Y",
"the process is still running", "no new files in 10 minutes" — and put that constraint in the
instruction explicitly: *you report facts; you do not interpret, conclude, or declare anything
proven*. Only an artifact check run by the party making the claim promotes anything to proven.

The same rule applies to the agent driving the device. `verified: false` from `type_text`, an empty
`changed` set after a tap, a zero-match selector — these are results to report, not obstacles to
route around.

### Make the app say which branch it took

**Symptom:** eight successful device sessions proved nothing about the code path anyone cared about.

**Cause:** a capture-source environment flag left set from an earlier debugging session routed every
run down a synthetic path. The sessions genuinely succeeded — down the wrong branch. All the
evidence said "session completed"; none of it said *which* implementation ran.

**Technique:** when the property you need to prove is not observable from adb — which audio route
was selected, which capture source is live, which transport a request used — have the app log the
branch it took, with the discriminating parameters attached, and assert on that line:

```
audio_start source=mic sample_rate=48000 aec=true       # vs source=synthetic sample_rate=24000 aec=false
```

Then the assertion is `source=mic`, not "the session started". Without a line like this, a whole
category of question is simply unanswerable after the fact — a field session where the wrong audio
route was chosen cannot be adjudicated at all if the chosen route was never recorded.

### Evidence has to survive the run that produced it

- **`tee -a`, never `tee`.** A run appending to a log and a run truncating it look identical until
  the second lane starts and the first lane's evidence is gone.
- **Pull artifacts off the device or host before the run ends.** A crash mid-run loses everything
  uncommitted. Rescuing a transcript takes seconds; regenerating a device session takes an hour and
  a human with a phone.
- **Run the gate, then push — as two separate commands.** `./gradlew test | tail -2` in a `set -e`
  chain reports the *pipe's* exit status, which is `tail`'s, which is always 0. Failures pass
  silently and get pushed. This bit twice before the pattern was banned; a pipeline that ends in
  `| tail` or `| grep` cannot fail a build.
