"""Shared fixtures for unit tests.

Clears cached 'handler' and 'shared' modules between test files
to prevent cross-contamination when multiple Lambda handlers share
the same module name.
"""
import sys


def pytest_collectstart(collector):
    """Remove cached handler modules before each test file is collected."""
    mods_to_remove = [key for key in sys.modules if key in ("handler", "shared") or key.startswith("shared.")]
    for mod in mods_to_remove:
        del sys.modules[mod]
