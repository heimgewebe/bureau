import sys
from pathlib import Path

from bureau.runtime_refresh import RUNTIME_LAUNCHER_ENTRYPOINTS, stable_launcher_bytes


def test_launchers_isolate_python_with_or_without_bureau_venv(tmp_path: Path) -> None:
    manifest_path = tmp_path / "bureau" / "deployment-manifest.json"
    venv_python = manifest_path.parent / "venv" / "bin" / "python"
    for name, entrypoint in RUNTIME_LAUNCHER_ENTRYPOINTS:
        launcher = stable_launcher_bytes(manifest_path, entrypoint).decode("utf-8")
        assert launcher.splitlines()[0] == f"#!{sys.executable} -I", name
        assert "# managed-by: heimgewebe-bureau-runtime-v1" in launcher
        assert f"importlib.import_module({entrypoint!r})" in launcher
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("stub", encoding="utf-8")
    for name, entrypoint in RUNTIME_LAUNCHER_ENTRYPOINTS:
        launcher = stable_launcher_bytes(manifest_path, entrypoint).decode("utf-8")
        assert launcher.splitlines()[0] == f"#!{venv_python} -I", name
