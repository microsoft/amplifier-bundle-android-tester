"""Shared test fixtures — pure unit tests, no emulator/device dependency."""

from __future__ import annotations

from dataclasses import dataclass, field

from amplifier_module_tool_android_inspector.adb import AdbCommandResult

SAMPLE_DUMP_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        content-desc="" bounds="[0,0][1080,2160]" clickable="false" enabled="true"
        focused="false" checkable="false" checked="false" />
  <node index="1" text="http://old-server:9000" resource-id="com.foo:id/base_url"
        class="android.widget.EditText" content-desc="" bounds="[100,300][900,400]"
        clickable="true" enabled="true" focused="true" checkable="false" checked="false" />
  <node index="2" text="" resource-id="com.foo:id/api_key"
        class="android.widget.EditText" content-desc="" bounds="[100,500][900,600]"
        clickable="true" enabled="true" focused="false" checkable="false" checked="false" />
  <node index="3" text="Save" resource-id="com.foo:id/save_button"
        class="android.widget.Button" content-desc="" bounds="[100,700][400,800]"
        clickable="true" enabled="true" focused="false" checkable="false" checked="false" />
  <node index="4" text="" resource-id="" class="android.widget.ImageView"
        content-desc="Home" bounds="[0,2000][200,2160]" clickable="true" enabled="true"
        focused="false" checkable="false" checked="false" />
  <node index="5" text="" resource-id="" class="android.widget.ImageView"
        content-desc="Settings" bounds="[200,2000][400,2160]" clickable="true" enabled="true"
        focused="false" checkable="false" checked="false" />
</hierarchy>
"""

# Two ambiguous EditText nodes with no other distinguishing selector key.
AMBIGUOUS_DUMP_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.EditText"
        content-desc="" bounds="[100,100][900,200]" clickable="true" enabled="true"
        focused="false" checkable="false" checked="false" />
  <node index="1" text="" resource-id="" class="android.widget.EditText"
        content-desc="" bounds="[100,300][900,400]" clickable="true" enabled="true"
        focused="false" checkable="false" checked="false" />
</hierarchy>
"""

ANR_DUMP_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="com.foo isn't responding" resource-id="android:id/message"
        class="android.widget.TextView" content-desc="" bounds="[100,900][900,1000]"
        clickable="false" enabled="true" focused="false" checkable="false" checked="false" />
  <node index="1" text="Wait" resource-id="android:id/aerr_wait"
        class="android.widget.Button" content-desc="" bounds="[100,1100][400,1200]"
        clickable="true" enabled="true" focused="false" checkable="false" checked="false" />
</hierarchy>
"""


@dataclass
class FakeAdbClient:
    """Duck-typed stand-in for AdbClient — implements only what ui.py needs
    (.serial, .shell(), .run()) and records every call for assertions."""

    serial: str = "fake-serial-0001"
    dump_xml_queue: list[str] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    default_dump_xml: str = SAMPLE_DUMP_XML

    def shell(
        self, command, *, check_output: bool = False, timeout: float | None = None
    ) -> AdbCommandResult:
        if isinstance(command, (list, tuple)):
            args = ("shell", *command)
        else:
            args = ("shell", command)
        self.calls.append(args)
        return AdbCommandResult(args=list(args), returncode=0, stdout="", stderr="")

    def run(
        self, *args: str, timeout: float | None = None, check_output: bool = False
    ) -> AdbCommandResult:
        self.calls.append(args)
        if args[:2] == ("exec-out", "cat"):
            xml = (
                self.dump_xml_queue.pop(0)
                if self.dump_xml_queue
                else self.default_dump_xml
            )
            return AdbCommandResult(
                args=list(args), returncode=0, stdout=xml, stderr=""
            )
        return AdbCommandResult(args=list(args), returncode=0, stdout="", stderr="")

    def keyevent_calls(self) -> list[str]:
        return [
            args[3] for args in self.calls if args[:3] == ("shell", "input", "keyevent")
        ]

    def tap_calls(self) -> list[tuple[str, str]]:
        return [
            (args[3], args[4])
            for args in self.calls
            if args[:3] == ("shell", "input", "tap")
        ]

    def text_calls(self) -> list[str]:
        return [
            args[3] for args in self.calls if args[:3] == ("shell", "input", "text")
        ]

    def wait_for_device(self, timeout: float | None = None) -> AdbCommandResult:
        args = ("wait-for-device",)
        self.calls.append(args)
        return AdbCommandResult(args=list(args), returncode=0, stdout="", stderr="")

    def emu_kill(self) -> AdbCommandResult:
        args = ("emu", "kill")
        self.calls.append(args)
        return AdbCommandResult(args=list(args), returncode=0, stdout="", stderr="")


@dataclass
class FakeProcess:
    """Test double for a Popen-like process handle (Defect 2: dead-process
    detection). `.poll()` returns `None` (alive) until it has been called
    `exit_after` times, then reports `exit_returncode` on every call after
    that -- lets a test simulate a process dying partway through a wait
    loop rather than being dead (or alive) for the whole test."""

    pid: int
    exit_after: int | None = None
    exit_returncode: int = -11  # SIGSEGV by default
    calls: int = field(default=0, init=False)
    returncode: int | None = field(default=None, init=False)

    def poll(self) -> int | None:
        self.calls += 1
        if (
            self.returncode is None
            and self.exit_after is not None
            and self.calls >= self.exit_after
        ):
            self.returncode = self.exit_returncode
        return self.returncode


class RecordingRunner:
    """An injectable AdbClient runner that records every argv it was called
    with and returns canned results keyed by the argv tuple *after*
    [adb_path, "-s", serial] (i.e. the subcommand)."""

    def __init__(
        self,
        responses: dict[tuple[str, ...], AdbCommandResult] | None = None,
        default: AdbCommandResult | None = None,
    ) -> None:
        self.responses = responses or {}
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> AdbCommandResult:
        self.calls.append(argv)
        key = tuple(argv[3:])
        if key in self.responses:
            return self.responses[key]
        if self.default is not None:
            return self.default
        return AdbCommandResult(args=argv, returncode=0, stdout="", stderr="")


def make_text_runner(
    responses: dict[tuple[str, ...], AdbCommandResult] | None = None,
    default: AdbCommandResult | None = None,
) -> RecordingRunner:
    """Build a `RecordingRunner` — kept as a function for a familiar call site."""
    return RecordingRunner(responses=responses, default=default)
