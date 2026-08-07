"""Unit tests for leases.py -- the host-global, cross-process AVD lease and
port-allocation module (Defects 1, 2, 3). No real emulator/adb dependency;
"another process" is simulated either via a monkeypatched `is_pid_alive`
(deterministic, for the common "someone else holds it" case) or via a real,
already-exited subprocess (for the "owner pid is genuinely dead" case)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import amplifier_module_tool_android_inspector.leases as leases_mod
from amplifier_module_tool_android_inspector.leases import (
    DEFAULT_LEASE_DIR,
    AvdLease,
    AvdLeaseError,
    acquire_avd_lease,
    describe_lease,
    find_lease_by_serial,
    is_pid_alive,
    list_live_leases,
    parse_port_from_serial,
    pick_free_port,
    read_lease,
    record_lease_serial,
    release_avd_lease,
    sanitize_name_for_filename,
)

# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def test_default_lease_dir_is_host_global_fixed_path() -> None:
    """Defect 1/2: the lease directory must be a FIXED path, never derived
    from a per-session work_dir -- otherwise unrelated sessions with
    different work_dirs would never see each other's leases."""
    assert (
        DEFAULT_LEASE_DIR == Path("~/.amplifier/android-sessions/leases").expanduser()
    )


def test_sanitize_name_for_filename_replaces_unsafe_chars() -> None:
    assert sanitize_name_for_filename("my avd/weird:name") == "my_avd_weird_name"


def test_sanitize_name_for_filename_empty_falls_back() -> None:
    assert sanitize_name_for_filename("") == "unknown-avd"
    assert sanitize_name_for_filename("   ") == "unknown-avd"


def test_parse_port_from_serial_valid() -> None:
    assert parse_port_from_serial("emulator-5554") == 5554


def test_parse_port_from_serial_non_emulator_serial() -> None:
    assert parse_port_from_serial("R58N90ABCDE") is None


def test_parse_port_from_serial_malformed() -> None:
    assert parse_port_from_serial("emulator-abc") is None
    assert parse_port_from_serial("emulator-") is None


def test_is_pid_alive_true_for_self() -> None:
    assert is_pid_alive(os.getpid()) is True


