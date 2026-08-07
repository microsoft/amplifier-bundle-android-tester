"""Unit tests for ui.py — dump parsing, selector matching, bounds/center
parsing, ANR detection, and the verified interaction protocols (tap,
type_text, wait_for, dismiss_anr) against a FakeAdbClient double. No
emulator/device dependency."""

from __future__ import annotations

import pytest
from amplifier_module_tool_android_inspector.adb import AdbCommandResult, AdbError
from amplifier_module_tool_android_inspector.ui import (
    KEYCODE_BACK,
    KEYCODE_DEL,
    KEYCODE_MOVE_END,
    MIN_DELETE_PRESSES,
    SelectorError,
    UiDumpCollisionError,
    UiDumpError,
    UiDumpTimeoutError,
    UiInteractionError,
    center_of,
    dismiss_anr,
    dump_ui_xml,
    find_anr,
    find_nodes,
    parse_bounds,
    parse_dump,
    resolve_selector,
    tap_selector,
    tap_xy,
    type_text,
    wait_for,
)

from .conftest import AMBIGUOUS_DUMP_XML, ANR_DUMP_XML, SAMPLE_DUMP_XML, FakeAdbClient

# ---------------------------------------------------------------------------
# bounds / center parsing
# ---------------------------------------------------------------------------


def test_parse_bounds_valid() -> None:
    assert parse_bounds("[877,142][1006,195]") == (877, 142, 1006, 195)


def test_parse_bounds_none_for_empty_or_missing() -> None:
    assert parse_bounds("") is None
    assert parse_bounds(None) is None


def test_parse_bounds_none_for_malformed() -> None:
    assert parse_bounds("not-bounds") is None


def test_center_of_measured_example() -> None:
    # The exact measured example from the design doc.
    assert center_of((877, 142, 1006, 195)) == (941, 168)


def test_center_of_rounds_down_on_odd_sum() -> None:
    assert center_of((0, 0, 1, 1)) == (0, 0)


# ---------------------------------------------------------------------------
# parse_dump
# ---------------------------------------------------------------------------


def test_parse_dump_extracts_all_fields() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    assert len(nodes) == 6

    url_field = next(n for n in nodes if n.resource_id == "com.foo:id/base_url")
    assert url_field.class_name == "android.widget.EditText"
    assert url_field.text == "http://old-server:9000"
    assert url_field.focused is True
    assert url_field.clickable is True
    assert url_field.enabled is True
    assert url_field.bounds == (100, 300, 900, 400)
    assert url_field.center == (500, 350)


def test_parse_dump_empty_raises() -> None:
    with pytest.raises(UiDumpError):
        parse_dump("")
    with pytest.raises(UiDumpError):
        parse_dump("   ")


def test_parse_dump_malformed_xml_raises() -> None:
    with pytest.raises(UiDumpError):
        parse_dump("<hierarchy><node ")


def test_node_has_content() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    frame = next(n for n in nodes if n.class_name == "android.widget.FrameLayout")
    save_button = next(n for n in nodes if n.text == "Save")
    assert frame.has_content() is False
    assert save_button.has_content() is True


# ---------------------------------------------------------------------------
# selector matching
# ---------------------------------------------------------------------------


def test_find_nodes_by_text() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    matches = find_nodes(nodes, {"text": "Save"})
    assert len(matches) == 1
    assert matches[0].resource_id == "com.foo:id/save_button"


def test_find_nodes_by_text_contains() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    matches = find_nodes(nodes, {"text_contains": "old-server"})
    assert len(matches) == 1


def test_find_nodes_by_res_id() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    matches = find_nodes(nodes, {"res_id": "com.foo:id/api_key"})
    assert len(matches) == 1
    assert matches[0].class_name == "android.widget.EditText"


def test_find_nodes_by_desc() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    matches = find_nodes(nodes, {"desc": "Settings"})
    assert len(matches) == 1


