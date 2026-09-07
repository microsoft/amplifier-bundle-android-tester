"""Scratch-only: deliberate F821 so the Lint job is observed red."""


def broken():
    return deliberately_undefined_name
