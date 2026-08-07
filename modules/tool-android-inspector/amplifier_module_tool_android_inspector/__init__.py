"""Android Inspector Tool for Amplifier.

A single verb-dispatch tool for driving and inspecting Android apps on an
adb-reachable device or emulator — booting an AVD, installing/launching apps,
and interacting with the UI.

The load-bearing constraint: uiautomator is the sensor, the screenshot is for
judgment. Every interaction is selector-first (dump -> resolve -> act ->
re-dump -> verify); raw coordinates require explicit, conspicuously-named
opt-in (`tap_xy`) and always carry a warning.

Operations:
- list_devices, start_emulator, stop_emulator      (device/emulator lifecycle)
- install, launch, stop_app                        (app lifecycle)
- screenshot, ui_dump, find, logcat                (sensing)
- tap, tap_xy, type_text, key, swipe               (interacting)
- wait_for, dismiss_anr                             (synchronising)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .adb import (
    AdbClient,
    AdbError,
    LaunchError,
    launch_app,
    resolve_adb_binary,
    resolve_keycode,
    resolve_target_serial,
)
from .adb import list_raw_devices as _list_raw_devices
from .avd import (
    AvdError,
    require_avd_exists,
    run_doctor,
)
from .avd import (
    create_avd as _create_avd_impl,
)
from .emulator import (
    EmulatorError,
    resolve_emulator_binary,
)
from .emulator import (
    start_emulator as _start_emulator_impl,
)
from .emulator import (
    stop_emulator as _stop_emulator_impl,
)
from .evidence import unique_evidence_path
from .leases import DEFAULT_LEASE_DIR, AvdLeaseError
from .ui import (
    SelectorError,
    UiDumpCollisionError,
    UiDumpError,
    UiInteractionError,
    dump_ui,
    find_anr,
    find_nodes,
    tap_selector,
)
from .ui import (
    dismiss_anr as _dismiss_anr_impl,
)
from .ui import (
    tap_xy as _tap_xy_impl,
)
from .ui import (
    type_text as _type_text_impl,
)
from .ui import (
    wait_for as _wait_for_impl,
)

__all__ = ["AndroidInspectorTool", "mount"]

_OPERATIONS = (
    "list_devices",
    "start_emulator",
    "stop_emulator",
    "doctor",
    "create_avd",
    "install",
    "launch",
    "stop_app",
    "screenshot",
    "ui_dump",
    "find",
    "logcat",
    "tap",
    "tap_xy",
    "type_text",
    "key",
    "swipe",
    "wait_for",
    "dismiss_anr",
)

MIN_SCREENSHOT_BYTES = 1024  # a real screencap PNG is always far larger; anything less is a liveness failure

_LAUNCH_FOCUS_TIMEOUT_S = 10.0  # default for the arrival-confirmation poll in `launch`


def _err(message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _ok(output: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, **output}


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read width/height from a PNG's IHDR chunk (always the first chunk).
    Stdlib-only — no imaging library dependency for a liveness/geometry check."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class AndroidInspectorState:
    config: dict[str, Any]
    base_dir: Path
    run_dir: Path
    registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    _screenshot_counters: dict[str, int] = field(default_factory=dict)
    _adb_path: str | None = None

    @property
    def adb_path(self) -> str:
        if self._adb_path is None:
            self._adb_path = resolve_adb_binary(self.config)
        return self._adb_path

    @property
    def lease_dir(self) -> Path:
        """Host-global by default -- deliberately NOT derived from
        `base_dir`/`work_dir`. Three sessions with three different
        `work_dir`s must all see the SAME lease directory, or AVD-lease
        cross-session collision detection (see leases.py) never triggers.
        Overridable via config['lease_dir'] mainly for test isolation."""
        override = self.config.get("lease_dir")
        if override:
            return Path(str(override)).expanduser()
        return DEFAULT_LEASE_DIR

    def next_screenshot_index(self, serial: str) -> int:
        n = self._screenshot_counters.get(serial, 0) + 1
        self._screenshot_counters[serial] = n
        return n


def _build_state(config: dict[str, Any]) -> AndroidInspectorState:
    """Pure construction, no module-level caching -- see
    `AndroidInspectorTool._get_state()`. Defect 4: this used to be built
    once into a MODULE-level singleton (`get_state()`), so a second
    `mount()` call in the same process with a different `config` silently
    reused the first mount's state. Every mounted tool now builds and owns
    its own state."""
    base_dir = Path(
        str(config.get("work_dir", "~/.amplifier/android-sessions"))
    ).expanduser()
    base_dir.mkdir(parents=True, exist_ok=True)
    run_dir = base_dir / "_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return AndroidInspectorState(config=config, base_dir=base_dir, run_dir=run_dir)


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class AndroidInspectorTool:
    """Amplifier Tool for driving and inspecting Android apps via adb + uiautomator."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # Defect 4: config lives on THIS instance, not a module global -- a
        # second `mount()` in the same process (e.g. two Amplifier sessions
        # sharing one interpreter, or a test constructing multiple tools)
        # gets its own independent config and state, never silently
        # inheriting or clobbering another instance's.
        self._config: dict[str, Any] = config or {}
        self._state: AndroidInspectorState | None = None

    def _get_state(self) -> AndroidInspectorState:
        if self._state is None:
            self._state = _build_state(self._config)
        return self._state

    @property
    def name(self) -> str:
        return "android_inspector"

    @property
    def description(self) -> str:
        return (
            "Drive and inspect Android apps on an adb-reachable device or emulator.\n\n"
            "uiautomator is the sensor — every interaction resolves a selector against the "
            "live accessibility tree before acting. Screenshots are for visual judgment "
            "only, never for computing tap coordinates.\n\n"
            "Device & emulator lifecycle:\n"
            "- list_devices: enumerate attached devices; errors on ambiguity (>1 ready "
            "device, no explicit serial)\n"
            "- start_emulator: boot an AVD (avd, port), applying host workarounds, "
            "returns serial. Fast-fails immediately (no 60s timeout) if the AVD does "
            "not exist, naming existing AVDs and the avdmanager remediation command. "
            "Acquires an exclusive, cross-process lease on 'avd' first -- refuses "
            "immediately if another live process already has it booted, naming the "
            "owning pid/port/duration (each unrelated session should use its own AVD; "
            "see create_avd). Without an explicit port, allocates one atomically "
            "(skipping ports already attached or leased elsewhere); result carries "
            "port_allocation_fallback if allocation was impossible\n"
            "- stop_emulator: kill the emulator and reap its process. Refuses unless "
            "a live AVD lease for that serial is owned by the calling session -- "
            "names the owning pid rather than silently killing someone else's "
            "emulator. 'force': true overrides, but the result always carries a "
            "prominent 'warning' naming whose emulator was killed\n"
            "- doctor: full host readiness report (ANDROID_HOME, adb, emulator binary, "
            "KVM, ptrace_scope/gdb, AVDs, cmdline-tools) -- every check runs even if "
            "an earlier one fails; never errors, always returns a report\n"
            "- create_avd: provision a new AVD from an installed or "
            "sdkmanager-installable system image; never clobbers an existing AVD or "
            "silently accepts SDK licenses\n\n"
            "App lifecycle:\n"
            "- install: adb install -r -g (reinstall, grant all runtime perms)\n"
            "- launch: component -> am start directly (most deterministic); package -> "
            "resolve launcher activity via 'cmd package resolve-activity', am start; "
            "monkey -c LAUNCHER only if resolution yields nothing. Confirms arrival by "
            "polling mCurrentFocus for the target package before reporting success — "
            "never reports success on exit code alone\n"
            "- stop_app: am force-stop\n\n"
            "Sensing:\n"
            "- screenshot: writes a PNG to disk, returns image_path (never inline base64), "
            "geometry, byte size\n"
            "- ui_dump: parsed node list (class, text, content_desc, resource_id, bounds, "
            "center, focused, clickable, enabled) — not raw XML\n"
            "- find: nodes matching a selector, with resolved centers (does not error on "
            "0 or >1 matches)\n"
            "- logcat: tail/filter by tag ('filter_spec') and/or by 'package' (resolved "
            "to running pid(s) via pidof/ps, scoped with --pid; composes with "
            "filter_spec rather than overriding it; errors by name if the package isn't "
            "running, instead of returning an empty result)\n\n"
            "Interacting — selector-first:\n"
            "- tap: dump -> resolve selector -> tap center -> re-dump -> report what changed\n"
            "- type_text: tap -> assert focus -> MOVE_END + N*DEL -> input text -> BACK -> "
            "re-dump -> assert readback\n"
            "- key: keyevent by name (e.g. 'BACK') or numeric code\n"
            "- swipe: explicit coordinates (gestures have no selector analogue)\n"
            "- tap_xy: RAW coordinates, no selector resolution — always returns a warning\n\n"
            "Synchronising:\n"
            "- wait_for: polls ui_dump until a selector appears/disappears, or timeout — "
            "no bare sleeps\n"
            "- dismiss_anr: detect/dismiss an ANR ('...isn't responding') dialog; never "
            "auto-dismissed silently by other operations\n\n"
            'Selector: {"text": "Save"} | {"res_id": "com.foo:id/save"} | '
            '{"desc": "Home"} | {"class": "EditText", "index": 0} | '
            '{"text_contains": "poll"}. Multiple keys AND together. An ambiguous match '
            "(>1 node, no index) is an error listing candidates."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(_OPERATIONS),
                    "description": "Operation to perform",
                },
                "serial": {
                    "type": "string",
                    "description": (
                        "Device serial. If omitted, auto-resolved: uses the single ready "
                        "device, or errors if zero/ambiguous."
                    ),
                },
                "avd": {
                    "type": "string",
                    "description": (
                        "AVD name to boot (start_emulator). Must already exist -- "
                        "fast-fails immediately (no 60s timeout) if not. See 'doctor' "
                        "and 'create_avd' to provision one. Not the same as 'name' "
                        "(create_avd's new-AVD name)."
                    ),
                },
                "port": {
                    "type": "integer",
                    "description": (
                        "Emulator console port (start_emulator). Must be even and "
                        "in the 5554-5682 range adb scans for emulator consoles -- "
                        "the emulator uses 'port' for its console and 'port + 1' "
                        "for adb. Invalid values are rejected before anything is "
                        "launched -- never silently ignored or adjusted. When "
                        "given, start_emulator waits on the deterministic serial "
                        "'emulator-<port>' rather than 'any new serial', and "
                        "refuses to launch if that serial is already attached to "
                        "another device/emulator. When OMITTED, a free port is "
                        "allocated atomically (lowest free even port in range, "
                        "skipping ports already attached in adb devices or "
                        "recorded on another live AVD lease) -- only falls back "
                        "to waiting for 'any new serial' if every port in range "
                        "is taken, and the result names that fallback explicitly "
                        "via 'port_allocation_fallback'."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "New AVD name to create (create_avd).",
                },
                "api_level": {
                    "type": "integer",
                    "default": 35,
                    "description": "Android API level for the system image (create_avd)",
                },
                "tag": {
                    "type": "string",
                    "default": "google_apis",
                    "description": (
                        "System image tag, e.g. 'google_apis', 'google_apis_playstore' "
                        "(create_avd)"
                    ),
                },
                "abi": {
                    "type": "string",
                    "description": (
                        "System image ABI, e.g. 'arm64-v8a', 'x86_64' (create_avd). "
                        "Defaults to the host's native ABI (arm64-v8a on aarch64, "
                        "x86_64 otherwise) -- detected, never hardcoded. Override "
                        "only to request a non-native ABI."
                    ),
                },
                "device": {
                    "type": "string",
                    "default": "pixel_6",
                    "description": "avdmanager device profile, e.g. 'pixel_6' (create_avd)",
                },
                "accept_licenses": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "create_avd: auto-accept sdkmanager SDK licenses when the "
                        "system image isn't already installed. False (default) fails "
                        "loud instead of silently accepting licenses on your behalf."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "create_avd: overwrite an existing AVD of the same name. "
                        "Without this, create_avd errors rather than silently "
                        "clobbering an existing AVD.\n"
                        "stop_emulator: stop a serial even though its AVD lease is "
                        "held by a DIFFERENT live process (or has no lease record "
                        "at all). Without this, stop_emulator refuses and names "
                        "the owning pid. When used cross-owner, the result always "
                        "carries a prominent 'warning' field naming whose "
                        "emulator was killed -- never silent."
                    ),
                },
                "apk_path": {
                    "type": "string",
                    "description": "Path to a .apk (install)",
                },
                "package": {
                    "type": "string",
                    "description": (
                        "Package name (launch, stop_app, logcat). launch resolves the "
                        "launcher activity via 'cmd package resolve-activity', falling "
                        "back to 'monkey -c LAUNCHER' only if resolution yields nothing. "
                        "logcat: resolves to the package's running pid(s) (pidof, falling "
                        "back to 'ps -A') and scopes output to them via --pid, composing "
                        "with 'filter_spec' rather than overriding it. Errors (does not "
                        "return an empty result) if the package has no running process."
                    ),
                },
                "component": {
                    "type": "string",
                    "description": (
                        "ComponentName pkg/.Activity (launch). Most deterministic: used "
                        "directly with 'am start -n', no resolve/monkey fallback. "
                        "Preferred over 'package' when known."
                    ),
                },
                "selector": {
                    "type": "object",
                    "description": (
                        "Selector dict: text, text_contains, res_id, desc, class, "
                        "optional index to disambiguate"
                    ),
                },
                "text": {"type": "string", "description": "Text to type (type_text)"},
                "x": {"type": "integer", "description": "Raw X coordinate (tap_xy)"},
                "y": {"type": "integer", "description": "Raw Y coordinate (tap_xy)"},
                "x1": {"type": "integer", "description": "Swipe start X"},
                "y1": {"type": "integer", "description": "Swipe start Y"},
                "x2": {"type": "integer", "description": "Swipe end X"},
                "y2": {"type": "integer", "description": "Swipe end Y"},
                "duration_ms": {
                    "type": "integer",
                    "default": 300,
                    "description": "Swipe duration in milliseconds",
                },
                "keycode": {
                    "description": "Keyevent name (e.g. 'BACK') or numeric code (key op)",
                },
                "timeout_s": {
                    "type": "number",
                    "default": 10.0,
                    "description": (
                        "Timeout in seconds (wait_for; launch arrival-confirmation poll "
                        "of mCurrentFocus)"
                    ),
                },
                "poll_s": {
                    "type": "number",
                    "default": 1.0,
                    "description": "Poll interval in seconds (wait_for)",
                },
                "absent": {
                    "type": "boolean",
                    "default": False,
                    "description": "wait_for succeeds when the selector disappears, not appears",
                },
                "all_nodes": {
                    "type": "boolean",
                    "default": False,
                    "description": "ui_dump: include all nodes, not just those with text/desc/res-id",
                },
                "lines": {
                    "type": "integer",
                    "default": 200,
                    "description": "logcat: number of trailing lines to dump",
                },
                "filter_spec": {
                    "type": "string",
                    "description": "logcat: filter spec, e.g. 'MyTag:D *:S'",
                },
            },
            "required": ["operation"],
        }

    async def execute(self, input: dict[str, Any]) -> dict[str, Any]:
        operation = input.get("operation")
        if not operation:
            return _err("Missing required parameter: operation")

        state = self._get_state()

        try:
            match operation:
                case "list_devices":
                    return self._list_devices(state, input)
                case "start_emulator":
                    return self._start_emulator(state, input)
                case "stop_emulator":
                    return self._stop_emulator(state, input)
                case "doctor":
                    return self._doctor(state, input)
                case "create_avd":
                    return self._create_avd(state, input)
                case "install":
                    return self._install(state, input)
                case "launch":
                    return self._launch(state, input)
                case "stop_app":
                    return self._stop_app(state, input)
                case "screenshot":
                    return self._screenshot(state, input)
                case "ui_dump":
                    return self._ui_dump(state, input)
                case "find":
                    return self._find(state, input)
                case "logcat":
                    return self._logcat(state, input)
                case "tap":
                    return self._tap(state, input)
                case "tap_xy":
                    return self._tap_xy(state, input)
                case "type_text":
                    return self._type_text(state, input)
                case "key":
                    return self._key(state, input)
                case "swipe":
                    return self._swipe(state, input)
                case "wait_for":
                    return self._wait_for(state, input)
                case "dismiss_anr":
                    return self._dismiss_anr(state, input)
                case _:
                    return _err(f"Unknown operation: {operation}")
        except LaunchError as exc:
            # LaunchError subclasses AdbError -- must be caught before it, so
            # its structured `attempts` (and whatever else it learned) reach
            # the caller instead of being collapsed to a bare message.
            return _err(str(exc), **exc.extra)
        except AvdError as exc:
            # Carries structured fields (existing_avds, remediation_command,
            # missing_tools, ...) that must reach the caller intact -- same
            # pattern as LaunchError.
            return _err(str(exc), **exc.extra)
        except AvdLeaseError as exc:
            # AVD-lease refusal (Defect 1: already leased by a live pid) or
            # stop_emulator ownership refusal (Defect 3) -- carries
            # owner_pid/avd/port/held_for_s so the caller sees exactly who
            # holds it, not a bare "refused" message.
            return _err(str(exc), **exc.extra)
        except SelectorError as exc:
            return _err(str(exc), candidates=[n.to_dict() for n in exc.candidates])
        except UiDumpCollisionError as exc:
            # UiDumpCollisionError subclasses UiDumpError -- must be caught
            # before it, so its structured `.extra` (serial, attempts,
            # max_retries, last observed stderr/stdout) reaches the caller
            # instead of being collapsed to a bare message. Same pattern as
            # LaunchError/AvdError above.
            return _err(str(exc), **exc.extra)
        except (AdbError, EmulatorError, UiDumpError, UiInteractionError) as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"Operation '{operation}' failed: {exc}")

    # -- Helpers --------------------------------------------------------

    def _client_for(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> AdbClient:
        serial = resolve_target_serial(
            inp.get("serial"),
            state.adb_path,
            default_serial=state.config.get("default_serial"),
        )
        return AdbClient(
            serial=serial,
            adb_path=state.adb_path,
            timeout=float(state.config.get("adb_timeout_s", 30.0)),
        )

    # -- Device & emulator lifecycle -------------------------------------

    def _list_devices(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        devices = _list_raw_devices(state.adb_path)
        explicit_serial = inp.get("serial")
        if explicit_serial:
            matches = [d for d in devices if d.serial == explicit_serial]
            return _ok({"devices": [asdict(d) for d in matches], "count": len(matches)})

        ready = [d for d in devices if d.ready]
        not_ready = [d for d in devices if not d.ready]
        if len(ready) > 1:
            return _err(
                "Ambiguous: multiple ready devices attached with no explicit serial: "
                + ", ".join(d.serial for d in ready)
                + ". Pass 'serial' to disambiguate.",
                devices=[asdict(d) for d in devices],
            )
        return _ok(
            {
                "devices": [asdict(d) for d in devices],
                "count": len(devices),
                "not_ready": [asdict(d) for d in not_ready],
            }
        )

    def _start_emulator(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        avd = inp.get("avd")
        if not avd:
            return _err("Missing required parameter: avd")

        raw_port = inp.get("port")
        port = int(raw_port) if raw_port is not None else None

        # Fast-fail BEFORE spawning anything: a missing AVD is detectable
        # instantly from <name>.ini files; it must never pay the ~60s
        # device-appear timeout that a doomed launch would otherwise
        # silently eat (measured: 60.0s to fail on a typo'd AVD name).
        # Best-effort resolve the emulator binary for the -list-avds
        # cross-check; if it doesn't resolve, require_avd_exists still
        # checks disk and raises AvdError with existing_avds/remediation.
        try:
            probe_emulator_binary: str | None = resolve_emulator_binary(state.config)
        except EmulatorError:
            probe_emulator_binary = None
        require_avd_exists(avd, state.config, emulator_binary=probe_emulator_binary)

        # `port` (even, in adb's console-scan range, and not already
        # attached) is validated inside _start_emulator_impl, BEFORE the
        # emulator process is ever spawned -- see emulator.start_emulator.
        # Cross-process AVD lease acquisition (Defect 1) and, when `port`
        # is omitted, atomic port allocation (Defect 2) also happen inside
        # _start_emulator_impl, against the HOST-GLOBAL `state.lease_dir`
        # -- never derived from this session's `work_dir`, so unrelated
        # sessions with different work_dirs still see each other's leases.
        result = _start_emulator_impl(
            avd=avd,
            adb_path=state.adb_path,
            config=state.config,
            run_dir=state.run_dir,
            port=port,
            lease_dir=state.lease_dir,
        )
        state.registry[result["serial"]] = {
            "avd": avd,
            "pid": result["pid"],
            "log_path": result["log_path"],
        }
        return _ok(result)

    def _stop_emulator(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        serial = inp.get("serial") or state.config.get("default_serial")
        if not serial:
            if len(state.registry) == 1:
                serial = next(iter(state.registry))
            else:
                return _err(
                    "Missing required parameter: serial (0 or >1 tracked emulators; "
                    "specify one explicitly)."
                )
        meta = state.registry.get(serial, {})
        client = AdbClient(serial=serial, adb_path=state.adb_path)
        # Ownership check (Defect 3) happens inside _stop_emulator_impl,
        # against the recorded AVD lease for this serial -- not against
        # `state.registry`, which is per-process/in-memory and would never
        # even see an emulator a DIFFERENT session started.
        result = _stop_emulator_impl(
            client,
            pid=meta.get("pid"),
            force=bool(inp.get("force", False)),
            lease_dir=state.lease_dir,
        )
        state.registry.pop(serial, None)
        result["serial"] = serial
        return _ok(result)

    def _doctor(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        # run_doctor() never raises and never stops at the first failure --
        # the whole point is to show everything wrong at once. `success` is
        # always true here: a machine with problems is a successful
        # diagnosis, not a tool error.
        report = run_doctor(state.config)
        return _ok(report)

    def _create_avd(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        name = inp.get("name")
        if not name:
            return _err("Missing required parameter: name")
        result = _create_avd_impl(
            name=name,
            config=state.config,
            api_level=int(inp.get("api_level", 35)),
            tag=str(inp.get("tag", "google_apis")),
            abi=inp.get("abi"),
            device=str(inp.get("device", "pixel_6")),
            accept_licenses=bool(inp.get("accept_licenses", False)),
            force=bool(inp.get("force", False)),
        )
        return _ok(result)

    # -- App lifecycle ----------------------------------------------------

    def _install(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        apk_path = inp.get("apk_path")
        if not apk_path:
            return _err("Missing required parameter: apk_path")
        client = self._client_for(state, inp)
        result = client.install(apk_path, reinstall=True, grant=True)
        return _ok(
            {
                "serial": client.serial,
                "apk_path": apk_path,
                "stdout": result.stdout.strip(),
            }
        )

    def _launch(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        component = inp.get("component")
        package = inp.get("package")
        if not component and not package:
            return _err("Missing required parameter: component or package")
        client = self._client_for(state, inp)
        # launch_app owns the full resolution order (explicit component ->
        # resolve-activity -> monkey fallback) and arrival confirmation; it
        # raises LaunchError (caught in execute()) naming every mechanism
        # tried if none succeed or arrival is never confirmed.
        result = launch_app(
            client,
            component=component,
            package=package,
            timeout_s=float(inp.get("timeout_s", _LAUNCH_FOCUS_TIMEOUT_S)),
        )
        return _ok(
            {
                "serial": client.serial,
                "component": component,
                "package": package,
                **result,
            }
        )

    def _stop_app(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        package = inp.get("package")
        if not package:
            return _err("Missing required parameter: package")
        client = self._client_for(state, inp)
        client.am_force_stop(package)
        return _ok({"serial": client.serial, "package": package, "status": "stopped"})

    # -- Sensing ----------------------------------------------------------

    def _screenshot(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        client = self._client_for(state, inp)
        data = client.screencap_bytes()
        if len(data) < MIN_SCREENSHOT_BYTES:
            return _err(
                f"Screenshot liveness check failed: only {len(data)} bytes captured "
                f"(expected a real PNG, minimum {MIN_SCREENSHOT_BYTES})."
            )
        out_dir = state.base_dir / client.serial
        out_dir.mkdir(parents=True, exist_ok=True)
        # `idx` is kept only for human-readable ordering within this process —
        # it plays no role in collision prevention (an in-process counter
        # resets to 0 every new process, which previously caused separate
        # invocations to silently overwrite each other's screenshots).
        # `unique_evidence_path` is what actually guarantees no clobbering:
        # a UTC-timestamp + random-suffix name, verified against disk.
        idx = state.next_screenshot_index(client.serial)
        path = unique_evidence_path(out_dir, "screenshot", "png", index=idx)
        path.write_bytes(data)
        dims = _png_dimensions(data)
        return _ok(
            {
                "serial": client.serial,
                "image_path": str(path.resolve()),
                "byte_size": len(data),
                "width": dims[0] if dims else None,
                "height": dims[1] if dims else None,
            }
        )

    def _ui_dump(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        client = self._client_for(state, inp)
        nodes = dump_ui(client, lock_dir=state.run_dir)
        all_nodes = bool(inp.get("all_nodes", False))
        filtered = nodes if all_nodes else [n for n in nodes if n.has_content()]
        anr = find_anr(nodes)
        return _ok(
            {
                "serial": client.serial,
                "nodes": [n.to_dict() for n in filtered],
                "node_count": len(filtered),
                "total_node_count": len(nodes),
                "anr": anr,
            }
        )

    def _find(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        selector = inp.get("selector")
        if not selector:
            return _err("Missing required parameter: selector")
        client = self._client_for(state, inp)
        nodes = dump_ui(client, lock_dir=state.run_dir)
        matches = find_nodes(nodes, selector)
        return _ok(
            {
                "serial": client.serial,
                "selector": selector,
                "matches": [n.to_dict() for n in matches],
                "count": len(matches),
            }
        )

    def _logcat(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        client = self._client_for(state, inp)
        lines = int(inp.get("lines", 200))
        filter_spec = inp.get("filter_spec")
        package = inp.get("package")

        pids: list[str] | None = None
        if package:
            resolved_pids = client.resolve_package_pids(package)
            if not resolved_pids:
                # Distinct from "no log output": there is nothing to filter
                # for because the package has no running process at all --
                # never silently returned as an empty (and therefore
                # ambiguous-looking) log result.
                return _err(
                    f"Package {package!r} is not running on {client.serial!r} -- "
                    "no pid to filter logcat by.",
                    serial=client.serial,
                    package=package,
                )
            pids = resolved_pids

        dump = client.logcat_dump(lines=lines, filter_spec=filter_spec, pids=pids)
        out_lines = dump.stdout.splitlines()
        response: dict[str, Any] = {
            "serial": client.serial,
            "lines": out_lines,
            "count": len(out_lines),
        }
        if package:
            response["package"] = package
            response["pids_requested"] = dump.pids_requested
            response["pids_used"] = dump.pids_used
            if dump.pid_fallback_reason:
                response["pid_fallback_reason"] = dump.pid_fallback_reason
        return _ok(response)

    # -- Interacting --------------------------------------------------------

    def _tap(self, state: AndroidInspectorState, inp: dict[str, Any]) -> dict[str, Any]:
        selector = inp.get("selector")
        if not selector:
            return _err("Missing required parameter: selector")
        client = self._client_for(state, inp)
        result = tap_selector(client, selector, lock_dir=state.run_dir)
        result["serial"] = client.serial
        return _ok(result)

    def _tap_xy(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        if "x" not in inp or "y" not in inp:
            return _err("Missing required parameters: x, y")
        client = self._client_for(state, inp)
        result = _tap_xy_impl(client, int(inp["x"]), int(inp["y"]))
        result["serial"] = client.serial
        return _ok(result)

    def _type_text(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        selector = inp.get("selector")
        text = inp.get("text")
        if not selector:
            return _err("Missing required parameter: selector")
        if text is None:
            return _err("Missing required parameter: text")
        client = self._client_for(state, inp)
        result = _type_text_impl(client, selector, text, lock_dir=state.run_dir)
        result["serial"] = client.serial
        return _ok(result)

    def _key(self, state: AndroidInspectorState, inp: dict[str, Any]) -> dict[str, Any]:
        keycode = inp.get("keycode")
        if keycode is None:
            return _err("Missing required parameter: keycode")
        client = self._client_for(state, inp)
        resolved = resolve_keycode(keycode)
        client.keyevent(resolved)
        return _ok(
            {"serial": client.serial, "keycode": keycode, "resolved_code": resolved}
        )

    def _swipe(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        required = ("x1", "y1", "x2", "y2")
        missing = [k for k in required if k not in inp]
        if missing:
            return _err(f"Missing required parameters: {', '.join(missing)}")
        client = self._client_for(state, inp)
        duration_ms = int(inp.get("duration_ms", 300))
        client.input_swipe(
            int(inp["x1"]), int(inp["y1"]), int(inp["x2"]), int(inp["y2"]), duration_ms
        )
        return _ok(
            {
                "serial": client.serial,
                "from": [inp["x1"], inp["y1"]],
                "to": [inp["x2"], inp["y2"]],
                "duration_ms": duration_ms,
            }
        )

    # -- Synchronising --------------------------------------------------------

    def _wait_for(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        selector = inp.get("selector")
        if not selector:
            return _err("Missing required parameter: selector")
        client = self._client_for(state, inp)
        result = _wait_for_impl(
            client,
            selector,
            timeout_s=float(inp.get("timeout_s", 10.0)),
            poll_s=float(inp.get("poll_s", 1.0)),
            absent=bool(inp.get("absent", False)),
            lock_dir=state.run_dir,
        )
        result["serial"] = client.serial
        return _ok(result)

    def _dismiss_anr(
        self, state: AndroidInspectorState, inp: dict[str, Any]
    ) -> dict[str, Any]:
        client = self._client_for(state, inp)
        result = _dismiss_anr_impl(client, lock_dir=state.run_dir)
        result["serial"] = client.serial
        return _ok(result)


# ---------------------------------------------------------------------------
# Amplifier module mount point
# ---------------------------------------------------------------------------


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> AndroidInspectorTool:
    """Mount the android_inspector tool onto the Amplifier coordinator.

    Args:
        coordinator: Amplifier coordinator for tool registration
        config: Configuration from behaviors/android-tester.yaml. Keys:
            work_dir: Base directory for screenshots/logs (default: ~/.amplifier/android-sessions)
            adb_path: Explicit adb binary override
            android_home: Override for $ANDROID_HOME/$ANDROID_SDK_ROOT
            adb_timeout_s: Per-command adb timeout (default: 30.0)
            default_serial: Device serial to use when none is given and >1 device is ready
            emulator_path: Explicit emulator binary override
            emulator_args: Override the default emulator flags
            gdb_path: Explicit gdb binary override
            device_appear_timeout_s: How long to wait for a new serial after launch (default: 60.0)
            boot_timeout_s: How long to wait for sys.boot_completed (default: 240.0)
            lease_dir: Override for the AVD-lease directory (default:
                ~/.amplifier/android-sessions/leases -- deliberately NOT
                derived from work_dir; see leases.py). Mainly for test
                isolation -- production callers should not need this.

    Each `mount()` call builds its OWN tool instance and config (Defect 4) --
    a second `mount()` in the same process, e.g. with a different
    `work_dir`, is fully independent and never silently shares or overwrites
    the first mount's state.

    Returns:
        The mounted tool instance.
    """
    tool = AndroidInspectorTool(config=config or {})
    await coordinator.mount("tools", tool, name=tool.name)
    return tool
