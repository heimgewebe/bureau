from pathlib import Path

from bureau.runtime_refresh import RUNTIME_LAUNCHER_ENTRYPOINTS, stable_launcher_bytes


def test_launchers_use_bureau_venv_with_python_isolation(tmp_path: Path) -> None:
    manifest_path = tmp_path / "bureau" / "deployment-manifest.json"
    expected_shebang = f"#!{manifest_path.parent / 'venv' / 'bin' / 'python'} -I"
    for name, entrypoint in RUNTIME_LAUNCHER_ENTRYPOINTS:
        launcher = stable_launcher_bytes(manifest_path, entrypoint).decode("utf-8")
        assert launcher.splitlines()[0] == expected_shebang, name
        assert "# managed-by: heimgewebe-bureau-runtime-v1" in launcher
        assert f"importlib.import_module({entrypoint!r})" in launcher
