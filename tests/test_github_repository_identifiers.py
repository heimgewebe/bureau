from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from bureau.cli import main, parser
from bureau.github_repository import (
    CanonicalRepositoryIdentity,
    RepositoryCanonicalIdentityError,
    RepositoryIdentifierError,
    canonical_slug_from_pull_requests,
    classify_repository_identities,
    resolve_github_repository,
    validate_github_repository_slug,
)
from bureau.legacy import Registry, Resource, ValidationError


def fake_gh(tmp_path: Path, marker: Path) -> str:
    path = tmp_path / "fake-gh-identifiers"
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {marker}\n"
        "printf '[]\\n'\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def set_repository_mapping(root: Path, slug: str = "heimgewebe/test") -> None:
    path = root / "registry/resources/1.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["github_slug"] = slug
    path.write_text(json.dumps(value), encoding="utf-8")


def cli_json(root: Path, state_root: Path, *arguments: str) -> list[str]:
    return [
        "--root",
        str(root),
        "--state-root",
        str(state_root),
        "--json",
        "github-observe",
        *arguments,
    ]


def test_slug_validation_accepts_owner_repo_and_rejects_resource_id() -> None:
    assert validate_github_repository_slug("heimgewebe/bureau") == "heimgewebe/bureau"
    with pytest.raises(RepositoryIdentifierError) as exc:
        validate_github_repository_slug("repo.bureau")
    assert exc.value.code == "unsupported-repository-slug"


def test_resource_resolution_uses_only_authoritative_registry_mapping(
    registry_factory,
) -> None:
    root = registry_factory()
    set_repository_mapping(root)
    selection = resolve_github_repository(
        Registry.load(root), repo_resource="repo"
    )
    assert selection.repository == "heimgewebe/test"
    assert selection.metadata()["resource_id"] == "repo"
    assert selection.mode == "resource"


def test_resource_resolution_has_stable_fail_closed_errors(registry_factory) -> None:
    root = registry_factory()
    registry = Registry.load(root)

    with pytest.raises(RepositoryIdentifierError) as missing:
        resolve_github_repository(registry, repo_resource="repo.missing")
    assert missing.value.code == "missing-repository-resource"

    with pytest.raises(RepositoryIdentifierError) as wrong_type:
        resolve_github_repository(registry, repo_resource="repo.alpha")
    assert wrong_type.value.code == "unsupported-repository-resource-type"

    with pytest.raises(RepositoryIdentifierError) as no_mapping:
        resolve_github_repository(registry, repo_resource="repo")
    assert no_mapping.value.code == "missing-github-mapping"


def test_defensive_resolver_reports_ambiguous_mapping(registry_factory) -> None:
    root = registry_factory()
    set_repository_mapping(root)
    registry = Registry.load(root)
    registry.resources["repo.other"] = Resource(
        id="repo.other",
        type="git-repository",
        parent="root",
        capacity=None,
        path=str(root / "other"),
        github_slug="HEIMGEWEBE/TEST",
        grabowski_key=None,
    )

    with pytest.raises(RepositoryIdentifierError) as exc:
        resolve_github_repository(registry, repo_resource="repo")

    assert exc.value.code == "ambiguous-github-mapping"
    assert exc.value.details["resource_ids"] == ["repo", "repo.other"]


