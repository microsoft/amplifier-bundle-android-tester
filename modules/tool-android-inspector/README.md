## tool-android-inspector

Amplifier tool module for driving and inspecting Android apps on an
adb-reachable device or emulator. Single verb-dispatch tool, `_ok`/`_err`
envelope — see `docs/designs/android-tester-design.md` in the bundle root for
the full design rationale.

### The load-bearing constraint

**uiautomator is the sensor. The screenshot is for judgment, never for
targeting.** Every interaction resolves a selector against the live
accessibility tree before acting — coordinates never come from a human or VLM
reading a screenshot. Raw coordinates require the explicitly-named `tap_xy`
operation, and its result always carries a warning.

### Files

| File | Purpose |
|---|---|
| `amplifier_module_tool_android_inspector/__init__.py` | `AndroidInspectorTool` + `mount()` — verb dispatch |
| `amplifier_module_tool_android_inspector/adb.py` | Serial-scoped adb wrapper — every device-scoped call carries `-s <serial>` |
| `amplifier_module_tool_android_inspector/emulator.py` | Host-workaround-aware emulator lifecycle (gdb/ptrace_scope, two-stage readiness) |
| `amplifier_module_tool_android_inspector/avd.py` | AVD discovery/fast-fail, host readiness (`doctor`), provisioning (`create_avd`) |
| `amplifier_module_tool_android_inspector/ui.py` | Dump parsing, selectors, the verified interaction protocol |
| `amplifier_module_tool_android_inspector/lock.py` | Per-serial, cross-process `fcntl.flock` for serialising `uiautomator dump` |
| `amplifier_module_tool_android_inspector/leases.py` | Host-global, cross-process AVD leases + atomic port allocation (multi-session safety) |

### Operations

**Device & emulator lifecycle:** `list_devices`, `start_emulator`, `stop_emulator`, `doctor`, `create_avd`
**App lifecycle:** `install`, `launch`, `stop_app`
**Sensing:** `screenshot`, `ui_dump`, `find`, `logcat`
**Interacting:** `tap`, `tap_xy`, `type_text`, `key`, `swipe`
**Synchronising:** `wait_for`, `dismiss_anr`

Selector dict: `{"text": "Save"}` · `{"text_contains": "poll"}` ·
`{"res_id": "com.foo:id/save"}` · `{"desc": "Home"}` ·
`{"class": "EditText", "index": 0}`. Multiple keys AND together. A match of
more than one node with no `index` is an error listing candidates — never a
silent first-match.

### Hard requirements this implementation enforces

1. **Every adb invocation targeting a device carries `-s <serial>`.**
   `AdbClient` requires a serial at construction; every action method funnels
   through `build_args()` / `run()`, which always prepend
   `[adb_path, "-s", serial]`. Verified in `tests/test_adb.py`.
2. **adb binary resolution** probes, in order: `config["adb_path"]`,
   `$ANDROID_HOME/platform-tools-arm64/adb`, `$ANDROID_HOME/platform-tools/adb`,
   `adb` on PATH — verified with `adb version`. An `Exec format error` (the
   aarch64-host / x86_64-binary mismatch) fails loudly with the arm64
   remediation; there is no silent fallback to a broken binary.
3. **`launch` resolves the most deterministic mechanism available, and
   confirms arrival — it never reports success on exit code alone.**
   Resolution order: an explicit `component` goes straight to `am start -n`
   (no fallback — that's the caller's exact intent); a bare `package` first
   resolves the launcher activity via `cmd package resolve-activity --brief
   -c android.intent.category.LAUNCHER`, then `am start -n <resolved>`;
   `monkey -p <package> -c android.intent.category.LAUNCHER 1` is a last
   resort, only when resolution yields nothing usable or the resolved
   activity itself fails to start. `am start` exiting 0 while printing
   `Error:` / `Warning: Activity not started` is treated as a failure, not a
   success — exit code alone is never proof. After a mechanism reports
   success, `launch` polls `dumpsys window | grep mCurrentFocus` until it
   names the target package (bounded by `timeout_s`) before returning
   success; the result always carries `launch_mechanism`, `attempts` (every
   mechanism tried, in order, with its command and outcome), and
   `current_focus` as evidence. If every mechanism fails, or arrival is
   never confirmed, the error names every mechanism tried with its exit
   code/stderr — never a silent "probably worked".
4. **`tap` resolves a selector; it does not take coordinates.** Protocol:
   dump → resolve selector → compute center from bounds → `input tap` →
   re-dump → before/after summary. Raw coordinates are the separate `tap_xy`
   operation, whose result always carries a `warning` string.
