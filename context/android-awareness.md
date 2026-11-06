# Android testing

For Android emulator/device UI tasks, use `android-tester:android-operator`
(driving flows), `android-tester:android-visual-tester` (rendered UI), or
`android-tester:android-debugger` (failures). They use `android_inspector` and
carry the setup and troubleshooting procedures. Route web UI to browser-tester,
iOS to ios-tester, and CLI/TUI to terminal-tester.

Target the intended device serial explicitly. Use the accessibility tree and
selectors for input; screenshots are for visual judgment, never guessed tap
coordinates. Run the tool's `doctor` when preparing the target; report missing
prerequisites and their actual fixes. Preserve the user's device/data scope and
permission requirements. Delegate device work to the specialist rather than
bypassing the tool with raw adb commands.

If working directly with the tool, first read
[Android procedures](android-procedures.md) for setup, supported operations and
failure handling. Unrelated work does not need this manual.