def test_find_nodes_by_class_suffix_match() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    matches = find_nodes(nodes, {"class": "EditText"})
    assert len(matches) == 2


def test_find_nodes_multiple_keys_and_together() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    matches = find_nodes(nodes, {"class": "EditText", "text_contains": "old-server"})
    assert len(matches) == 1


def test_find_nodes_no_match_returns_empty() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    assert find_nodes(nodes, {"text": "Nonexistent"}) == []


# ---------------------------------------------------------------------------
# resolve_selector — ambiguity is an error, never a silent first-match
# ---------------------------------------------------------------------------


def test_resolve_selector_unique_match() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    node = resolve_selector(nodes, {"text": "Save"})
    assert node.resource_id == "com.foo:id/save_button"


def test_resolve_selector_ambiguous_raises_with_candidates() -> None:
    nodes = parse_dump(AMBIGUOUS_DUMP_XML)
    with pytest.raises(SelectorError) as excinfo:
        resolve_selector(nodes, {"class": "EditText"})
    assert len(excinfo.value.candidates) == 2


def test_resolve_selector_absent_raises() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    with pytest.raises(SelectorError):
        resolve_selector(nodes, {"text": "Nonexistent"})


def test_resolve_selector_empty_selector_raises() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    with pytest.raises(SelectorError):
        resolve_selector(nodes, {})


def test_resolve_selector_unrecognized_keys_raises() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    with pytest.raises(SelectorError):
        resolve_selector(nodes, {"bogus_key": "x"})


def test_resolve_selector_index_disambiguates() -> None:
    nodes = parse_dump(AMBIGUOUS_DUMP_XML)
    first = resolve_selector(nodes, {"class": "EditText", "index": 0})
    second = resolve_selector(nodes, {"class": "EditText", "index": 1})
    assert first.bounds == (100, 100, 900, 200)
    assert second.bounds == (100, 300, 900, 400)


def test_resolve_selector_index_out_of_range_raises() -> None:
    nodes = parse_dump(AMBIGUOUS_DUMP_XML)
    with pytest.raises(SelectorError):
        resolve_selector(nodes, {"class": "EditText", "index": 5})


def test_resolve_selector_index_with_no_candidates_raises() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    with pytest.raises(SelectorError):
        resolve_selector(nodes, {"text": "Nonexistent", "index": 0})


# ---------------------------------------------------------------------------
# ANR detection
# ---------------------------------------------------------------------------


def test_find_anr_detects_dialog_and_wait_button() -> None:
    nodes = parse_dump(ANR_DUMP_XML)
    anr = find_anr(nodes)
    assert anr is not None
    assert anr["detected"] is True
    assert "isn't responding" in anr["message"]
    assert anr["wait_button_available"] is True
    assert anr["wait_center"] == [250, 1150]


def test_find_anr_none_when_absent() -> None:
    nodes = parse_dump(SAMPLE_DUMP_XML)
    assert find_anr(nodes) is None


# ---------------------------------------------------------------------------
# tap_selector — dump -> resolve -> tap -> re-dump
# ---------------------------------------------------------------------------


def test_tap_selector_taps_computed_center_and_redumps() -> None:
    client = FakeAdbClient()
    result = tap_selector(client, {"text": "Save"})

    assert result["tapped_at"] == [250, 750]  # center of [100,700][400,800]
    assert client.tap_calls() == [("250", "750")]
    # Two dumps: before and after (each dump = shell "uiautomator dump" + exec-out cat)
    dump_calls = [c for c in client.calls if c[:2] == ("exec-out", "cat")]
    assert len(dump_calls) == 2


def test_tap_selector_ambiguous_selector_raises() -> None:
    client = FakeAdbClient(default_dump_xml=AMBIGUOUS_DUMP_XML)
    with pytest.raises(SelectorError):
        tap_selector(client, {"class": "EditText"})


