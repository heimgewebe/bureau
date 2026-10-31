import shlex
import subprocess
import sys
from pathlib import Path

from bureau.runtime_refresh import RUNTIME_LAUNCHER_ENTRYPOINTS, stable_launcher_bytes


def test_launchers_isolate_python_with_or_without_bureau_venv(tmp_path: Path) -> None:
    manifest_path = tmp_path / "bureau prefix with spaces and 'quotes'" / "deployment-manifest.json"
    venv_python = manifest_path.parent / "venv" / "bin" / "python"
    fallback_shebang = f"#!/usr/bin/env -S {shlex.quote(sys.executable)} -I"
    for name, entrypoint in RUNTIME_LAUNCHER_ENTRYPOINTS:
        launcher = stable_launcher_bytes(manifest_path, entrypoint).decode("utf-8")
        assert launcher.splitlines()[0] == fallback_shebang, name
        assert "# managed-by: heimgewebe-bureau-runtime-v1" in launcher
        assert f"importlib.import_module({entrypoint!r})" in launcher
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(sys.executable)
    expected_shebang = f"#!/usr/bin/env -S {shlex.quote(str(venv_python))} -I"
    for name, entrypoint in RUNTIME_LAUNCHER_ENTRYPOINTS:
        launcher = stable_launcher_bytes(manifest_path, entrypoint).decode("utf-8")
        assert launcher.splitlines()[0] == expected_shebang, name

    executable = tmp_path / "launcher-shebang-execution-check"
    executable.write_text(expected_shebang + "\nprint('BUREAU_SHEBANG_OK')\n", encoding="utf-8")
    executable.chmod(0o755)
    completed = subprocess.run([str(executable)], capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == "BUREAU_SHEBANG_OK"
