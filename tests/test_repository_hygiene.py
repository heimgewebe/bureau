from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_repo_local_worktrees_are_ignored() -> None:
    patterns = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".worktrees/" in patterns


def test_uv_does_not_manage_bureau_project() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    uv_section = pyproject.split("[tool.uv]", 1)[1].split("[", 1)[0]

    assert "managed = false" in uv_section