def test_tap_selector_surfaces_anr_without_dismissing() -> None:
    # Both the "before" and "after" dump show the ANR dialog; tap_selector
    # targets the "Wait" button by selector (same as any other tap) and must
    # surface the ANR in its result WITHOUT any implicit extra dismiss tap.
    client = FakeAdbClient(dump_xml_queue=[ANR_DUMP_XML, ANR_DUMP_XML])
    result = tap_selector(client, {"text": "Wait"})
    assert result["anr_before"]["detected"] is True
    assert result["anr_after"]["detected"] is True
    # Exactly one tap — the selector's own resolved target, nothing implicit.
    assert client.tap_calls() == [("250", "1150")]


# ---------------------------------------------------------------------------
# tap_xy — always carries a warning
# ---------------------------------------------------------------------------


def test_tap_xy_always_has_warning_and_taps_raw_coords() -> None:
    client = FakeAdbClient()
    result = tap_xy(client, 42, 84)
    assert result["x"] == 42
    assert result["y"] == 84
    assert result.get("warning")
    assert client.tap_calls() == [("42", "84")]


# ---------------------------------------------------------------------------
# type_text — the full verified field-write protocol
# ---------------------------------------------------------------------------


def test_type_text_happy_path_verified() -> None:
    # Same dump used for tap+focus-check+readback: base_url field shows the
    # NEW text so the readback assertion passes, and is focused=true.
    written_dump = SAMPLE_DUMP_XML.replace(
        'text="http://old-server:9000"', 'text="http://new-server:9000"'
    )
    client = FakeAdbClient(
        dump_xml_queue=[SAMPLE_DUMP_XML, SAMPLE_DUMP_XML, written_dump]
    )

    result = type_text(
        client, {"res_id": "com.foo:id/base_url"}, "http://new-server:9000"
    )

    assert result["verified"] is True
    assert result["readback"] == "http://new-server:9000"
    assert result["existing_text_before"] == "http://old-server:9000"

    keyevents = client.keyevent_calls()
    assert keyevents[0] == str(KEYCODE_MOVE_END)
    delete_presses = [k for k in keyevents if k == str(KEYCODE_DEL)]
    expected_deletes = max(len("http://old-server:9000") + 10, MIN_DELETE_PRESSES)
    assert len(delete_presses) == expected_deletes
    assert keyevents[-1] == str(KEYCODE_BACK)

    text_calls = client.text_calls()
    assert len(text_calls) == 1
    assert "new%sserver" not in text_calls[0]  # no literal spaces in the URL to encode
    assert "http://new-server:9000" in text_calls[0]


def test_type_text_errors_when_focus_not_gained() -> None:
    # After tapping, re-dump shows the field still NOT focused.
    unfocused_dump = SAMPLE_DUMP_XML.replace('focused="true"', 'focused="false"')
    client = FakeAdbClient(dump_xml_queue=[SAMPLE_DUMP_XML, unfocused_dump])

    with pytest.raises(UiInteractionError, match="did not gain focus"):
        type_text(client, {"res_id": "com.foo:id/base_url"}, "http://new-server:9000")


def test_type_text_errors_on_readback_mismatch() -> None:
    # Final re-dump still shows the OLD text — the write silently failed.
    client = FakeAdbClient(
        dump_xml_queue=[SAMPLE_DUMP_XML, SAMPLE_DUMP_XML, SAMPLE_DUMP_XML]
    )

    with pytest.raises(UiInteractionError, match="Readback mismatch"):
        type_text(client, {"res_id": "com.foo:id/base_url"}, "http://new-server:9000")


def test_type_text_encodes_spaces_in_input_text() -> None:
    written_dump = SAMPLE_DUMP_XML.replace(
        'text="http://old-server:9000"', 'text="hello world"'
    )
    client = FakeAdbClient(
        dump_xml_queue=[SAMPLE_DUMP_XML, SAMPLE_DUMP_XML, written_dump]
    )
    type_text(client, {"res_id": "com.foo:id/base_url"}, "hello world")
    text_calls = client.text_calls()
    assert "hello%sworld" in text_calls[0]


