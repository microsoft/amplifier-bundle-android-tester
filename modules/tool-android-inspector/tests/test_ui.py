"""Unit tests for ui.py — dump parsing, selector matching, bounds/center
parsing, ANR detection, and the verified interaction protocols (tap,
type_text, wait_for, dismiss_anr) against a FakeAdbClient double. No
emulator/device dependency."""

from __future__ import annotations

import pytest
from amplifier_module_tool_android_inspector.ui import (
    KEYCODE_BACK,
    KEYCODE_DEL,
    KEYCODE_MOVE_END,
    MIN_DELETE_PRESSES,
    SelectorError,
    UiDumpError,
    UiInteractionError,
    center_of,
    dismiss_anr,
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