def test_is_pid_alive_false_for_a_real_exited_process() -> None:
    """Real (not mocked) proof that a genuinely dead pid is detected as
    dead -- via an actual short-lived subprocess rather than guessing a
    number that happens not to be a live pid on this host."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    assert is_pid_alive(proc.pid) is False


# ---------------------------------------------------------------------------
# pick_free_port -- pure, no I/O
# ---------------------------------------------------------------------------


def test_pick_free_port_returns_lowest_free() -> None:
    assert pick_free_port(port_min=5554, port_max=5560, in_use=set()) == 5554


def test_pick_free_port_skips_in_use_ports() -> None:
    assert pick_free_port(port_min=5554, port_max=5560, in_use={5554, 5556}) == 5558


def test_pick_free_port_returns_none_when_exhausted() -> None:
    in_use = {5554, 5556, 5558, 5560}
    assert pick_free_port(port_min=5554, port_max=5560, in_use=in_use) is None


def test_pick_free_port_only_considers_even_ports() -> None:
    # 5555 (odd) being "in_use" must not affect anything -- the range only
    # ever steps over even ports in the first place.
    assert pick_free_port(port_min=5554, port_max=5556, in_use={5555}) == 5554


# ---------------------------------------------------------------------------
# acquire_avd_lease -- acquire / refuse / stale-reclaim
# ---------------------------------------------------------------------------


def test_acquire_avd_lease_records_full_metadata(tmp_path: Path) -> None:
    lease = acquire_avd_lease(
        "my-avd", port=5570, serial="emulator-5570", lease_dir=tmp_path
    )
    assert lease.avd == "my-avd"
    assert lease.pid == os.getpid()
    assert lease.port == 5570
    assert lease.serial == "emulator-5570"
    assert lease.started_at > 0
    assert lease.port_allocation_exhausted is False

    on_disk = read_lease("my-avd", tmp_path)
    assert on_disk == lease


def test_acquire_avd_lease_creates_lease_dir_if_missing(tmp_path: Path) -> None:
    lease_dir = tmp_path / "does" / "not" / "exist" / "yet"
    assert not lease_dir.exists()
    acquire_avd_lease("my-avd", port=5554, lease_dir=lease_dir)
    assert lease_dir.exists()
    assert (lease_dir / "my-avd.lease").exists()


def test_acquire_avd_lease_same_pid_can_reacquire_its_own_lease(tmp_path: Path) -> None:
    """Re-running start_emulator under the SAME process for the SAME avd
    (e.g. a retry) must not be treated as a foreign collision."""
    first = acquire_avd_lease("my-avd", port=5554, lease_dir=tmp_path)
    second = acquire_avd_lease("my-avd", port=5570, lease_dir=tmp_path)
    assert first.pid == second.pid == os.getpid()
    assert second.port == 5570  # overwritten, not refused


def test_acquire_avd_lease_refuses_when_live_owner_holds_it(
    tmp_path: Path, monkeypatch
) -> None:
    """Defect 1: a DIFFERENT, currently-live pid already holds the AVD --
    refuse immediately and loudly, naming the owning pid/port/duration."""
    other = AvdLease(
        avd="concur-probe",
        pid=999999,
        port=5554,
        serial="emulator-5554",
        started_at=time.time() - 12.5,
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "concur-probe.lease").write_text(
        json.dumps(other.to_dict()), encoding="utf-8"
    )
    monkeypatch.setattr(leases_mod, "is_pid_alive", lambda pid: pid == 999999)

    try:
        acquire_avd_lease("concur-probe", port=5570, lease_dir=lease_dir)
        raise AssertionError("expected AvdLeaseError")
    except AvdLeaseError as exc:
        assert exc.extra["owner_pid"] == 999999
        assert exc.extra["avd"] == "concur-probe"
        assert exc.extra["port"] == 5554
        assert exc.extra["suggested_operation"] == "create_avd"
        assert "999999" in str(exc)
        assert "concur-probe" in str(exc)


def test_acquire_avd_lease_reclaims_stale_lease_from_dead_pid(tmp_path: Path) -> None:
    """Defect 1: a lease recorded by a now-dead pid is stale -- reclaim it
    rather than refusing/deadlocking forever. Uses a REAL exited process
    (not a mocked is_pid_alive) as the dead-owner-pid case."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    stale = AvdLease(
        avd="my-avd",
        pid=dead_pid,
        port=5554,
        serial="emulator-5554",
        started_at=time.time() - 500,
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "my-avd.lease").write_text(
        json.dumps(stale.to_dict()), encoding="utf-8"
    )

    reclaimed = acquire_avd_lease("my-avd", port=5570, lease_dir=lease_dir)
    assert reclaimed.pid == os.getpid()
    assert reclaimed.port == 5570

    on_disk = read_lease("my-avd", lease_dir)
    assert on_disk is not None
    assert on_disk.pid == os.getpid()


def test_acquire_avd_lease_reclaim_is_safe_against_concurrent_racers(
    tmp_path: Path,
) -> None:
    """Safe against races (re-check under the lock): many threads racing to
    acquire the SAME avd's lease at once must never corrupt the on-disk
    metadata or leave it in an inconsistent state -- the flock genuinely
    serialises the read-decide-write critical section."""
    lease_dir = tmp_path / "leases"
    errors: list[Exception] = []
    results: list[AvdLease] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            lease = acquire_avd_lease("shared-avd", port=5554 + i, lease_dir=lease_dir)
            with lock:
                results.append(lease)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # All racers share this test process's pid -- none should be refused.
    assert not errors
    assert len(results) == 10

    final = read_lease("shared-avd", lease_dir)
    assert final is not None
    assert final.pid == os.getpid()
    # The final port on disk must be exactly one of the attempted values --
    # proof the write was never interleaved/corrupted.
    assert final.port in {5554 + i for i in range(10)}