# ---------------------------------------------------------------------------
# wait_for — the only synchronisation mechanism; polls, no bare sleeps
# ---------------------------------------------------------------------------


def test_wait_for_present_succeeds_immediately() -> None:
    client = FakeAdbClient()
    result = wait_for(client, {"text": "Save"}, timeout_s=1.0, poll_s=0.01)
    assert result["met"] is True
    assert result["match_count"] == 1


def test_wait_for_absent_true_succeeds_when_selector_missing() -> None:
    client = FakeAdbClient()
    result = wait_for(
        client, {"text": "Nonexistent"}, timeout_s=1.0, poll_s=0.01, absent=True
    )
    assert result["met"] is True
    assert result["match_count"] == 0


def test_wait_for_times_out_when_never_satisfied() -> None:
    client = FakeAdbClient()
    result = wait_for(
        client, {"text": "Nonexistent"}, timeout_s=0.05, poll_s=0.02, absent=False
    )
    assert result["met"] is False


# ---------------------------------------------------------------------------
# dismiss_anr
# ---------------------------------------------------------------------------


def test_dismiss_anr_no_dialog_present() -> None:
    client = FakeAdbClient()
    result = dismiss_anr(client)
    assert result == {"detected": False}
    assert client.tap_calls() == []


def test_dismiss_anr_taps_wait_and_reports_dismissed() -> None:
    resolved_dump = SAMPLE_DUMP_XML  # ANR gone after the tap
    client = FakeAdbClient(dump_xml_queue=[ANR_DUMP_XML, resolved_dump])
    result = dismiss_anr(client)
    assert result["detected"] is True
    assert result["dismissed"] is True
    assert client.tap_calls() == [("250", "1150")]


def test_dismiss_anr_still_present_after_tap() -> None:
    client = FakeAdbClient(dump_xml_queue=[ANR_DUMP_XML, ANR_DUMP_XML])
    result = dismiss_anr(client)
    assert result["detected"] is True
    assert result["dismissed"] is False


# ---------------------------------------------------------------------------
# dump_ui_xml -- Defect 1: per-serial serialisation + bounded collision retry
# ---------------------------------------------------------------------------

_COLLISION_RESULT = AdbCommandResult(
    args=[],
    returncode=137,
    stdout="",
    stderr=(
        "java.lang.IllegalStateException: UiAutomationService "
        "0198... already registered!"
    ),
)

_UNRELATED_FAILURE_RESULT = AdbCommandResult(
    args=[], returncode=1, stdout="", stderr="/system/bin/sh: uiautomator: not found"
)


def test_dump_ui_xml_succeeds_immediately_when_no_collision(tmp_path) -> None:
    client = FakeAdbClient()
    xml = dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0)
    assert xml == SAMPLE_DUMP_XML
    dump_shell_calls = [c for c in client.calls if "uiautomator dump" in c[-1]]
    assert len(dump_shell_calls) == 1


def test_dump_ui_xml_retries_on_collision_then_succeeds(tmp_path) -> None:
    # Fails twice with the collision signature, succeeds on the third try --
    # well within the default max_retries=3 budget.
    client = FakeAdbClient(shell_result_queue=[_COLLISION_RESULT, _COLLISION_RESULT])
    xml = dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0, max_retries=3)
    assert xml == SAMPLE_DUMP_XML
    dump_shell_calls = [c for c in client.calls if "uiautomator dump" in c[-1]]
    assert len(dump_shell_calls) == 3  # 2 failures + 1 success


