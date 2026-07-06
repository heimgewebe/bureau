from __future__ import annotations

from pathlib import Path

from bureau.core import Registry


def test_repository_registry_loads_current_checkout():
    root = Path(__file__).resolve().parents[1]
    registry = Registry.load(root)
    assert registry.tasks
