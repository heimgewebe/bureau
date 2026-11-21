from __future__ import annotations

import json
import subprocess

import pytest

from bureau.runtime_refresh import bind_digest
from bureau_cycle import operator_runner as operator


@pytest.mark.parametrize(
    ('status', 'result', 'degraded'),
    [('already_current', 'idle', False), ('candidate', 'partial', False),
     ('alert', 'partial', True), ('blocked', 'partial', True)],
)
def test_observation_is_read_only_and_visible(monkeypatch, tmp_path, status, result, degraded):
    monkeypatch.setattr(operator, 'STATE', tmp_path)
    observation = bind_digest({
        'kind': 'bureau_runtime_refresh_observation', 'status': status,
        'main_commit': 'a' * 40, 'deployed_source_commit': 'b' * 40,
        'lag_commits': 1, 'age_seconds': 6000,
    }, 'observation_sha256')
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            **observation, 'observation_path': str(tmp_path / 'observation.json'),
        }))

    monkeypatch.setattr(operator.subprocess, 'run', run)
    receipt = operator.terminal_receipt('stamp', 'run', operator.utc_now())
    operator.observe_runtime(receipt)
    assert calls == [([
        str(operator.REFRESH_LAUNCHER), '--state-root', str(tmp_path / 'runtime-refresh'),
        'observe',
    ], {'capture_output': True, 'text': True, 'check': True, 'timeout': 60})]
    assert receipt['result'] == result
    assert receipt['degraded'] is degraded
    assert receipt['evidence'] == [{'kind': 'runtime_refresh', 'value': observation}]
    assert receipt['queue_changes'] == []
    assert receipt['selected_task_id'] is None


@pytest.mark.parametrize('failure', [
    FileNotFoundError('missing launcher'),
    subprocess.TimeoutExpired('observe', 60),
    subprocess.CalledProcessError(2, 'observe', stderr='private detail'),
    'not json', '[]', '{"status":"already_current"}',
    json.dumps(bind_digest({'kind': 'wrong', 'status': 'already_current'}, 'observation_sha256')),
    json.dumps(bind_digest({
        'kind': 'bureau_runtime_refresh_observation', 'status': [],
    }, 'observation_sha256')),
])
def test_failed_observation_cannot_be_current(monkeypatch, failure):
    def run(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return subprocess.CompletedProcess([], 0, failure)

    monkeypatch.setattr(operator.subprocess, 'run', run)
    receipt = operator.terminal_receipt('stamp', 'run', operator.utc_now())
    operator.observe_runtime(receipt)
    assert receipt['result'] == 'partial'
    assert receipt['degraded'] is True
    assert receipt['actions'][-1]['decision'] == 'read_only_unavailable'
    assert 'private detail' not in json.dumps(receipt)


def test_health_block_survives_observation_failure(monkeypatch):
    def run(*args, **kwargs):
        raise OSError('unavailable')

    monkeypatch.setattr(operator.subprocess, 'run', run)
    receipt = operator.terminal_receipt('stamp', 'run', operator.utc_now())
    receipt.update(result='blocked', next_action='repair health')
    receipt['circuit_breaker']['active'] = True
    operator.observe_runtime(receipt)
    assert receipt['result'] == 'blocked'
    assert receipt['next_action'] == 'repair health'
    assert receipt['circuit_breaker']['active'] is True


def test_existing_cycle_publishes_observation_under_operator_lock(monkeypatch, tmp_path):
    import fcntl

    for name, path in {
        'STATE': tmp_path, 'RUNS': tmp_path / 'runs', 'LOCKS': tmp_path / 'locks',
        'LOCK': tmp_path / 'locks/operator.lock', 'CURATOR_STATE': tmp_path / 'curator',
        'HEALTH_PATH': tmp_path / 'health.json',
    }.items():
        monkeypatch.setattr(operator, name, path)
    monkeypatch.setattr(operator, 'classify_task_attention', lambda *a, **kw: {})
    observation = bind_digest({
        'kind': 'bureau_runtime_refresh_observation', 'status': 'alert',
    }, 'observation_sha256')

    def run(*args, **kwargs):
        with operator.LOCK.open('a+') as handle, pytest.raises(BlockingIOError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return subprocess.CompletedProcess([], 0, json.dumps(observation))

    monkeypatch.setattr(operator.subprocess, 'run', run)
    assert operator.main() == 0
    receipt = json.loads((tmp_path / 'latest.json').read_text())
    assert receipt['lifecycle_state'] == 'terminal'
    assert receipt['result'] == 'partial'
    assert receipt['degraded'] is True
    assert {'kind': 'runtime_refresh', 'value': observation} in receipt['evidence']