def test_acquire_avd_lease_refuses_when_orphaned_dead_pid_but_device_alive(
    tmp_path,
) -> None:
    """Owner-pid-dead is NOT the same as stale: an emulator commonly
    outlives the session that started it. When a `device_liveness_probe`
    confirms the recorded serial is still alive, this must refuse
    (Orphaned) exactly as loudly as a live owner would -- never silently
    reclaim just because the recorded pid happens to be dead."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    orphaned = AvdLease(
        avd="concur-probe",
        pid=dead_pid,
        port=5560,
        serial="emulator-5560",
        started_at=time.time() - 500,
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "concur-probe.lease").write_text(
        json.dumps(orphaned.to_dict()), encoding="utf-8"
    )

    try:
        acquire_avd_lease(
            "concur-probe",
            port=5570,
            lease_dir=lease_dir,
            device_liveness_probe=lambda serial: serial == "emulator-5560",
        )
        raise AssertionError("expected AvdLeaseError")
    except AvdLeaseError as exc:
        assert exc.extra["orphaned"] is True
        assert exc.extra["owner_pid"] == dead_pid
        assert exc.extra["serial"] == "emulator-5560"
        assert "emulator-5560" in str(exc)
        assert str(dead_pid) in str(exc)

    # Refused -- lease is untouched, not reclaimed.
    on_disk = read_lease("concur-probe", lease_dir)
    assert on_disk is not None
    assert on_disk.pid == dead_pid


def test_acquire_avd_lease_reclaims_when_dead_pid_and_device_also_gone(
    tmp_path,
) -> None:
    """Owner pid dead AND the probe confirms the device is ALSO gone --
    genuinely Stale. Reclaimed exactly like the pid-only stale case."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    stale = AvdLease(
        avd="my-avd",
        pid=dead_pid,
        port=5554,
        serial="emulator-5554",
        started_at=time.time() - 500,
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "my-avd.lease").write_text(
        json.dumps(stale.to_dict()), encoding="utf-8"
    )

    reclaimed = acquire_avd_lease(
        "my-avd",
        port=5570,
        lease_dir=lease_dir,
        device_liveness_probe=lambda serial: False,  # confirmed gone
    )
    assert reclaimed.pid == os.getpid()
    assert reclaimed.port == 5570

    on_disk = read_lease("my-avd", lease_dir)
    assert on_disk is not None
    assert on_disk.pid == os.getpid()


def test_acquire_avd_lease_no_probe_given_still_reclaims_dead_pid(
    tmp_path,
) -> None:
    """Backward compatibility: when no `device_liveness_probe` is supplied
    at all, a dead owner pid alone is treated as stale -- exactly the
    pre-existing behaviour. The probe is purely additive, never required."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    stale = AvdLease(
        avd="my-avd",
        pid=dead_pid,
        port=5554,
        serial="emulator-5554",
        started_at=time.time() - 500,
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "my-avd.lease").write_text(
        json.dumps(stale.to_dict()), encoding="utf-8"
    )

    reclaimed = acquire_avd_lease("my-avd", port=5570, lease_dir=lease_dir)
    assert reclaimed.pid == os.getpid()


def test_acquire_avd_lease_orphan_check_skipped_when_no_serial_recorded(
    tmp_path,
) -> None:
    """A lease with a dead owner pid but no recorded serial at all (e.g. a
    prior attempt crashed before the serial was ever discovered) cannot be
    checked for device liveness by serial -- treated as stale, same as a
    probe that isn't given."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    stale = AvdLease(
        avd="my-avd", pid=dead_pid, port=5554, serial=None, started_at=time.time()
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "my-avd.lease").write_text(
        json.dumps(stale.to_dict()), encoding="utf-8"
    )

    probe_calls: list[str] = []

    def probe(serial: str) -> bool:
        probe_calls.append(serial)
        return True

    reclaimed = acquire_avd_lease(
        "my-avd", port=5570, lease_dir=lease_dir, device_liveness_probe=probe
    )
    assert reclaimed.pid == os.getpid()
    assert probe_calls == []  # never consulted -- no serial to check


# ---------------------------------------------------------------------------
# acquire_avd_lease -- port allocation (Defect 2)
# ---------------------------------------------------------------------------