4. **`type_text` implements the full verified field-write protocol:** tap →
   re-dump → assert `focused == True` on the target (error out if not) →
   `KEYCODE_MOVE_END` → N × `KEYCODE_DEL` (N = `max(len(existing)+10, 40)`) →
   `input text` (shell-quoted, spaces encoded as `%s`) → `KEYCODE_BACK`
   (dismiss IME — never "tap elsewhere") → re-dump → assert readback equals
   the intended text (error out if not).
5. **`wait_for(selector, timeout_s, poll_s, absent=False)`** polls `ui_dump`.
   It is the only synchronisation mechanism in the module — there is no bare
   `sleep()` standing in for it anywhere. Short settle delays after an input
   event (`SETTLE_AFTER_TAP_S`, `SETTLE_AFTER_TEXT_S` in `ui.py`) are named
   constants, not ad-hoc waits.
6. **`start_emulator`** fast-fails immediately if the requested AVD does not
   exist — **before** resolving/spawning anything else. Measured live: a
   missing AVD used to silently eat the full 60s device-appear timeout
   before reporting failure. The check consults `<name>.ini` files under the
   AVD home (`$ANDROID_AVD_HOME`, else `$ANDROID_SDK_HOME/.android/avd`,
   else `~/.android/avd`), cross-checked with `emulator -list-avds` when the
   emulator binary resolves. The error names every AVD that DOES exist (a
   typo is instantly obvious) and the exact `avdmanager create avd`
   remediation command (ABI detected from the host, never hardcoded), plus
   a pointer to `create_avd`. See `avd.py::require_avd_exists`. Once past
   that check, `start_emulator` detects `kernel.yama.ptrace_scope != 0` and
   launches under `gdb -batch` with a script containing `set pagination
   off` / `set confirm off` / `handle all nostop noprint pass` / `run -avd
   ...` ("handle all" is load-bearing — QEMU uses SIGUSR1/SIGUSR2/SIGCONT
   internally). Always passes `-no-window -no-snapshot -no-boot-anim -gpu
   swiftshader_indirect`. Launches detached (`start_new_session=True`, stdin
   from `/dev/null`, output to a log file). An explicit `port` is validated
   (even, in the 5554-5682 range adb scans — see `validate_emulator_port`)
   before anything is launched, threaded into the argv as `-port <port>`,
   and — since the resulting serial is then deterministic
   (`emulator-<port>`) — waited on exactly, refusing to launch at all if
   that serial is already attached to another device/emulator. Without a
   `port`, snapshots `adb devices` before launch and accepts only a
   genuinely new serial. Either way, every wait stage (serial appearance,
   device-ready, `sys.boot_completed`) polls the launched process for
   liveness and stops the moment it has exited — with the exit code/signal
   and a diagnostic log excerpt — rather than running out the full timeout
   on a process that can never respond (measured live: a segfaulted
   emulator used to leave the tool waiting the full 240s boot timeout
   before reporting a bare "adb timed out" symptom instead of the cause).
   Then `wait-for-device`, poll `sys.boot_completed`, `input keyevent 82`.

   **Multi-session safety (AVD leases -- see "Concurrent sessions on one
   host" below).** Before any of the above, `start_emulator` acquires an
   exclusive, cross-process lease on `avd`. An AVD's QCOW2 disk state
   cannot be opened by two `emulator` processes at once -- measured live,
   three concurrent `start_emulator` calls for the same AVD (no port), from
   three unrelated Amplifier sessions on one host, **all three failed**
   almost immediately. The lease turns that into an immediate, loud
   refusal naming the owning pid, its port, and how long it's been held,
   instead of a silent, confusing double-boot failure. A lease held by a
   process that has since died is stale and reclaimed automatically. When
   `port` is omitted, a free port is now allocated atomically (as part of
   the same lease acquisition) from the lowest free even port in
   5554-5682, skipping ports already attached in `adb devices` and ports
   recorded on any other currently-live lease -- closing the previous "two
   sessions each adopt whichever serial appears first" race. Falls back to
   waiting for "any new serial" only if every port in range is taken, and
   the result names that fallback via `port_allocation_fallback`.
7. **`list_devices`** treats ambiguity (no serial configured, >1 ready
   device) as an error listing them, demanding an explicit serial. Offline
   and unauthorized devices are reported distinctly, never silently treated
   as usable.
8. **`screenshot`** uses `exec-out screencap -p`, written to a file —
   `image_path` is always an absolute filesystem path, never inline base64.
   Byte size under 1KB is a liveness-check failure. Filenames are
   collision-proof (UTC timestamp + microseconds + random suffix, verified
   against disk) — never derived solely from an in-process counter, which
   resets every new process and would otherwise let two separate
   invocations silently overwrite each other's evidence.
9. **`ui_dump`** returns a parsed node list (class, text, content_desc,
   resource_id, bounds, center, focused, clickable, enabled), never raw XML.
   `all_nodes: true` includes every node; default filters to nodes with
   text/desc/resource-id.
10. **ANR auto-dismiss is never silent.** `ui_dump` and `tap` detect and
    surface an ANR ("isn't responding") dialog in their result; only the
    explicit `dismiss_anr` operation taps "Wait", and it reports what it did.
11. **Fail loud, no fallbacks.** Errors return a structured `{"success":
    false, "error": "..."}` envelope with concrete remediation — never a
    synthetic/degraded result or a silently-masked retry.
12. **`doctor`** never raises and never stops at the first failure — every
    one of its 10 checks (host arch/OS, `ANDROID_HOME`, adb binary, adb
    server reachability, emulator binary, KVM, `ptrace_scope`, gdb presence,
    AVDs available, `sdkmanager`/`avdmanager` presence) runs regardless of
    whether an earlier check failed, so the report always shows everything
    wrong at once. Each check returns `{name, status: ok|warn|fail, detail,
    remediation}`; the envelope adds a top-level `ready` (true iff no
    `fail`) and a `summary` naming what to fix first. `success` is always
    `true` for this operation — a machine with problems is a successful
    diagnosis, not a tool error. This makes the prerequisite checklist
    (otherwise only prose in `agents/android-operator.md`) structural
    rather than a reminder that decays. See `avd.py::run_doctor`.
13. **`create_avd`** provisions a new AVD, failing loud throughout: it never
    silently clobbers an existing AVD (requires `force: true` to overwrite),
    never silently accepts an SDK license (requires `accept_licenses: true`
    to auto-accept when a system image isn't installed), never silently
    substitutes a different ABI/API level than requested (ABI defaults to
    the host's native arch — `arm64-v8a` on aarch64, `x86_64` otherwise —
    detected, not hardcoded, but is always overridable), and never trusts
    `avdmanager`'s exit code alone — it verifies the AVD actually appears in
    `emulator -list-avds` afterward (falling back to an `.ini`-file disk
    check if the emulator binary itself doesn't resolve). See
    `avd.py::create_avd`.
14. **`stop_emulator` refuses unless a live AVD lease for the target serial
    is owned by the calling session.** Ownership is resolved by serial via
    `leases.find_lease_by_serial` -- not via any in-memory registry, since
    the whole point is protecting against a DIFFERENT process (which never
    shares this one's memory) calling `stop_emulator` on a serial it never
    started. If a different, currently-live process holds the lease, or no
    lease record exists at all (predates lease tracking, started outside
    this tool, or a physical device), `stop_emulator` refuses and names the
    owning pid (when known) -- `force: true` overrides, but the result
    always carries a prominent `warning` field naming whose emulator was
    just killed. See `emulator.py::stop_emulator` and "Concurrent sessions
    on one host" below.
15. **`mount()` config is per-instance, not a module-level global.** A
    second `mount()` call in the same process (e.g. two Amplifier sessions
    sharing one interpreter) used to silently reuse the FIRST mount's
    config and state -- measured live: three `mount()` calls with
    `work_dir` `/tmp/at-c1`, `/tmp/at-c2`, `/tmp/at-c3` all wrote to
    `/tmp/at-c1`; `/tmp/at-c2` and `/tmp/at-c3` were never created. Each
    `AndroidInspectorTool` instance now owns its own config and lazily-built
    state (`AndroidInspectorTool.__init__(config=...)` /
    `AndroidInspectorTool._get_state()`).

### Concurrent sessions on one host

Three unrelated projects, three Amplifier sessions, three separate OS
processes, **none aware of the others**, all on one machine, all wanting an
Android emulator at the same time -- without care, this corrupts silently
or fails mysteriously (see `leases.py`'s module docstring for the full
rationale). Three protections apply, in order of where they trigger:

1. **AVD lease (`start_emulator`).** Booting an AVD acquires an exclusive,
   cross-process lease on that AVD name, held under
   `~/.amplifier/android-sessions/leases/<avd>.lease` -- a **fixed,
   host-global path**, deliberately NOT derived from any session's
   `work_dir` (three sessions with three different `work_dir`s must all
   see the same lease directory, or cross-session detection never
   triggers). If another live process already holds it, `start_emulator`
   fails immediately, naming the owning pid, its port, and how long it's
   been held. A lease recorded by a since-dead pid is stale and reclaimed
   automatically. **The honest fix for two unrelated projects is that they
   should each have their own AVD** -- the lease does not make sharing
   safe, it makes the collision *discoverable and loud* instead of
   silently corrupting both instances' disk state; refusal messages point
   at `create_avd` to provision a distinct one.
2. **Atomic port allocation (`start_emulator`, no explicit `port`).** Two
   sessions starting at the same instant, with no port pinned, used to each
   adopt whichever adb serial appeared first -- a coin flip over whose
   emulator was whose. A free port is now chosen and claimed atomically
   (as part of the SAME lease acquisition, under a dedicated host-global
   allocation lock), skipping ports already attached in `adb devices` and
   ports recorded on any other currently-live lease. Only if every port in
   the 5554-5682 range is taken does this fall back to the old
   non-deterministic "any new serial" wait -- and the result says so via
   `port_allocation_fallback`.
3. **Ownership-gated `stop_emulator`.** Without a lease, `stop_emulator`
   would kill whatever serial it's handed with zero ownership check --
   session A could terminate session B's emulator (this happened by hand
   during this project's own development and caused a real cross-project
   incident: a sibling harness then adopted the wrong device and drove the
   wrong app). `stop_emulator` now refuses unless the target serial's live
   lease names the calling process as owner, naming the actual owning pid
   otherwise. `force: true` is the explicit escape hatch -- it works, but
   the result always carries a prominent `warning` naming whose emulator
   was killed. Never silent either way.

### Configuration (via `mount()` config, i.e. `behaviors/android-tester.yaml`)

| Key | Default | Purpose |
|---|---|---|
| `work_dir` | `~/.amplifier/android-sessions` | Screenshots, dumps, emulator logs |
| `adb_path` | — | Explicit adb binary override |
| `android_home` | `$ANDROID_HOME`/`$ANDROID_SDK_ROOT` | SDK root for adb/emulator discovery |
| `adb_timeout_s` | `30.0` | Per-command adb timeout |
| `default_serial` | — | Used when no explicit serial and >1 device is ready |
| `emulator_path` | `$ANDROID_HOME/emulator/emulator` | Explicit emulator binary override |
| `emulator_args` | `-no-window -no-snapshot -no-boot-anim -gpu swiftshader_indirect` | Override emulator flags |
| `gdb_path` | `gdb` on PATH | Explicit gdb binary override |
| `device_appear_timeout_s` | `60.0` | Wait for a new serial after launch |
| `boot_timeout_s` | `240.0` | Wait for `sys.boot_completed` |
| `avd_home` | `$ANDROID_AVD_HOME`, else `$ANDROID_SDK_HOME/.android/avd`, else `~/.android/avd` | Explicit AVD home override (`start_emulator`, `doctor`, `create_avd`) |
| `lease_dir` | `~/.amplifier/android-sessions/leases` (host-global -- NOT derived from `work_dir`) | Explicit AVD-lease directory override. Mainly for test isolation; production callers should not need this -- see "Concurrent sessions on one host" above |

### Testing

All tests are pure unit tests — no emulator or device dependency. Real
device/adb interaction is exercised via dependency-injected runners
(`AdbClient(runner=...)`), a duck-typed `FakeAdbClient` double
(`tests/conftest.py`), and monkeypatched `subprocess.run`/`subprocess.Popen`.

```bash
cd modules/tool-android-inspector
python -m pytest tests/ -v
```

- `tests/test_adb.py` — binary resolution, `-s <serial>` presence on every
  action method, device listing/state parsing, serial-resolution ambiguity.
- `tests/test_ui.py` — bounds/center parsing, dump parsing, selector matching
  (including ambiguity and index disambiguation), ANR detection, and the
  verified `tap`/`type_text`/`wait_for`/`dismiss_anr` protocols against a fake
  client.
- `tests/test_emulator.py` — ptrace_scope detection, gdb launch script
  content, process-launch argv construction (direct vs. gdb-wrapped), new
  serial discovery, two-stage boot readiness, AVD-lease acquisition/refusal/
  release around `start_emulator`, port auto-allocation, and
  `stop_emulator` ownership refusal/`force` override.
- `tests/test_leases.py` — the AVD-lease/port-allocation module itself:
  acquire/refuse/stale-reclaim (including a dead-owner-pid case via a real
  exited subprocess), concurrent-racer serialisation, port allocation
  skipping in-use/leased ports and reporting exhaustion, release ownership
  checks, serial reverse-lookup.
- `tests/test_tool.py` — dispatch surface sanity (schema, missing/unknown
  operation handling), per-instance `mount()`/config isolation.

### Explicitly deferred

Snapshots, AVD provisioning, DTU integration, `reality-check` routing, and
physical-device-over-Tailscale discovery — see the design doc's "Explicitly
deferred" section. Not gaps; intentionally out of scope for this module.
