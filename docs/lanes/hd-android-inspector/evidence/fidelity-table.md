## Sizes

| Surface | Stock (fbf8ce6) | Lean | Delta | % |
|---|---:|---:|---:|---:|
| tool description | 3,619 | 3,044 | -575 | -15.9% |
| 30 parameter descriptions | 3,398 | 3,293 | -105 | -3.1% |
| tool prose total | 7,017 | 6,337 | -680 | -9.7% |
| serialized tool definition | 9,097 | 8,417 | -680 | -7.5% |
| context/android-awareness.md | 3,413 | 2,787 | -626 | -18.3% |
| always-on total (tool + awareness) | 12,510 | 11,204 | -1,306 | -10.4% |

### Per-parameter description sizes

| Parameter | Stock | Lean | Delta |
|---|---:|---:|---:|
| `operation` | 20 | 20 | +0 |
| `serial` | 100 | 100 | +0 |
| `avd` ** | 204 | 245 | +41 |
| `port` ** | 799 | 710 | -89 |
| `name` | 36 | 36 | +0 |
| `api_level` | 51 | 51 | +0 |
| `tag` | 74 | 74 | +0 |
| `abi` ** | 206 | 199 | -7 |
| `device` | 54 | 54 | +0 |
| `accept_licenses` ** | 176 | 168 | -8 |
| `force` ** | 449 | 418 | -31 |
| `apk_path` | 24 | 24 | +0 |
| `package` ** | 441 | 416 | -25 |
| `component` ** | 156 | 170 | +14 |
| `selector` | 87 | 87 | +0 |
| `text` | 24 | 24 | +0 |
| `x` | 25 | 25 | +0 |
| `y` | 25 | 25 | +0 |
| `x1` | 13 | 13 | +0 |
| `y1` | 13 | 13 | +0 |
| `x2` | 11 | 11 | +0 |
| `y2` | 11 | 11 | +0 |
| `duration_ms` | 30 | 30 | +0 |
| `keycode` | 52 | 52 | +0 |
| `timeout_s` | 80 | 80 | +0 |
| `poll_s` | 35 | 35 | +0 |
| `absent` | 59 | 59 | +0 |
| `all_nodes` | 64 | 64 | +0 |
| `lines` | 40 | 40 | +0 |
| `filter_spec` | 39 | 39 | +0 |

`**` = re-worded. Every other parameter description is byte-for-byte stock.

## Fidelity — Tool surface