def test_acquire_avd_lease_allocates_free_port_when_none_given(tmp_path: Path) -> None:
    lease = acquire_avd_lease(
        "my-avd",
        port=None,
        lease_dir=tmp_path,
        allocate_port_range=(5554, 5560),
        attached_ports=frozenset(),
    )
    assert lease.port == 5554
    assert lease.port_allocation_exhausted is False


def test_acquire_avd_lease_allocation_skips_attached_ports(tmp_path: Path) -> None:
    lease = acquire_avd_lease(
        "my-avd",
        port=None,
        lease_dir=tmp_path,
        allocate_port_range=(5554, 5560),
        attached_ports=frozenset({5554, 5556}),
    )
    assert lease.port == 5558


def test_acquire_avd_lease_allocation_skips_ports_leased_by_other_live_avds(
    tmp_path: Path,
) -> None:
    """The measured scenario: two DIFFERENT avds booting concurrently with
    no explicit port must not collide -- a port already claimed on another
    AVD's live lease must be skipped."""
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    other = AvdLease(
        avd="other-avd",
        pid=os.getpid(),  # "live" (our own pid) so it isn't reclaimed
        port=5554,
        serial="emulator-5554",
        started_at=time.time(),
    )
    (lease_dir / "other-avd.lease").write_text(
        json.dumps(other.to_dict()), encoding="utf-8"
    )

    lease = acquire_avd_lease(
        "my-avd",
        port=None,
        lease_dir=lease_dir,
        allocate_port_range=(5554, 5560),
        attached_ports=frozenset({5556}),
    )
    assert lease.port == 5558  # 5554 leased elsewhere, 5556 attached