def test_dump_ui_xml_raises_structured_error_when_retries_exhausted(tmp_path) -> None:
    # Every attempt (initial + all retries) hits the collision signature.
    client = FakeAdbClient(
        shell_result_queue=[_COLLISION_RESULT, _COLLISION_RESULT, _COLLISION_RESULT]
    )
    with pytest.raises(UiDumpCollisionError) as excinfo:
        dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0, max_retries=2)

    exc = excinfo.value
    assert exc.extra["serial"] == client.serial
    assert exc.extra["max_retries"] == 2
    assert exc.extra["attempts"] == 3  # initial attempt + 2 retries
    assert "already registered" in exc.extra["last_stderr"]
    # Names the cause and the honest limitation explicitly.
    assert "competing uiautomator instance" in str(exc)
    assert "different process" in str(exc).lower()

    dump_shell_calls = [c for c in client.calls if "uiautomator dump" in c[-1]]
    assert len(dump_shell_calls) == 3  # initial + 2 retries, no 4th attempt


def test_dump_ui_xml_does_not_retry_unrelated_failure(tmp_path) -> None:
    """A general retry here would mask real failures -- only the specific
    collision signature is retried."""
    client = FakeAdbClient(shell_result_queue=[_UNRELATED_FAILURE_RESULT])
    with pytest.raises(AdbError, match="uiautomator: not found"):
        dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0)

    dump_shell_calls = [c for c in client.calls if "uiautomator dump" in c[-1]]
    assert len(dump_shell_calls) == 1  # no retry attempted


def test_dump_ui_xml_serialises_two_concurrent_dumps_for_same_serial(tmp_path) -> None:
    """Proves the lock is actually exercised end-to-end from dump_ui_xml,
    not merely constructed and ignored: two threads dumping the SAME serial
    must never overlap inside the dump."""
    import threading
    import time

    entered: list[str] = []
    barrier = threading.Barrier(2)

    class SlowFakeAdbClient(FakeAdbClient):
        def run(self, *args, timeout=None, check_output=False):
            if args[:2] == ("exec-out", "cat"):
                entered.append("enter")
                time.sleep(0.15)
                entered.append("exit")
            return super().run(*args, timeout=timeout, check_output=check_output)

    client = SlowFakeAdbClient(serial="emulator-5554")

    def worker() -> None:
        barrier.wait()
        dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert entered == ["enter", "exit", "enter", "exit"]


# ---------------------------------------------------------------------------
# dump_ui_xml -- CRITICAL defect: a FAILED dump must never serve a stale
# tree. Verified against a fake that models the actual on-device remote-file
# lifecycle (rm clears it, a dump outcome only writes fresh content when it
# is a genuine uiautomator success), not merely a canned content queue --
# so these tests fail if the fix regresses to "cat whatever is there".
# ---------------------------------------------------------------------------

_STALE_XML = SAMPLE_DUMP_XML.replace(
    'text="http://old-server:9000"', 'text="STALE-FROM-A-PREVIOUS-DUMP"'
)

_IDLE_FAILURE_RESULT = AdbCommandResult(
    args=[], returncode=0, stdout="ERROR: could not get idle state.\n", stderr=""
)


