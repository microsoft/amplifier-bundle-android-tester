"""UI dump parsing, selectors, and the verified interaction protocol.

The load-bearing constraint this module encodes: **uiautomator is the sensor,
the screenshot is for judgment, never for targeting.** Coordinates always come
from the accessibility tree (parsed here), never from a human/VLM reading a
screenshot.

Every interaction here is selector-first: dump -> resolve selector -> act ->
re-dump -> verify. There is no bare `sleep()` used as a synchronisation
mechanism anywhere in this module — `wait_for()` polls; the small settle
delays after tap/text events are named constants, not ad-hoc sleeps standing
in for a real wait condition.
"""

from __future__ import annotations

import re
import shlex
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adb import AdbClientLike, AdbError
from .lock import dump_lock

__all__ = [
    "DEFAULT_DUMP_COLLISION_BACKOFF_S",
    "DEFAULT_DUMP_COLLISION_RETRIES",
    "DEFAULT_DUMP_REMOTE_PATH",
    "SETTLE_AFTER_TAP_S",
    "SETTLE_AFTER_TEXT_S",
    "Node",
    "SelectorError",
    "UiDumpCollisionError",
    "UiDumpError",
    "UiInteractionError",
    "center_of",
    "dismiss_anr",
    "dump_ui",
    "dump_ui_xml",
    "find_anr",
    "find_nodes",
    "parse_bounds",
    "parse_dump",
    "resolve_selector",
    "tap_selector",
    "tap_xy",
    "type_text",
    "wait_for",
]

# ---------------------------------------------------------------------------
# Constants (named, not magic sleeps)
# ---------------------------------------------------------------------------

DEFAULT_DUMP_REMOTE_PATH = "/sdcard/window_dump.xml"

# Short settle delays after an input event — these are NOT the synchronisation
# mechanism (wait_for's poll loop is); they just give the renderer a moment
# before the next dump so the "after" snapshot isn't a race against the
# animation that the tap/keystroke just triggered.
SETTLE_AFTER_TAP_S = 0.3
SETTLE_AFTER_TEXT_S = 0.3

KEYCODE_MOVE_END = 123
KEYCODE_DEL = 67
KEYCODE_BACK = 4

DELETE_PRESS_PADDING = 10
MIN_DELETE_PRESSES = 40

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_TRUE_VALUES = {"true", "1"}

_ANR_TEXT_MARKERS = ("isn't responding", "isn\u2019t responding")
_ANR_WAIT_TEXT = "Wait"

# `uiautomator dump` colliding with another live uiautomator instance on the
# same device raises this on-device, typically surfacing to adb shell as
# exit 137. Detected on message content, not bare exit code -- 137 (SIGKILL)
# has many unrelated causes (OOM, host kill); these two substrings are what
# uiautomator itself prints for exactly this collision.
_ALREADY_REGISTERED_MARKER = "already registered"
_UIAUTOMATION_SERVICE_MARKER = "UiAutomationService"

DEFAULT_DUMP_COLLISION_RETRIES = 3
DEFAULT_DUMP_COLLISION_BACKOFF_S = 0.5


class UiDumpError(RuntimeError):
    """Raised when a UI dump cannot be obtained or parsed."""


class UiDumpCollisionError(UiDumpError):
    """Raised when `uiautomator dump` collided with a competing uiautomator
    instance on the same device and the bounded retry was exhausted.

    Carries `.extra` (serial, attempts made, max_retries, the last observed
    stderr/stdout, and an explicit `note` of the honest limitation) so a
    caller sees exactly what was tried rather than a bare message -- same
    pattern as `adb.LaunchError`.
    """

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra: dict[str, Any] = extra


class SelectorError(RuntimeError):
    """Raised when a selector matches zero, or more than one, node ambiguously.

    Ambiguous and absent matches are *always* errors — never a silent
    first-match. `candidates` carries whatever nodes DID match (for the
    ambiguous case) so the caller can see exactly what needs disambiguating.
    """

    def __init__(self, message: str, candidates: list[Node] | None = None) -> None:
        super().__init__(message)
        self.candidates: list[Node] = candidates or []


class UiInteractionError(RuntimeError):
    """Raised when a verified interaction protocol step fails its assertion
    (focus not gained, readback mismatch, missing bounds, etc.)."""