def test_acquire_avd_lease_allocation_ignores_ports_from_dead_owned_leases(
    tmp_path: Path,
) -> None:
    """A port recorded on a STALE (dead-pid) lease must not be treated as
    in-use -- nobody is actually using it."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    stale = AvdLease(
        avd="other-avd", pid=proc.pid, port=5554, serial=None, started_at=time.time()
    )
    (lease_dir / "other-avd.lease").write_text(
        json.dumps(stale.to_dict()), encoding="utf-8"
    )

    lease = acquire_avd_lease(
        "my-avd",
        port=None,
        lease_dir=lease_dir,
        allocate_port_range=(5554, 5560),
        attached_ports=frozenset(),
    )
    assert lease.port == 5554  # the "leased" port was actually free


def test_acquire_avd_lease_reports_exhaustion_when_no_free_port(tmp_path: Path) -> None:
    lease = acquire_avd_lease(
        "my-avd",
        port=None,
        lease_dir=tmp_path,
        allocate_port_range=(5554, 5556),
        attached_ports=frozenset({5554, 5556}),
    )
    assert lease.port is None
    assert lease.port_allocation_exhausted is True


def test_acquire_avd_lease_explicit_port_skips_allocation_entirely(
    tmp_path: Path,
) -> None:
    """When `port` is given, allocate_port_range must be ignored even if a
    caller mistakenly passes both -- explicit intent always wins."""
    lease = acquire_avd_lease(
        "my-avd",
        port=5600,
        lease_dir=tmp_path,
        allocate_port_range=(5554, 5560),  # would be exhausted if consulted
        attached_ports=frozenset({5554, 5556, 5558, 5560}),
    )
    assert lease.port == 5600
    assert lease.port_allocation_exhausted is False


# ---------------------------------------------------------------------------
# record_lease_serial
# ---------------------------------------------------------------------------


def test_record_lease_serial_updates_existing_lease(tmp_path: Path) -> None:
    acquire_avd_lease("my-avd", port=5554, lease_dir=tmp_path)
    record_lease_serial("my-avd", "emulator-5554", lease_dir=tmp_path)
    lease = read_lease("my-avd", tmp_path)
    assert lease is not None
    assert lease.serial == "emulator-5554"


def test_record_lease_serial_is_noop_when_lease_absent(tmp_path: Path) -> None:
    # Must not raise even though nothing was ever acquired.
    record_lease_serial("never-acquired", "emulator-5554", lease_dir=tmp_path)
    assert read_lease("never-acquired", tmp_path) is None


# ---------------------------------------------------------------------------
# release_avd_lease
# ---------------------------------------------------------------------------


def test_release_avd_lease_clears_owned_lease(tmp_path: Path) -> None:
    acquire_avd_lease("my-avd", port=5554, lease_dir=tmp_path)
    assert release_avd_lease("my-avd", expected_pid=os.getpid(), lease_dir=tmp_path)
    assert read_lease("my-avd", tmp_path) is None


def test_release_avd_lease_is_idempotent_when_absent(tmp_path: Path) -> None:
    assert release_avd_lease("never-acquired", lease_dir=tmp_path) is True


def test_release_avd_lease_refuses_to_clear_a_different_live_owner(
    tmp_path: Path, monkeypatch
) -> None:
    other = AvdLease(
        avd="my-avd", pid=424242, port=5554, serial=None, started_at=time.time()
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "my-avd.lease").write_text(
        json.dumps(other.to_dict()), encoding="utf-8"
    )
    monkeypatch.setattr(leases_mod, "is_pid_alive", lambda pid: pid == 424242)

    assert (
        release_avd_lease("my-avd", expected_pid=os.getpid(), lease_dir=lease_dir)
        is False
    )
    # Untouched -- the other process's lease is still intact.
    still_there = read_lease("my-avd", lease_dir)
    assert still_there is not None
    assert still_there.pid == 424242


def test_release_avd_lease_allows_clearing_a_stale_dead_owner(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    stale = AvdLease(
        avd="my-avd", pid=proc.pid, port=5554, serial=None, started_at=time.time()
    )
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "my-avd.lease").write_text(
        json.dumps(stale.to_dict()), encoding="utf-8"
    )

    assert (
        release_avd_lease("my-avd", expected_pid=os.getpid(), lease_dir=lease_dir)
        is True
    )
    assert read_lease("my-avd", lease_dir) is None


# ---------------------------------------------------------------------------
# find_lease_by_serial / list_live_leases
# ---------------------------------------------------------------------------


def test_find_lease_by_serial_finds_matching_lease(tmp_path: Path) -> None:
    acquire_avd_lease("my-avd", port=5554, serial="emulator-5554", lease_dir=tmp_path)
    found = find_lease_by_serial("emulator-5554", lease_dir=tmp_path)
    assert found is not None
    assert found.avd == "my-avd"


def test_find_lease_by_serial_none_when_no_match(tmp_path: Path) -> None:
    acquire_avd_lease("my-avd", port=5554, serial="emulator-5554", lease_dir=tmp_path)
    assert find_lease_by_serial("emulator-9999", lease_dir=tmp_path) is None


def test_find_lease_by_serial_none_when_dir_missing(tmp_path: Path) -> None:
    assert find_lease_by_serial("emulator-5554", lease_dir=tmp_path / "nope") is None


def test_list_live_leases_excludes_dead_owners(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    dead = AvdLease(
        avd="dead-avd", pid=proc.pid, port=5554, serial=None, started_at=time.time()
    )
    (lease_dir / "dead-avd.lease").write_text(
        json.dumps(dead.to_dict()), encoding="utf-8"
    )
    acquire_avd_lease("live-avd", port=5570, lease_dir=lease_dir)

    live = list_live_leases(lease_dir)
    assert {lease.avd for lease in live} == {"live-avd"}


def test_list_live_leases_skip_avd_excludes_that_one(tmp_path: Path) -> None:
    acquire_avd_lease("avd-a", port=5554, lease_dir=tmp_path)
    acquire_avd_lease("avd-b", port=5556, lease_dir=tmp_path)
    live = list_live_leases(tmp_path, skip_avd="avd-a")
    assert {lease.avd for lease in live} == {"avd-b"}


# ---------------------------------------------------------------------------
# describe_lease
# ---------------------------------------------------------------------------


def test_describe_lease_includes_pid_port_serial() -> None:
    lease = AvdLease(
        avd="my-avd",
        pid=1234,
        port=5554,
        serial="emulator-5554",
        started_at=time.time() - 10,
    )
    description = describe_lease(lease)
    assert "1234" in description
    assert "5554" in description
    assert "emulator-5554" in description
