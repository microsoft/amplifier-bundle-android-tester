"""Scratch-only: one deliberately failing test so the Tests job is observed red."""


def test_ci_can_go_red():
    assert 1 == 2