# ---------------------------------------------------------------------------
# Node model + parsing
# ---------------------------------------------------------------------------


@dataclass
class Node:
    class_name: str
    text: str
    content_desc: str
    resource_id: str
    bounds: tuple[int, int, int, int] | None
    focused: bool
    clickable: bool
    enabled: bool
    checkable: bool = False
    checked: bool = False
    dump_index: int = -1

    @property
    def center(self) -> tuple[int, int] | None:
        if not self.bounds:
            return None
        return center_of(self.bounds)

    def has_content(self) -> bool:
        return bool(self.text or self.content_desc or self.resource_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.class_name,
            "text": self.text,
            "content_desc": self.content_desc,
            "resource_id": self.resource_id,
            "bounds": list(self.bounds) if self.bounds else None,
            "center": list(self.center) if self.center else None,
            "focused": self.focused,
            "clickable": self.clickable,
            "enabled": self.enabled,
            "checkable": self.checkable,
            "checked": self.checked,
        }


def parse_bounds(bounds_str: str | None) -> tuple[int, int, int, int] | None:
    """Parse a uiautomator bounds string like '[877,142][1006,195]'."""
    if not bounds_str:
        return None
    m = _BOUNDS_RE.match(bounds_str.strip())
    if not m:
        return None
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    return (x1, y1, x2, y2)