class _RemoteFileFakeAdbClient(FakeAdbClient):
    """Models the REAL on-device remote-file lifecycle at `remote_path`,
    instead of the base FakeAdbClient's canned `exec-out cat` queue (which
    returns content unconditionally, regardless of whether a dump actually
    succeeded -- too permissive to prove the staleness fix).

    - `rm -f <remote_path>` (via `.run()`) clears `remote_file_content`.
    - `uiautomator dump` (via `.shell()`) only sets `remote_file_content`
      to `fresh_dump_xml` when the canned outcome is a genuine success:
      `ok` AND none of the idle-failure markers present -- exactly
      uiautomator's own real-world contract (a collision or an idle
      failure never writes a fresh file).
    - `exec-out cat` (via `.run()`) reflects `remote_file_content` exactly:
      empty/error if absent, the real content if present. No canned queue.
    """

    def __init__(
        self,
        *,
        initial_remote_content: str | None = None,
        dump_outcomes: list[AdbCommandResult] | None = None,
        fresh_dump_xml: str = SAMPLE_DUMP_XML,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.remote_file_content = initial_remote_content
        self._dump_outcomes = list(dump_outcomes or [])
        self.fresh_dump_xml = fresh_dump_xml

    def shell(
        self, command, *, check_output: bool = False, timeout: float | None = None
    ) -> AdbCommandResult:
        text = command if isinstance(command, str) else " ".join(command)
        args = ("shell", text)
        self.calls.append(args)
        if not text.startswith("uiautomator dump"):
            return AdbCommandResult(args=list(args), returncode=0, stdout="", stderr="")

        outcome = (
            self._dump_outcomes.pop(0)
            if self._dump_outcomes
            else AdbCommandResult(args=list(args), returncode=0, stdout="", stderr="")
        )
        combined = f"{outcome.stdout}\n{outcome.stderr}"
        genuinely_succeeded = (
            outcome.ok
            and "could not get idle state" not in combined
            and "ERROR:" not in combined
        )
        if genuinely_succeeded:
            self.remote_file_content = self.fresh_dump_xml
        return outcome

    def run(
        self, *args: str, timeout: float | None = None, check_output: bool = False
    ) -> AdbCommandResult:
        self.calls.append(args)
        if args[:3] == ("shell", "rm", "-f"):
            self.remote_file_content = None
            return AdbCommandResult(args=list(args), returncode=0, stdout="", stderr="")
        if args[:2] == ("exec-out", "cat"):
            if self.remote_file_content is None:
                if check_output:
                    raise AdbError(
                        "adb command failed (1): exec-out cat -- "
                        "No such file or directory"
                    )
                return AdbCommandResult(
                    args=list(args),
                    returncode=1,
                    stdout="",
                    stderr="No such file or directory",
                )
            return AdbCommandResult(
                args=list(args),
                returncode=0,
                stdout=self.remote_file_content,
                stderr="",
            )
        return AdbCommandResult(args=list(args), returncode=0, stdout="", stderr="")


def test_dump_ui_xml_deletes_remote_path_before_first_attempt(tmp_path) -> None:
    """The stale-file fix is unconditional: even on the ordinary happy
    path, `rm -f <remote_path>` is issued before `uiautomator dump`."""
    client = FakeAdbClient()
    dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0)

    rm_calls = [c for c in client.calls if c[:3] == ("shell", "rm", "-f")]
    assert len(rm_calls) == 1

    dump_index = next(
        i for i, c in enumerate(client.calls) if "uiautomator dump" in c[-1]
    )
    rm_index = next(
        i for i, c in enumerate(client.calls) if c[:3] == ("shell", "rm", "-f")
    )
    assert rm_index < dump_index


def test_dump_ui_xml_normal_success_overwrites_a_pre_existing_stale_file(
    tmp_path,
) -> None:
    """A stale file already sitting at remote_path from an earlier,
    unrelated call must not leak into a NEW successful dump's result."""
    client = _RemoteFileFakeAdbClient(
        initial_remote_content=_STALE_XML,
        dump_outcomes=[AdbCommandResult(args=[], returncode=0, stdout="", stderr="")],
        fresh_dump_xml=SAMPLE_DUMP_XML,
    )
    xml = dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0)
    assert xml == SAMPLE_DUMP_XML
    assert "STALE-FROM-A-PREVIOUS-DUMP" not in xml


