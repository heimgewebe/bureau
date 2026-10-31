from pathlib import Path
from types import SimpleNamespace

from bureau import cli, grabowski_adapter


def test_active_nix_python_comes_from_systemd_executable(monkeypatch):
    native = Path(
        "/nix/store/" + "a" * 32 + "-grabowski-operator-02c70ad4/bin/python3"
    )
    operator = native.with_name("grabowski-operator")

    def fake_run(argv, **kwargs):
        assert argv[:2] == ["systemctl", "show"]
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "ActiveState=active\n"
                f"ExecStart={{ path={operator} ; argv[]={operator} ; }}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(grabowski_adapter.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "is_file", lambda path: path == native)
    assert grabowski_adapter._active_grabowski_python() == native


def test_inactive_operator_does_not_offer_a_false_runtime(monkeypatch):
    monkeypatch.setattr(
        grabowski_adapter.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="ActiveState=inactive\n", stderr=""
        ),
    )
    assert grabowski_adapter._active_grabowski_python() is None


def test_live_bridge_preserves_task_api_and_keeps_argv_off_command_line(monkeypatch):
    native = Path("/nix/store/known-operator/bin/python3")
    legacy_root = Path("/temporary/legacy-grabowski-python310")
    requests = []

    def fake_run(argv, **kwargs):
        assert argv[:3] == [str(native), "-I", "-B"]
        assert kwargs["timeout"] == 45
        import json

        payload = json.loads(kwargs["input"])
        requests.append({"argv": argv, "payload": payload})
        method = payload["method"]
        responses = {
            "_probe": {"python": [3, 12]},
            "grabowski_task_start": {"task": {"task_id": "started-task"}},
            "grabowski_task_status": {
                "task_id": "started-task",
                "state": "completed",
            },
            "grabowski_task_cancel": {"state": "cancelled"},
            "grabowski_task_resume": {"state": "running"},
            "grabowski_task_find_by_request_id": None,
        }
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"result": responses[method]}),
            stderr="",
        )

    monkeypatch.setattr(grabowski_adapter, "_active_grabowski_python", lambda: native)
    monkeypatch.setattr(grabowski_adapter.subprocess, "run", fake_run)
    adapter = grabowski_adapter.GrabowskiTaskAdapter(
        legacy_root, prefer_live_operator=True
    )
    assert adapter.dispatch({
        "host": "heim-pc", "argv": ["echo", "secret-example-value"]
    }) == "started-task"
    observed = adapter.observe("started-task")
    assert observed.state == "succeeded"
    assert observed.detail["task_id"] == "started-task"
    assert adapter.recover("not-present") is None
    assert adapter.cancel("started-task")["state"] == "cancelled"
    assert adapter.resume("started-task")["state"] == "running"
    assert [x["payload"]["method"] for x in requests] == [
        "_probe",
        "grabowski_task_start",
        "grabowski_task_status",
        "grabowski_task_find_by_request_id",
        "grabowski_task_cancel",
        "grabowski_task_resume",
    ]
    for call in requests:
        assert "secret-example-value" not in " ".join(call["argv"])


def test_timeout_never_implicitly_retries_task_dispatch(monkeypatch):
    import json
    import subprocess

    calls = []

    def fake_run(argv, **kwargs):
        payload = json.loads(kwargs["input"])
        calls.append(payload["method"])
        if payload["method"] == "_probe":
            return SimpleNamespace(
                returncode=0,
                stdout='{"result":{"python":[3,12]}}',
                stderr="",
            )
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(grabowski_adapter.subprocess, "run", fake_run)
    bridge = grabowski_adapter._NixTasksBridge(Path("/nix/store/known/bin/python3"))
    try:
        bridge.grabowski_task_start(host="heim-pc", argv=["true"])
    except RuntimeError as exc:
        assert "outcome unknown" in str(exc)
    else:
        raise AssertionError("task dispatch timeout was incorrectly accepted")
    assert calls == ["_probe", "grabowski_task_start"]


def test_cli_keeps_explicit_source_override(monkeypatch, tmp_path):
    calls = []

    class Adapter:
        system = "grabowski-task"
        aliases = ("grabowski-job",)

        def __init__(self, path, **kwargs):
            calls.append((path, kwargs))

    monkeypatch.setattr(grabowski_adapter, "GrabowskiTaskAdapter", Adapter)
    source = tmp_path / "explicit-grabowski-src"
    assert cli.adapters(SimpleNamespace(grabowski_source=str(source))).status()[
        "grabowski-task"
    ] == {"available": True}
    assert calls == [(source, {})]
    monkeypatch.setattr(cli, "default_grabowski_source", lambda: source)
    assert cli.adapters(SimpleNamespace(grabowski_source=None)).status()[
        "grabowski-task"
    ] == {"available": True}
    assert calls[-1] == (source, {"prefer_live_operator": True})