def center_of(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _bool_attr(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def parse_dump(xml_text: str) -> list[Node]:
    """Parse a uiautomator window dump (XML text) into a flat Node list."""
    stripped = xml_text.strip()
    if not stripped:
        raise UiDumpError(
            "UI dump was empty — the device screen may be off, unresponsive, "
            "or the accessibility service failed to attach."
        )
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError as exc:
        raise UiDumpError(f"Failed to parse UI dump XML: {exc}") from exc

    nodes: list[Node] = []
    for idx, elem in enumerate(root.iter("node")):
        nodes.append(
            Node(
                class_name=elem.get("class", ""),
                text=elem.get("text", ""),
                content_desc=elem.get("content-desc", ""),
                resource_id=elem.get("resource-id", ""),
                bounds=parse_bounds(elem.get("bounds")),
                focused=_bool_attr(elem.get("focused")),
                clickable=_bool_attr(elem.get("clickable")),
                enabled=_bool_attr(elem.get("enabled")),
                checkable=_bool_attr(elem.get("checkable")),
                checked=_bool_attr(elem.get("checked")),
                dump_index=idx,
            )
        )
    return nodes


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

_MATCH_KEYS = ("text", "text_contains", "res_id", "desc", "class")


def find_nodes(nodes: list[Node], selector: dict[str, Any]) -> list[Node]:
    """Return all nodes matching the AND-combined selector (excluding 'index',
    which only disambiguates in `resolve_selector`)."""
    candidates = list(nodes)

    if "text" in selector:
        target = selector["text"]
        candidates = [n for n in candidates if n.text == target]
    if "text_contains" in selector:
        target = selector["text_contains"]
        candidates = [n for n in candidates if target in n.text]
    if "res_id" in selector:
        target = selector["res_id"]
        candidates = [n for n in candidates if n.resource_id == target]
    if "desc" in selector:
        target = selector["desc"]
        candidates = [n for n in candidates if n.content_desc == target]
    if "class" in selector:
        target = selector["class"]
        candidates = [
            n
            for n in candidates
            if n.class_name == target or n.class_name.endswith("." + target)
        ]

    return candidates


def resolve_selector(nodes: list[Node], selector: dict[str, Any]) -> Node:
    """Resolve a selector dict to exactly one Node.

    An ambiguous match (more than one candidate, no disambiguating 'index')
    is an ERROR listing candidates — never a silent first-match. A selector
    matching zero nodes is likewise an error.
    """
    if not selector:
        raise SelectorError("Selector must not be empty.")

    if not any(k in selector for k in _MATCH_KEYS):
        raise SelectorError(
            f"Selector has no recognized match keys {_MATCH_KEYS}; got {sorted(selector.keys())}."
        )

    candidates = find_nodes(nodes, selector)

    if "index" in selector:
        idx = selector["index"]
        if not candidates:
            raise SelectorError(
                f"No nodes matched selector (before applying index={idx}): {selector}"
            )
        if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            raise SelectorError(
                f"index={idx!r} out of range for selector {selector} — "
                f"{len(candidates)} candidate(s) matched.",
                candidates=candidates,
            )
        return candidates[idx]

    if not candidates:
        raise SelectorError(f"No nodes matched selector: {selector}")
    if len(candidates) > 1:
        raise SelectorError(
            f"Selector {selector} matched {len(candidates)} nodes ambiguously; "
            "add 'index' to disambiguate or narrow the selector.",
            candidates=candidates,
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# ANR detection
# ---------------------------------------------------------------------------


def find_anr(nodes: list[Node]) -> dict[str, Any] | None:
    """Detect an ANR ("...isn't responding") dialog in a node list.

    Never auto-dismissed silently — this only detects and surfaces it. Use
    `dismiss_anr()` to actually tap "Wait", and even that reports what it did.
    """
    anr_node = next(
        (n for n in nodes if any(marker in n.text for marker in _ANR_TEXT_MARKERS)),
        None,
    )
    if anr_node is None:
        return None
    wait_candidates = [n for n in nodes if n.text == _ANR_WAIT_TEXT]
    wait_node = wait_candidates[0] if wait_candidates else None
    return {
        "detected": True,
        "message": anr_node.text,
        "wait_button_available": wait_node is not None,
        "wait_center": list(wait_node.center)
        if wait_node and wait_node.center
        else None,
    }


# ---------------------------------------------------------------------------
# Device I/O — dump
# ---------------------------------------------------------------------------


def _looks_like_uiautomator_collision(stdout: str, stderr: str) -> bool:
    """True if `stdout`/`stderr` show the on-device
    `IllegalStateException: UiAutomationService ... already registered!`
    collision -- checked on message content, never on exit code alone (see
    module-level constants for why)."""
    combined = f"{stdout}\n{stderr}"
    return (
        _ALREADY_REGISTERED_MARKER in combined
        or _UIAUTOMATION_SERVICE_MARKER in combined
    )


def dump_ui_xml(
    client: AdbClientLike,
    remote_path: str = DEFAULT_DUMP_REMOTE_PATH,
    *,
    lock_dir: Path | None = None,
    max_retries: int = DEFAULT_DUMP_COLLISION_RETRIES,
    retry_backoff_s: float = DEFAULT_DUMP_COLLISION_BACKOFF_S,
) -> str:
    """Run `uiautomator dump` on-device and return the resulting XML text.

    Serialised per-serial via an OS-level, cross-process file lock (see
    `lock.dump_lock`) -- `tap`, `type_text`, `find`, and `wait_for` all dump
    internally, and `wait_for`'s poll loop makes overlapping dumps against
    the same device likely the moment two invocations of this tool run
    concurrently against it. Two `uiautomator dump` processes racing on one
    device collide with `IllegalStateException: UiAutomationService ...
    already registered!` (adb shell typically reporting exit 137);
    serialising this tool's own dumps eliminates that self-inflicted case.

    On that specific collision signature (message content, not bare exit
    code), retries up to `max_retries` times with a `retry_backoff_s` pause
    between attempts. No other failure is retried -- a general retry here
    would mask real failures (device offline, screen off, etc.) behind a
    "just try again" that has nothing to do with this specific race.

    Raises:
        AdbError: the dump failed for a reason OTHER than the uiautomator
            collision signature.
        UiDumpCollisionError: the collision signature was seen and retries
            were exhausted. `.extra` names the serial, attempts made, and
            the last observed stderr/stdout, and its message states the
            honest limit explicitly: this lock only serialises *this
            tool's* dumps -- a different process on the host dumping the
            same device concurrently is not covered and can still collide.
        UiDumpError: the dump command succeeded but produced no output.
    """
    with dump_lock(client.serial, lock_dir):
        attempt = 0
        while True:
            result = client.shell(f"uiautomator dump {remote_path}", check_output=False)
            if result.ok:
                break
            if not _looks_like_uiautomator_collision(result.stdout, result.stderr):
                raise AdbError(
                    f"uiautomator dump failed ({result.returncode}): "
                    f"{(result.stderr or result.stdout).strip()}"
                )
            attempt += 1
            if attempt > max_retries:
                raise UiDumpCollisionError(
                    f"uiautomator dump on {client.serial!r} collided with a "
                    f"competing uiautomator instance ({max_retries} retries "
                    f"exhausted): {(result.stderr or result.stdout).strip()!r}. "
                    "This tool serialises its OWN dumps per-serial, but that "
                    "lock cannot serialise against a DIFFERENT process on "
                    "this host dumping the same device concurrently -- that "
                    "is the most likely remaining cause.",
                    serial=client.serial,
                    attempts=attempt,
                    max_retries=max_retries,
                    last_stderr=result.stderr,
                    last_stdout=result.stdout,
                )
            time.sleep(retry_backoff_s)

        cat_result = client.run("exec-out", "cat", remote_path, check_output=True)
        if not cat_result.stdout.strip():
            raise UiDumpError(
                "uiautomator dump produced no output — the device screen may "
                "be off, or the accessibility service failed to attach."
            )
        return cat_result.stdout


def dump_ui(
    client: AdbClientLike,
    remote_path: str = DEFAULT_DUMP_REMOTE_PATH,
    *,
    lock_dir: Path | None = None,
) -> list[Node]:
    """Dump and parse the current UI in one step."""
    xml_text = dump_ui_xml(client, remote_path=remote_path, lock_dir=lock_dir)
    return parse_dump(xml_text)


# ---------------------------------------------------------------------------
# Verified interaction protocol
# ---------------------------------------------------------------------------


def tap_selector(
    client: AdbClientLike,
    selector: dict[str, Any],
    *,
    remote_path: str = DEFAULT_DUMP_REMOTE_PATH,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    """dump -> resolve selector -> tap center -> re-dump -> report what changed."""
    nodes_before = dump_ui(client, remote_path, lock_dir=lock_dir)
    anr_before = find_anr(nodes_before)
    node = resolve_selector(nodes_before, selector)
    if node.center is None:
        raise UiInteractionError(
            f"Resolved node has no bounds/center for selector {selector}."
        )

    x, y = node.center
    client.run("shell", "input", "tap", str(x), str(y), check_output=True)
    time.sleep(SETTLE_AFTER_TAP_S)

    nodes_after = dump_ui(client, remote_path, lock_dir=lock_dir)
    anr_after = find_anr(nodes_after)

    return {
        "selector": selector,
        "tapped_at": [x, y],
        "before": node.to_dict(),
        "anr_before": anr_before,
        "anr_after": anr_after,
        "node_count_before": len(nodes_before),
        "node_count_after": len(nodes_after),
    }


def tap_xy(client: AdbClientLike, x: int, y: int) -> dict[str, Any]:
    """Raw-coordinate tap. Conspicuously named and always carries a warning —
    this is NOT the safe path; `tap_selector` is."""
    client.run("shell", "input", "tap", str(x), str(y), check_output=True)
    time.sleep(SETTLE_AFTER_TAP_S)
    return {
        "x": x,
        "y": y,
        "warning": (
            "tap_xy used raw coordinates with no selector resolution or "
            "verification. Coordinates are not validated against the current "
            "UI and may silently land on the wrong element (or nothing) if the "
            "screen differs from what you expect. Prefer 'tap' with a selector."
        ),
    }


def _encode_input_text(text: str) -> str:
    encoded = text.replace(" ", "%s")
    return shlex.quote(encoded)


def type_text(
    client: AdbClientLike,
    selector: dict[str, Any],
    text: str,
    *,
    remote_path: str = DEFAULT_DUMP_REMOTE_PATH,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    """The full verified field-write protocol.

    tap target -> re-dump -> ASSERT focused=="true" (error out if not) ->
    KEYCODE_MOVE_END + N*KEYCODE_DEL -> input text (shell-quoted) ->
    KEYCODE_BACK (dismiss IME — never "tap elsewhere") -> re-dump ->
    ASSERT readback equals intended (error out if not).
    """
    nodes = dump_ui(client, remote_path, lock_dir=lock_dir)
    node = resolve_selector(nodes, selector)
    if node.center is None:
        raise UiInteractionError(
            f"Resolved node has no bounds/center for selector {selector}."
        )

    x, y = node.center
    client.run("shell", "input", "tap", str(x), str(y), check_output=True)
    time.sleep(SETTLE_AFTER_TAP_S)

    nodes_focused = dump_ui(client, remote_path, lock_dir=lock_dir)
    focused_node = resolve_selector(nodes_focused, selector)
    if not focused_node.focused:
        raise UiInteractionError(
            f"Tapped target for selector {selector} but it did not gain focus "
            f"(focused={focused_node.focused!r}). Refusing to type into a field "
            "that isn't confirmed focused — this is exactly how a previous run "
            "typed a server URL into the wrong field."
        )

    existing_text = focused_node.text or ""
    delete_count = max(len(existing_text) + DELETE_PRESS_PADDING, MIN_DELETE_PRESSES)

    client.run("shell", "input", "keyevent", str(KEYCODE_MOVE_END), check_output=True)
    for _ in range(delete_count):
        client.run("shell", "input", "keyevent", str(KEYCODE_DEL), check_output=True)

    encoded = _encode_input_text(text)
    client.run("shell", "input", "text", encoded, check_output=True)
    client.run("shell", "input", "keyevent", str(KEYCODE_BACK), check_output=True)
    time.sleep(SETTLE_AFTER_TEXT_S)

    nodes_after = dump_ui(client, remote_path, lock_dir=lock_dir)
    after_node = resolve_selector(nodes_after, selector)
    if after_node.text != text:
        raise UiInteractionError(
            f"Readback mismatch after type_text: expected {text!r}, field now "
            f"reads {after_node.text!r}. Refusing to report success on an "
            "unverified write."
        )

    return {
        "selector": selector,
        "tapped_at": [x, y],
        "existing_text_before": existing_text,
        "delete_presses": delete_count,
        "text_written": text,
        "readback": after_node.text,
        "verified": True,
    }


def wait_for(
    client: AdbClientLike,
    selector: dict[str, Any],
    *,
    timeout_s: float = 10.0,
    poll_s: float = 1.0,
    absent: bool = False,
    remote_path: str = DEFAULT_DUMP_REMOTE_PATH,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    """Poll `ui_dump` until a selector appears (default) or disappears
    (absent=True), or timeout. This is the ONLY synchronisation mechanism in
    the module — there is no bare sleep standing in for it anywhere else."""
    start = time.monotonic()
    deadline = start + timeout_s
    match_count = 0

    while True:
        nodes = dump_ui(client, remote_path, lock_dir=lock_dir)
        match_count = len(find_nodes(nodes, selector))
        condition_met = (match_count == 0) if absent else (match_count > 0)
        elapsed = time.monotonic() - start
        if condition_met:
            return {
                "met": True,
                "selector": selector,
                "absent": absent,
                "match_count": match_count,
                "elapsed_s": elapsed,
            }
        if time.monotonic() >= deadline:
            return {
                "met": False,
                "selector": selector,
                "absent": absent,
                "match_count": match_count,
                "elapsed_s": elapsed,
            }
        time.sleep(poll_s)


def dismiss_anr(
    client: AdbClientLike,
    *,
    remote_path: str = DEFAULT_DUMP_REMOTE_PATH,
    lock_dir: Path | None = None,
) -> dict[str, Any]:
    """Tap 'Wait' on an ANR dialog if one is present. Never runs silently as a
    side effect of another operation — must be explicitly invoked, and always
    reports what it found and did."""
    nodes = dump_ui(client, remote_path, lock_dir=lock_dir)
    anr = find_anr(nodes)
    if not anr:
        return {"detected": False}

    if not anr["wait_button_available"]:
        return {
            "detected": True,
            "dismissed": False,
            "reason": "No 'Wait' button found in ANR dialog.",
            **anr,
        }

    x, y = anr["wait_center"]
    client.run("shell", "input", "tap", str(x), str(y), check_output=True)
    time.sleep(SETTLE_AFTER_TAP_S)

    nodes_after = dump_ui(client, remote_path, lock_dir=lock_dir)
    still_present = find_anr(nodes_after) is not None
    return {"detected": True, "dismissed": not still_present, **anr}