def test_cli_help_names_explicit_and_deprecated_modes(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(["github-observe", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--repo-slug OWNER/REPO" in output
    assert "--repo-resource RESOURCE_ID" in output
    assert "deprecated compatibility alias" in output


def test_multiple_identifier_modes_are_rejected(registry_factory) -> None:
    registry = Registry.load(registry_factory())
    with pytest.raises(RepositoryIdentifierError) as exc:
        resolve_github_repository(
            registry,
            repo_slug="heimgewebe/bureau",
            repo_resource="repo",
        )
    assert exc.value.code == "ambiguous-repository-identifier-options"
    assert exc.value.details["options"] == ["--repo-slug", "--repo-resource"]


def test_cli_repo_slug_is_validated_before_gh(
    registry_factory, tmp_path: Path, capsys, monkeypatch
) -> None:
    root = registry_factory()
    marker = tmp_path / "gh-arguments"
    monkeypatch.setenv("BUREAU_GH_BIN", fake_gh(tmp_path, marker))

    code = main(cli_json(root, tmp_path / "state", "--repo-slug", "repo.bureau"))

    assert code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "repository-identifier-error"
    assert value["code"] == "unsupported-repository-slug"
    assert not marker.exists()


def test_cli_missing_mapping_fails_before_gh(
    registry_factory, tmp_path: Path, capsys, monkeypatch
) -> None:
    root = registry_factory()
    marker = tmp_path / "gh-arguments"
    monkeypatch.setenv("BUREAU_GH_BIN", fake_gh(tmp_path, marker))

    code = main(cli_json(root, tmp_path / "state", "--repo-resource", "repo"))

    assert code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["code"] == "missing-github-mapping"
    assert value["details"] == {"resource_id": "repo"}
    assert not marker.exists()


def test_cli_repo_resource_resolves_before_gh(
    registry_factory, tmp_path: Path, capsys, monkeypatch
) -> None:
    root = registry_factory()
    set_repository_mapping(root)
    marker = tmp_path / "gh-arguments"
    monkeypatch.setenv("BUREAU_GH_BIN", fake_gh(tmp_path, marker))

    code = main(cli_json(root, tmp_path / "state", "--repo-resource", "repo"))

    assert code == 0
    value = json.loads(capsys.readouterr().out)
    assert value["repository"] == "heimgewebe/test"
    assert value["repository_input"] == {
        "mode": "resource",
        "repository": "heimgewebe/test",
        "supplied_value": "repo",
        "deprecated": False,
        "resource_id": "repo",
        "does_not_establish": [
            "GitHub availability",
            "pull-request binding health",
            "merge readiness",
            "write authority",
        ],
    }
    arguments = marker.read_text(encoding="utf-8").splitlines()
    assert "--repo" in arguments
    assert "heimgewebe/test" in arguments
    assert "repo" not in arguments


def test_cli_legacy_repo_is_explicitly_deprecated_but_compatible(
    registry_factory, tmp_path: Path, capsys, monkeypatch
) -> None:
    root = registry_factory()
    marker = tmp_path / "gh-arguments"
    monkeypatch.setenv("BUREAU_GH_BIN", fake_gh(tmp_path, marker))

    code = main(cli_json(root, tmp_path / "state", "--repo", "heimgewebe/test"))

    assert code == 0
    value = json.loads(capsys.readouterr().out)
    assert value["repository_input"]["mode"] == "legacy-repo"
    assert value["repository_input"]["deprecated"] is True
    assert "legacy-repo-option-deprecated-use-repo-slug" in value["notes"]


def test_cli_ambiguous_options_return_machine_readable_error(
    registry_factory, tmp_path: Path, capsys, monkeypatch
) -> None:
    root = registry_factory()
    marker = tmp_path / "gh-arguments"
    monkeypatch.setenv("BUREAU_GH_BIN", fake_gh(tmp_path, marker))

    code = main(
        cli_json(
            root,
            tmp_path / "state",
            "--repo-slug",
            "heimgewebe/test",
            "--repo-resource",
            "repo",
        )
    )

    assert code == 2
    value = json.loads(capsys.readouterr().out)
    assert value["code"] == "ambiguous-repository-identifier-options"
    assert not marker.exists()


def test_registry_rejects_invalid_and_duplicate_github_mappings(
    registry_factory,
) -> None:
    invalid_root = registry_factory()
    set_repository_mapping(invalid_root, "repo.bureau")
    with pytest.raises(ValidationError, match="invalid github_slug"):
        Registry.load(invalid_root)

    duplicate_root = registry_factory()
    set_repository_mapping(duplicate_root, "heimgewebe/test")
    duplicate = {
        "schema_version": 1,
        "id": "repo.other",
        "type": "git-repository",
        "parent": "root",
        "path": str(duplicate_root / "other"),
        "github_slug": "HEIMGEWEBE/TEST",
    }
    (duplicate_root / "registry/resources/duplicate.json").write_text(
        json.dumps(duplicate), encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="share github_slug"):
        Registry.load(duplicate_root)


def _canonical_resolver(mapping: dict[str, str]):
    def resolve(slug: str) -> str:
        try:
            return mapping[slug]
        except KeyError as exc:  # pragma: no cover - guards test wiring mistakes
            raise AssertionError(f"unexpected canonical lookup for {slug}") from exc

    return resolve


def _pr(url: str) -> dict[str, object]:
    return {"number": 1130, "url": url}


def test_canonical_slug_is_derived_from_observed_pull_request_urls():
    observed = [_pr("https://github.com/heimgewebe/repoground/pull/1130")]

    assert (
        canonical_slug_from_pull_requests(observed) == "heimgewebe/repoground"
    )
    assert canonical_slug_from_pull_requests([]) is None
    assert (
        canonical_slug_from_pull_requests(
            [
                _pr("https://github.com/heimgewebe/repoground/pull/1130"),
                _pr("https://github.com/heimgewebe/other/pull/7"),
            ]
        )
        is None
    )


def test_redirect_slug_is_classified_as_alias_not_a_second_identity():
    """The live heimgewebe/lenskit -> heimgewebe/repoground redirect case."""
    pull_requests = [_pr("https://github.com/heimgewebe/repoground/pull/1130")]
    classification = classify_repository_identities(
        {"repo.lenskit": "heimgewebe/lenskit", "repo.repoground": "heimgewebe/repoground"},
        {"heimgewebe/lenskit": pull_requests, "heimgewebe/repoground": pull_requests},
        resolver=_canonical_resolver(
            {
                "heimgewebe/lenskit": "heimgewebe/repoground",
                "heimgewebe/repoground": "heimgewebe/repoground",
            }
        ),
    )

    assert classification.canonical_by_resource == {"repo.repoground": "heimgewebe/repoground"}
    assert "repo.lenskit" not in classification.canonical_by_resource
    assert classification.blocked_by_resource == {}
    alias = classification.alias_by_resource["repo.lenskit"]
    assert alias["supplied_slug"] == "heimgewebe/lenskit"
    assert alias["canonical_slug"] == "heimgewebe/repoground"
    assert alias["canonical_resource_id"] == "repo.repoground"
    assert classification.absorbed_aliases == {"repo.repoground": ("heimgewebe/lenskit",)}


def test_distinct_repositories_are_untouched_and_need_no_provider_call():
    def resolve(slug: str) -> str:  # pragma: no cover - must not be reached
        raise AssertionError(f"no canonical lookup expected for {slug}")

    classification = classify_repository_identities(
        {"repo.alpha": "heimgewebe/alpha", "repo.beta": "heimgewebe/beta"},
        {
            "heimgewebe/alpha": [_pr("https://github.com/heimgewebe/alpha/pull/1")],
            "heimgewebe/beta": [_pr("https://github.com/heimgewebe/beta/pull/2")],
        },
        resolver=resolve,
    )

    assert classification.canonical_by_resource == {
        "repo.alpha": "heimgewebe/alpha",
        "repo.beta": "heimgewebe/beta",
    }
    assert classification.alias_by_resource == {}
    assert classification.blocked_by_resource == {}


def test_provider_failure_on_a_colliding_group_fails_closed():
    pull_requests = [_pr("https://github.com/heimgewebe/repoground/pull/1130")]

    def resolve(slug: str) -> str:
        raise RepositoryCanonicalIdentityError(
            "canonical-identity-unavailable", f"gh repo view failed for {slug}"
        )

    classification = classify_repository_identities(
        {"repo.lenskit": "heimgewebe/lenskit", "repo.repoground": "heimgewebe/repoground"},
        {"heimgewebe/lenskit": pull_requests, "heimgewebe/repoground": pull_requests},
        resolver=resolve,
    )

    assert classification.alias_by_resource == {}
    assert set(classification.blocked_by_resource) == {"repo.lenskit", "repo.repoground"}
    for diagnostic in classification.blocked_by_resource.values():
        assert "canonical-identity-unavailable" in diagnostic


def test_collision_without_exactly_one_canonical_owner_fails_closed():
    pull_requests = [_pr("https://github.com/heimgewebe/repoground/pull/1130")]
    classification = classify_repository_identities(
        {"repo.lenskit": "heimgewebe/lenskit", "repo.old": "heimgewebe/old"},
        {"heimgewebe/lenskit": pull_requests, "heimgewebe/old": pull_requests},
        resolver=_canonical_resolver(
            {
                "heimgewebe/lenskit": "heimgewebe/repoground",
                "heimgewebe/old": "heimgewebe/repoground",
                "heimgewebe/repoground": "heimgewebe/repoground",
            }
        ),
    )

    assert classification.alias_by_resource == {}
    assert set(classification.blocked_by_resource) == {"repo.lenskit", "repo.old"}
    for diagnostic in classification.blocked_by_resource.values():
        assert "without exactly one canonical owner" in diagnostic


def test_unstable_alias_chain_fails_closed():
    pull_requests = [_pr("https://github.com/heimgewebe/repoground/pull/1130")]
    classification = classify_repository_identities(
        {"repo.lenskit": "heimgewebe/lenskit", "repo.repoground": "heimgewebe/repoground"},
        {"heimgewebe/lenskit": pull_requests, "heimgewebe/repoground": pull_requests},
        resolver=_canonical_resolver(
            {
                "heimgewebe/lenskit": "heimgewebe/repoground",
                "heimgewebe/repoground": "heimgewebe/moved-again",
                "heimgewebe/moved-again": "heimgewebe/moved-again",
            }
        ),
    )

    assert classification.alias_by_resource == {}
    assert set(classification.blocked_by_resource) == {"repo.lenskit", "repo.repoground"}


def test_canonical_identity_can_be_disabled_for_recovery(monkeypatch):
    monkeypatch.setenv("BUREAU_GITHUB_CANONICAL_IDENTITY", "0")
    pull_requests = [_pr("https://github.com/heimgewebe/repoground/pull/1130")]

    classification = classify_repository_identities(
        {"repo.lenskit": "heimgewebe/lenskit", "repo.repoground": "heimgewebe/repoground"},
        {"heimgewebe/lenskit": pull_requests, "heimgewebe/repoground": pull_requests},
        resolver=_canonical_resolver({}),
    )

    assert classification.canonical_by_resource == {
        "repo.lenskit": "heimgewebe/lenskit",
        "repo.repoground": "heimgewebe/repoground",
    }
    assert classification.alias_by_resource == {}


def test_classification_grants_no_new_authority():
    identity = CanonicalRepositoryIdentity("heimgewebe/lenskit", "heimgewebe/repoground")

    assert identity.redirect is True
    metadata = identity.metadata()
    assert "local checkout ownership or migration authority" in metadata["does_not_establish"]
    assert "branch deletion or dirty-state authority" in metadata["does_not_establish"]
    assert "merge readiness" in metadata["does_not_establish"]
    assert "claim or queue authority" in metadata["does_not_establish"]