| ID | Stock contract | Survives | Probe (asserted in CI) |
|---|---|:--:|---|
| T-01 | drives and inspects Android apps on an adb-reachable device or emulator | ✅ | <code>Drive and inspect Android apps</code> · <code>adb-reachable device or emulator</code> |
| T-02 | uiautomator is the sensor | ✅ | <code>uiautomator is the sensor</code> |
| T-03 | every interaction resolves a selector against the live accessibility tree first | ✅ | <code>resolves a selector against the live accessibility tree before acting</code> |
| T-04 | screenshots are for judgment, never for computing tap coordinates | ✅ | <code>visual judgment only, never for computing tap coordinates</code> |
| T-05 | list_devices enumerates attached devices | ✅ | <code>list_devices: enumerate attached devices</code> |
| T-06 | list_devices errors on ambiguity | ✅ | <code>>1 ready device, no explicit serial</code> |
| T-07 | serial auto-resolves to the single ready device, else errors | ✅ | <code>uses the single ready device, or errors if zero/ambiguous</code> |
| T-08 | start_emulator boots an AVD from avd/port | ✅ | <code>start_emulator: boot an AVD (avd, port)</code> |
| T-09 | start_emulator applies host workarounds | ✅ | <code>host workarounds</code> |
| T-10 | start_emulator returns the serial | ✅ | <code>returns serial</code> |
| T-11 | a missing AVD fast-fails immediately, with no 60s timeout | ✅ | <code>fast-fails immediately (no 60s timeout)</code> |
| T-12 | the fast-fail names the existing AVDs and the avdmanager remediation command | ✅ | <code>naming the AVDs</code> · <code>avdmanager remediation command</code> |
| T-13 | 'avd' is not 'name' (create_avd's new-AVD name) | ✅ | <code>Not 'name' (create_avd's new-AVD name)</code> |
| T-14 | doctor/create_avd are how you provision an AVD | ✅ | <code>See 'doctor' and 'create_avd'</code> |
| T-15 | start_emulator takes an exclusive cross-process lease on the AVD first | ✅ | <code>exclusive cross-process lease on the AVD first</code> |
| T-16 | it refuses if another live process already has that AVD booted | ✅ | <code>another live process has it booted</code> |
| T-17 | the refusal names the owning pid/port/duration | ✅ | <code>owning pid/port/duration</code> |
| T-18 | each unrelated session should use its own AVD | ✅ | <code>its own AVD</code> · <code>see create_avd</code> |
| T-19 | port must be even and inside the range adb scans | ✅ | <code>Must be even</code> · <code>5554-5682</code> |
| T-20 | console uses 'port', adb uses 'port + 1' | ✅ | <code>'port + 1' for adb</code> |
| T-21 | invalid ports are rejected before launch, never silently ignored or adjusted | ✅ | <code>rejected before launch, never silently ignored or adjusted</code> |
| T-22 | an explicit port waits on the deterministic serial, not 'any new serial' | ✅ | <code>deterministic serial</code> · <code>'emulator-&lt;port>'</code> · <code>'any new serial'</code> |
| T-23 | it refuses if that serial is already attached to another device/emulator | ✅ | <code>already attached to another device/emulator</code> |
| T-24 | an omitted port is allocated atomically, lowest free even port in range | ✅ | <code>allocated atomically</code> · <code>lowest free even port in range</code> |
| T-25 | allocation skips ports attached in adb devices or held on another live AVD lease | ✅ | <code>already attached in adb devices or recorded on another live AVD lease</code> |
| T-26 | it falls back to 'any new serial' only when every port is taken | ✅ | <code>only if every port is taken</code> |
| T-27 | the fallback is named in the result as port_allocation_fallback | ✅ | <code>port_allocation_fallback</code> |
| T-28 | stop_emulator kills the emulator and reaps its process | ✅ | <code>kill the emulator and reap its process</code> |
| T-29 | it refuses unless the calling session owns a live AVD lease for that serial | ✅ | <code>Refuses unless a live AVD lease for that serial is owned by the calling session</code> |
| T-30 | it names the owning pid rather than silently killing someone else's emulator | ✅ | <code>names the owning pid rather than silently killing someone else's emulator</code> |
| T-31 | 'force' stops a serial whose lease is held by a DIFFERENT live process | ✅ | <code>stop_emulator: stop a serial whose AVD lease is held by a DIFFERENT live process</code> |
| T-32 | 'force' also covers the no-lease-record-at-all case | ✅ | <code>no lease record at all</code> |
| T-33 | a cross-owner kill always carries a warning naming whose emulator died | ✅ | <code>'warning' field naming whose emulator was killed</code> · <code>never</code> |
| T-34 | doctor reports the full host readiness check list | ✅ | <code>ANDROID_HOME, adb, emulator binary, KVM, ptrace_scope/gdb, AVDs, cmdline-tools</code> |
| T-35 | every doctor check runs even if an earlier one fails | ✅ | <code>every check runs even if an earlier one fails</code> |
| T-36 | doctor never errors and always returns a report | ✅ | <code>never errors, always returns a report</code> |
| T-37 | create_avd provisions from an installed or sdkmanager-installable system image | ✅ | <code>installed or sdkmanager-installable system image</code> |
| T-38 | create_avd never clobbers an existing AVD | ✅ | <code>never clobbers an existing AVD</code> · <code>silently clobbering</code> |
| T-39 | create_avd never silently accepts SDK licenses | ✅ | <code>silently accepts SDK licenses</code> · <code>silently accepting licenses on your behalf</code> |
| T-40 | accept_licenses defaults to False and fails loud | ✅ | <code>False (default) fails loud</code> |
| T-41 | create_avd takes a new AVD name | ✅ | <code>New AVD name to create (create_avd)</code> |
| T-42 | create_avd api_level names the system image API level | ✅ | <code>Android API level for the system image (create_avd)</code> |
| T-43 | create_avd tag selects the system image tag | ✅ | <code>'google_apis', 'google_apis_playstore'</code> |
| T-44 | create_avd abi defaults to the host's native ABI, detected not hardcoded | ✅ | <code>arm64-v8a on aarch64</code> · <code>detected, never hardcoded</code> |
| T-45 | create_avd device is an avdmanager device profile | ✅ | <code>avdmanager device profile, e.g. 'pixel_6'</code> |
| T-46 | install reinstalls and grants all runtime perms | ✅ | <code>adb install -r -g (reinstall, grant all runtime perms)</code> |
| T-47 | install takes an apk path | ✅ | <code>Path to a .apk (install)</code> |
| T-48 | launch by component uses am start -n directly and is the most deterministic path | ✅ | <code>'am start -n'</code> · <code>most deterministic</code> |
| T-49 | launch by package resolves the launcher activity via cmd package resolve-activity | ✅ | <code>'cmd package resolve-activity'</code> |
| T-50 | monkey -c LAUNCHER is only the last resort when resolution yields nothing | ✅ | <code>'monkey -c LAUNCHER' only if that yields nothing</code> |
| T-51 | component is preferred over package when known | ✅ | <code>Preferred over 'package' when known</code> |
| T-52 | launch confirms arrival by polling mCurrentFocus for the target package | ✅ | <code>polling mCurrentFocus for the target package</code> |
| T-53 | launch never reports success on exit code alone | ✅ | <code>never reports success on exit code alone</code> |
| T-54 | timeout_s also bounds the launch arrival-confirmation poll | ✅ | <code>launch arrival-confirmation poll</code> |
| T-55 | stop_app force-stops | ✅ | <code>stop_app: am force-stop</code> |
| T-56 | screenshot writes a PNG to disk and returns image_path, never inline base64 | ✅ | <code>writes a PNG to disk, returns image_path (never inline base64)</code> |
| T-57 | screenshot also returns geometry and byte size | ✅ | <code>geometry, byte size</code> |
| T-58 | ui_dump returns a parsed node list with every documented field | ✅ | <code>class, text, content_desc, resource_id, bounds, center, focused, clickable, enabled</code> |
| T-59 | ui_dump is not raw XML | ✅ | <code>not raw XML</code> |
| T-60 | all_nodes widens ui_dump beyond nodes with text/desc/res-id | ✅ | <code>include all nodes, not just those with text/desc/res-id</code> |
| T-61 | find returns matching nodes with resolved centers | ✅ | <code>nodes matching a selector, with resolved centers</code> |
| T-62 | find does not error on 0 or >1 matches | ✅ | <code>does not error on 0 or >1 matches</code> |
| T-63 | logcat tails, filtered by tag and/or package | ✅ | <code>logcat: tail, filtered by tag</code> · <code>running package</code> |
| T-64 | filter_spec is a logcat filter spec | ✅ | <code>logcat: filter spec, e.g. 'MyTag:D *:S'</code> |
| T-65 | lines bounds the trailing logcat dump | ✅ | <code>number of trailing lines to dump</code> |
| T-66 | logcat resolves a package to its running pid(s) via pidof, else ps -A | ✅ | <code>running pid(s) (pidof, else</code> · <code>'ps -A'</code> |
| T-67 | logcat scopes output to those pids with --pid | ✅ | <code>scopes output via --pid</code> |
| T-68 | package composes with filter_spec rather than overriding it | ✅ | <code>composing with 'filter_spec' rather than overriding it</code> |
| T-69 | logcat errors by name -- never an empty result -- if the package is not running | ✅ | <code>errors by name</code> · <code>never an empty result</code> · <code>no running process</code> |
| T-70 | tap follows dump -> resolve -> tap centre -> re-dump -> report | ✅ | <code>dump -> resolve selector -> tap center -> re-dump -> report what changed</code> |
| T-71 | type_text follows the verified field-write protocol | ✅ | <code>tap -> assert focus -> MOVE_END + N*DEL -> input text -> BACK -> re-dump -> assert readback</code> |
| T-72 | text is what type_text types | ✅ | <code>Text to type (type_text)</code> |
| T-73 | key takes a keyevent name or a numeric code | ✅ | <code>by name (e.g. 'BACK') or numeric code</code> |
| T-74 | keycode is the key operation's parameter | ✅ | <code>(key op)</code> |
| T-75 | swipe takes explicit coordinates because gestures have no selector analogue | ✅ | <code>gestures have no selector analogue</code> |
| T-76 | swipe start/end coordinates are named parameters | ✅ | <code>Swipe start X</code> · <code>Swipe start Y</code> · <code>Swipe end X</code> · <code>Swipe end Y</code> |
| T-77 | swipe duration is in milliseconds | ✅ | <code>Swipe duration in milliseconds</code> |
| T-78 | tap_xy is raw, unresolved, and always warns | ✅ | <code>RAW coordinates, no selector resolution</code> · <code>always returns a warning</code> |
| T-79 | tap_xy's raw coordinates are named parameters | ✅ | <code>Raw X coordinate (tap_xy)</code> · <code>Raw Y coordinate (tap_xy)</code> |
| T-80 | wait_for polls ui_dump until the selector appears/disappears, or times out | ✅ | <code>polls ui_dump until a selector appears/disappears, or timeout</code> |
| T-81 | wait_for exists so there are no bare sleeps | ✅ | <code>no bare sleeps</code> |
| T-82 | absent inverts wait_for to wait for disappearance | ✅ | <code>wait_for succeeds when the selector disappears, not appears</code> |
| T-83 | poll_s is wait_for's poll interval | ✅ | <code>Poll interval in seconds (wait_for)</code> |
| T-84 | dismiss_anr detects/dismisses the ANR dialog | ✅ | <code>dismiss_anr: detect/dismiss an ANR</code> · <code>isn't responding</code> |
| T-85 | an ANR is never auto-dismissed silently by other operations | ✅ | <code>never auto-dismissed silently by other operations</code> |
| T-86 | selector supports text, res_id, desc, class+index and text_contains | ✅ | <code>{"text": "Save"}</code> · <code>{"res_id": "com.foo:id/save"}</code> · <code>{"desc": "Home"}</code> · <code>{"class": "EditText", "index": 0}</code> · <code>{"text_contains": "poll"}</code> |
| T-87 | multiple selector keys AND together | ✅ | <code>Multiple keys AND together</code> |
| T-88 | an ambiguous match is an error that lists the candidates | ✅ | <code>An ambiguous match (>1 node, no index) is an error listing candidates</code> |
| T-89 | the selector parameter enumerates its keys | ✅ | <code>text, text_contains, res_id, desc, class</code> |
| T-90 | operation is the dispatch parameter | ✅ | <code>Operation to perform</code> |

**90 semantics · expected absent 0 · observed absent 0.**

## Fidelity — Awareness surface

| ID | Stock contract | Survives | Probe (asserted in CI) |
|---|---|:--:|---|
| A-01 | the bundle drives Android apps on emulators AND physical devices | ✅ | <code>emulators and physical devices</code> |
| A-02 | it does so via the android_inspector tool | ✅ | <code>&#96;android_inspector&#96;</code> |
| A-03 | interaction is accessibility-tree driven | ✅ | <code>accessibility-tree-driven interaction</code> |
| A-04 | verification is screenshot-based | ✅ | <code>screenshot-based visual verification</code> |
| A-05 | the one rule: uiautomator is the sensor, the screenshot is for judgment not targeting | ✅ | <code>uiautomator is the sensor</code> · <code>for judgment, never for targeting</code> |
| A-06 | coordinates come from ui_dump | ✅ | <code>Coordinates come from &#96;ui_dump&#96;</code> |
| A-07 | what a screenshot legitimately answers | ✅ | <code>does this look right / what state am I in / is anything clipped</code> |
| A-08 | a screenshot never answers 'where do I tap' | ✅ | <code>"where do I tap"</code> |
| A-09 | the measured VLM miss: 61px horizontally, 93px vertically, on this host | ✅ | <code>61px horizontally</code> · <code>93px vertically</code> · <code>on this host</code> |
| A-10 | the derived tap centre fell outside the button | ✅ | <code>derived tap center fell outside the button</code> |
| A-11 | the miss is silent | ✅ | <code>The miss is silent</code> |
| A-12 | a tap on nothing or on a neighbouring field raises no error | ✅ | <code>lands on nothing, or on a neighbouring field, raises no error</code> |
| A-13 | a recorded run typed a server URL into the API-key field and reported success | ✅ | <code>typed a server URL into the API-key field and reported success</code> |
| A-14 | android-operator drives and verifies end-to-end | ✅ | <code>android-tester:android-operator</code> · <code>boot emulator, install APK, launch app</code> · <code>verify behaviour end-to-end</code> |
| A-15 | android-visual-tester judges how a screen looks | ✅ | <code>android-tester:android-visual-tester</code> · <code>clipping/blank/overlap detection</code> · <code>before/after visual comparison</code> |
| A-16 | android-debugger root-causes anomalies | ✅ | <code>android-tester:android-debugger</code> · <code>why a tap did nothing</code> · <code>frame/logcat correlation</code> |
| A-17 | do not run adb/uiautomator/emulator from the root session -- delegate | ✅ | <code>Delegate</code> · <code>do not run &#96;adb&#96;, &#96;uiautomator&#96; or &#96;emulator&#96; from the root session</code> |
| A-18 | the agents hold the safety raw adb does not enforce | ✅ | <code>procedural safety raw adb does not enforce</code> |
| A-19 | serial scoping on every call | ✅ | <code>serial scoping on every call</code> |
| A-20 | dump-before-tap | ✅ | <code>dump-before-tap</code> |
| A-21 | focus assertion before typing | ✅ | <code>focus assertion before typing</code> |
| A-22 | KEYCODE_BACK commits fields | ✅ | <code>&#96;KEYCODE_BACK&#96; to commit fields</code> |
| A-23 | aarch64 host workarounds | ✅ | <code>aarch64 host workarounds</code> |
| A-24 | hand-driven adb is how two source projects installed onto the wrong emulator and typed into the wrong field | ✅ | <code>the two source projects silently installed an APK onto the wrong emulator</code> · <code>typed a URL into the wrong field</code> |
| A-25 | destructive operations branch on serial shape | ✅ | <code>reinstall, &#96;pm clear&#96;, reboot</code> · <code>&#96;emulator-*&#96; vs &#96;&lt;ip>:&lt;port>&#96;</code> |
| A-26 | because a physical device is not recoverable by re-running the test | ✅ | <code>not recoverable by re-running the test</code> |
| A-27 | scope: on-device and emulator Android UI only | ✅ | <code>on-device and emulator Android UI only</code> |
| A-28 | web UI/browsers/SPAs route to browser-tester | ✅ | <code>browsers, SPAs</code> · <code>&#96;browser-tester&#96;</code> |
| A-29 | TUI and CLI route to terminal-tester | ✅ | <code>TUI and CLI</code> · <code>&#96;terminal-tester&#96;</code> |
| A-30 | the Amplifier ecosystem itself routes to amplifier-tester | ✅ | <code>bundles, agents, sessions</code> · <code>&#96;amplifier-tester&#96;</code> |
| A-31 | prerequisite: Android SDK at ANDROID_HOME, default ~/android-sdk, with emulator and adb | ✅ | <code>&#96;ANDROID_HOME&#96;</code> · <code>&#96;~/android-sdk&#96;</code> · <code>&#96;emulator&#96;</code> · <code>working &#96;adb&#96;</code> |
| A-32 | prerequisite: /dev/kvm readable and writable | ✅ | <code>&#96;/dev/kvm&#96; readable and writable</code> |
| A-33 | on aarch64 Linux Google's platform-tools adb is x86_64-only | ✅ | <code>aarch64 Linux</code> · <code>&#96;platform-tools/adb&#96; is x86_64-only</code> |
| A-34 | Google ships no linux-aarch64 emulator build | ✅ | <code>linux-aarch64 emulator build (Google ships none)</code> |
| A-35 | setup problems are a supported path, not a dead end | ✅ | <code>not a dead end</code> |
| A-36 | doctor reports every host problem at once, each with its fix | ✅ | <code>every host problem at once, each with its fix</code> |
| A-37 | create_avd provisions an AVD | ✅ | <code>&#96;create_avd&#96; provisions an AVD</code> |
| A-38 | the bundle does not install the SDK | ✅ | <code>does not install the SDK</code> |
| A-39 | missing prerequisites: agents report the exact fix and stop, never improvise | ✅ | <code>report the exact fix and stop</code> · <code>improvise</code> |

**39 semantics · expected absent 0 · observed absent 0.**

## Token census (extractor derived from the STOCK text)

| Surface | Named tokens in stock | Absent from lean |
|---|---:|---:|
| tool surface | 64 | 0  |
| awareness | 33 | 0  |
