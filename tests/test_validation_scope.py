from __future__ import annotations

import subprocess

import pytest

from bureau.validation_scope import (
    FULL,
    QUEUE_ONLY,
    TASK_ONLY,
    classify_entries,
    classify_git_diff,
    parse_name_status,
)


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([('A', ('registry/tasks/EXAMPLE-V1-T001.json',))], TASK_ONLY),
        ([('M', ('registry/tasks/EXAMPLE-V1-T001.json',))], TASK_ONLY),
        (
            [
                ('A', ('registry/tasks/EXAMPLE-V1-T001.json',)),
                ('M', ('registry/tasks/EXAMPLE-V1-T002.json',)),
            ],
            TASK_ONLY,
        ),
        ([('M', ('registry/queue.json',))], QUEUE_ONLY),
        ([], FULL),
        ([('A', ('registry/queue.json',))], FULL),
        ([('D', ('registry/queue.json',))], FULL),
        ([('R100', ('registry/old.json', 'registry/queue.json'))], FULL),
        (
            [
                ('M', ('registry/queue.json',)),
                ('M', ('registry/tasks/EXAMPLE-V1-T001.json',)),
            ],
            FULL,
        ),
        ([('M', ('.github/workflows/validate.yml',))], FULL),
        ([('M', ('registry/tasks/not a task.json',))], FULL),
        ([('INVALID', ())], FULL),
    ],
)
def test_classify_entries_is_narrow_and_fail_closed(entries, expected):
    assert classify_entries(entries) == expected


def test_parse_name_status_preserves_rename_ambiguity():
    assert parse_name_status(
        'R100\tregistry/tasks/OLD.json\tregistry/tasks/NEW.json\n'
    ) == [
        (
            'R100',
            ('registry/tasks/OLD.json', 'registry/tasks/NEW.json'),
        )
    ]


def test_parse_name_status_malformed_line_falls_back_to_invalid():
    assert parse_name_status('M\n') == [('INVALID', ())]


def test_invalid_sha_falls_back_without_running_git(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('git must not run for invalid SHAs')

    monkeypatch.setattr(subprocess, 'run', forbidden)
    assert classify_git_diff('not-a-sha', 'f' * 40) == FULL


def test_git_diff_failure_falls_back_to_full(monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess,
        'run',
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=128,
            stdout='',
            stderr='fatal: unknown revision',
        ),
    )
    assert classify_git_diff('a' * 40, 'b' * 40) == FULL
    assert 'fatal: unknown revision' in capsys.readouterr().err


def test_git_diff_queue_only_result_uses_unfiltered_statuses(monkeypatch):
    observed = {}

    def fake_run(argv, **kwargs):
        observed['argv'] = argv
        observed['kwargs'] = kwargs
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout='M\tregistry/queue.json\n',
            stderr='',
        )

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert classify_git_diff('a' * 40, 'b' * 40) == QUEUE_ONLY
    assert observed['argv'] == [
        'git',
        'diff',
        '--name-status',
        f"{'a' * 40}...{'b' * 40}",
    ]
    assert observed['kwargs'] == {
        'check': False,
        'capture_output': True,
        'text': True,
    }
