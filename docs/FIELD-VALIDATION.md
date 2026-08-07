# Field Validation — first run against a real app

**2026-08-06.** The bundle was pointed at `com.attend.app`, a Compose app with a documented history
of shipping UI fixes that passed unit tests and server-side `curl` and were still visibly broken
when a human opened it. That history is the reason this bundle exists.

The agent was given the app, the serial, and the mission — **no hints about any known defect.**

Scale: 184 tool calls · 37 taps · 45 screenshots · one `android-operator` run, one `android-debugger`
delegation.

## What it found

| Finding | Verdict | Independently re-confirmed |
|---|---|---|
| **App-wide BACK has no back stack.** Files → open `ATTENTION.md` → BACK exits to the launcher instead of returning to the file list. Same for Alerts → BACK. Only the Autos modal sheet dismisses correctly — and that is Compose's free `ModalBottomSheet` handling, not app code. | BROKEN | Yes — see below |
| **Items sub-tab filters are invisible to accessibility.** Open / Proposed / Handled all report `clickable=false`, `checkable=false`, `checked=false` in every state. Nothing in the tree marks which filter is active; a screen-reader user cannot tell. | BROKEN | Via `ui_dump` across all three states |
| Home, Alerts, Items, Autos, Files, Runs, Settings all render real, live, cross-consistent data | OK | Values advanced between visits (`43m → 47m → 50m ago`; notifications `269 → 270`) — not a static shell |
| Chat text entry end-to-end | UNVERIFIED | Reported as unverified rather than assumed — see Known issues |

**It retracted its own false positive.** The first sweep called the Items filters broken. Under
controlled pacing they were 3/3 correct (48 open / 0 proposed / 50 handled, distinct content each
time). The agent re-tested, retracted, and said so.

**It found the blocker before the bugs.** Partway through, it noticed `com.voicecos.android` being
launched on the same serial by another automation session and pushing the app under test to the
background mid-run — quoting the `ActivityTaskManager: START` and `WindowManager: ... m=TO_BACK`
lines from logcat. It correctly attributed every "tap did nothing" symptom to that rather than to
the app, and refused to trust the affected results.

That contamination was self-inflicted: an earlier step in the same session had stopped the
`vcos-rig` emulator, and the sibling project's harness — which picks the *first* serial from
`adb devices` — then grabbed this one. It is the exact hazard `start_emulator`'s "refuse to adopt a
device I did not start" guard exists to prevent, observed in the wild.

## Re-confirmation of the headline defect

The BACK finding was re-run independently on an uncontaminated device
(`pm disable-user com.voicecos.android` for the duration of a single test):

```
tap Files            -> ok
file list visible    -> ATTENTION.md, EMAIL.md, IDENTITY.md, MEETINGS.md
tap ATTENTION.md     -> ok, detail view (15 nodes, list gone)
BACK                 -> mCurrentFocus = NexusLauncherActivity
back on file list?   -> False
```

Confirmed: BACK from a drilled-in detail view leaves the app entirely.

## Relationship to the four historical bugs

None of the four originally-cited defects (chat `session_id` persistence, Items showing a closure
archive, a JSON parse failure, a non-updating Acknowledge button) reproduced. They appear fixed —
Settings' "Test connection" in particular fired and *did* visibly change state, which is the exact
shape of historical bug #4.

The bundle instead surfaced **two previously unreported defects** in the same render/interaction
layer. That is the more useful result: the value is not replaying known bugs, it is catching the
next ones.

## Known issues surfaced by this run

1. **`ui_dump` can fail with exit 137 / `UiAutomationService ... already registered!`** when dumps
   overlap. Hit 4 times, and it is what blocked Chat text-entry verification. The tool should
   serialise dumps per-serial and retry on this specific error.
2. **`logcat` cannot filter by package**, only by tag. The agent tried and had to fall back to
   reading unfiltered system noise. Add a package filter (resolve pid via `pidof`, filter by `--pid`).
