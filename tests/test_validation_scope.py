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


@pytest.mark.parametrize(
    ("status", "path"),
    [
        ("A", "registry/tasks/AUTH.json"),
        ("M", "registry/tasks/AUTH.json"),
        ("A", "registry/tasks/ORDINARY.json"),
    ],
)
def test_git_diff_task_only_uses_path_scope_and_registry_validation(
    monkeypatch, status, path
):
    base = "a" * 40
    head = "b" * 40

    def fake_run(argv, **kwargs):
        # Changed TaskSpec contents, including source-precondition metadata,
        # are validated by the existing Registry check and registration preflight.
        # The classifier must not duplicate their payload inspection or run
        # unrelated code tests for a document-only PR.
        assert argv == ["git", "diff", "--name-status", f"{base}...{head}"]
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=f"{status}\t{path}\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert classify_git_diff(base, head) == TASK_ONLY


def test_task_content_is_checked_by_registry_not_by_scope_classifier(monkeypatch):
    base = "a" * 40
    head = "b" * 40

    def fake_run(argv, **kwargs):
        assert argv == ["git", "diff", "--name-status", f"{base}...{head}"]
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout="A\tregistry/tasks/BROKEN.json\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Invalid contents are rejected by the fast-path Registry validation,
    # not through a redundant Git-show classifier probe.
    assert classify_git_diff(base, head) == TASK_ONLY