def test_dump_ui_xml_raises_on_idle_failure_even_with_exit_zero(tmp_path) -> None:
    """The single most important case: `uiautomator dump` prints
    'ERROR: could not get idle state.' but STILL EXITS 0. Must raise
    UiDumpTimeoutError -- never return content, cached or otherwise."""
    client = _RemoteFileFakeAdbClient(
        initial_remote_content=_STALE_XML,
        dump_outcomes=[_IDLE_FAILURE_RESULT],
    )

    with pytest.raises(UiDumpTimeoutError) as excinfo:
        dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0)

    exc = excinfo.value
    assert "idle" in str(exc).lower()
    assert "exit code" in str(exc).lower()
    assert exc.extra["serial"] == client.serial
    assert isinstance(exc.extra["duration_s"], float)
    assert exc.extra["duration_s"] >= 0.0
    assert "could not get idle state" in exc.extra["stdout"]

    # No fallback to the stale tree: `cat` is never even reached, and the
    # stale file was already cleared by the pre-dump `rm -f`.
    cat_calls = [c for c in client.calls if c[:2] == ("exec-out", "cat")]
    assert cat_calls == []
    assert client.remote_file_content is None


def test_dump_ui_xml_idle_failure_is_not_retried(tmp_path) -> None:
    """Distinct from the collision signature: the idle failure is NEVER
    folded into the bounded collision-retry loop -- it raises on the very
    first occurrence, even though max_retries > 0."""
    client = _RemoteFileFakeAdbClient(dump_outcomes=[_IDLE_FAILURE_RESULT])

    with pytest.raises(UiDumpTimeoutError):
        dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0, max_retries=3)

    dump_shell_calls = [c for c in client.calls if "uiautomator dump" in c[-1]]
    assert len(dump_shell_calls) == 1  # no retry attempted


def test_dump_ui_xml_never_serves_stale_file_after_unrelated_failure(tmp_path) -> None:
    """A stale file present from a prior dump must never be returned after
    a FAILED (non-collision, non-idle) dump in the current call."""
    client = _RemoteFileFakeAdbClient(
        initial_remote_content=_STALE_XML,
        dump_outcomes=[_UNRELATED_FAILURE_RESULT],
    )

    with pytest.raises(AdbError, match="uiautomator: not found"):
        dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0)

    cat_calls = [c for c in client.calls if c[:2] == ("exec-out", "cat")]
    assert cat_calls == []
    assert client.remote_file_content is None


def test_dump_ui_xml_never_serves_stale_file_after_collision_exhausted(
    tmp_path,
) -> None:
    """Same guarantee through the OTHER failure path: collision retries
    exhausted must not leave a stale tree servable either."""
    client = _RemoteFileFakeAdbClient(
        initial_remote_content=_STALE_XML,
        dump_outcomes=[_COLLISION_RESULT, _COLLISION_RESULT, _COLLISION_RESULT],
    )

    with pytest.raises(UiDumpCollisionError):
        dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0, max_retries=2)

    cat_calls = [c for c in client.calls if c[:2] == ("exec-out", "cat")]
    assert cat_calls == []
    assert client.remote_file_content is None


def test_dump_ui_xml_collision_retry_path_unaffected_by_the_fix(tmp_path) -> None:
    """The existing collision-retry behaviour (Defect 1) is unchanged by
    the staleness fix: fails twice with the collision signature, succeeds
    on the third try, and returns the FRESH content -- not the stale one
    that was sitting there beforehand."""
    client = _RemoteFileFakeAdbClient(
        initial_remote_content=_STALE_XML,
        dump_outcomes=[
            _COLLISION_RESULT,
            _COLLISION_RESULT,
            AdbCommandResult(args=[], returncode=0, stdout="", stderr=""),
        ],
        fresh_dump_xml=SAMPLE_DUMP_XML,
    )
    xml = dump_ui_xml(client, lock_dir=tmp_path, retry_backoff_s=0.0, max_retries=3)
    assert xml == SAMPLE_DUMP_XML
    assert "STALE-FROM-A-PREVIOUS-DUMP" not in xml
    dump_shell_calls = [c for c in client.calls if "uiautomator dump" in c[-1]]
    assert len(dump_shell_calls) == 3  # 2 failures + 1 success
