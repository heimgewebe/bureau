from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .legacy import GITHUB_REPOSITORY_SLUG_RE, BureauError, Registry

REPOSITORY_IDENTIFIER_DOES_NOT_ESTABLISH = [
    "GitHub availability",
    "pull-request binding health",
    "merge readiness",
    "write authority",
]


class RepositoryIdentifierError(BureauError):
    """A stable, machine-readable repository identifier failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "status": "repository-identifier-error",
            "code": self.code,
            "message": self.message,
            "does_not_establish": REPOSITORY_IDENTIFIER_DOES_NOT_ESTABLISH,
        }
        if self.details:
            value["details"] = self.details
        return value


@dataclass(frozen=True)
class RepositorySelection:
    repository: str | None
    mode: str
    supplied_value: str | None
    resource_id: str | None = None
    deprecated: bool = False

    def metadata(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "mode": self.mode,
            "repository": self.repository,
            "supplied_value": self.supplied_value,
            "deprecated": self.deprecated,
            "does_not_establish": REPOSITORY_IDENTIFIER_DOES_NOT_ESTABLISH,
        }
        if self.resource_id is not None:
            value["resource_id"] = self.resource_id
        return value

    def notes(self) -> list[str]:
        if self.deprecated:
            return ["legacy-repo-option-deprecated-use-repo-slug"]
        return []


def validate_github_repository_slug(value: str) -> str:
    if not isinstance(value, str) or not GITHUB_REPOSITORY_SLUG_RE.fullmatch(value):
        raise RepositoryIdentifierError(
            "unsupported-repository-slug",
            "GitHub repository slug must be an explicit OWNER/REPO value",
            details={"value": value},
        )
    return value


def resolve_github_repository(
    registry: Registry,
    *,
    repo_slug: str | None = None,
    repo_resource: str | None = None,
    legacy_repo: str | None = None,
) -> RepositorySelection:
    supplied = {
        "--repo-slug": repo_slug,
        "--repo-resource": repo_resource,
        "--repo": legacy_repo,
    }
    selected = [name for name, value in supplied.items() if value is not None]
    if len(selected) > 1:
        raise RepositoryIdentifierError(
            "ambiguous-repository-identifier-options",
            "Repository identifier options are mutually exclusive",
            details={"options": selected},
        )

    if repo_resource is not None:
        resource = registry.resources.get(repo_resource)
        if resource is None:
            raise RepositoryIdentifierError(
                "missing-repository-resource",
                "Bureau repository resource does not exist",
                details={"resource_id": repo_resource},
            )
        if resource.type != "git-repository":
            raise RepositoryIdentifierError(
                "unsupported-repository-resource-type",
                "Bureau resource is not a git-repository",
                details={"resource_id": repo_resource, "resource_type": resource.type},
            )
        if resource.github_slug is None:
            raise RepositoryIdentifierError(
                "missing-github-mapping",
                "Bureau repository resource has no authoritative GitHub slug mapping",
                details={"resource_id": repo_resource},
            )
        try:
            repository = validate_github_repository_slug(resource.github_slug)
        except RepositoryIdentifierError as exc:
            raise RepositoryIdentifierError(
                "invalid-github-mapping",
                "Bureau repository resource has an invalid GitHub slug mapping",
                details={
                    "resource_id": repo_resource,
                    "github_slug": resource.github_slug,
                    "mapping_error": exc.code,
                },
            ) from exc
        mapped_resources = sorted(
            item.id
            for item in registry.resources.values()
            if item.type == "git-repository"
            and isinstance(item.github_slug, str)
            and item.github_slug.casefold() == repository.casefold()
        )
        if len(mapped_resources) > 1:
            raise RepositoryIdentifierError(
                "ambiguous-github-mapping",
                "GitHub slug is assigned to more than one Bureau repository resource",
                details={
                    "github_slug": repository,
                    "resource_ids": mapped_resources,
                },
            )
        return RepositorySelection(
            repository=repository,
            mode="resource",
            supplied_value=repo_resource,
            resource_id=repo_resource,
        )

    if repo_slug is not None:
        return RepositorySelection(
            repository=validate_github_repository_slug(repo_slug),
            mode="slug",
            supplied_value=repo_slug,
        )

    if legacy_repo is not None:
        return RepositorySelection(
            repository=validate_github_repository_slug(legacy_repo),
            mode="legacy-repo",
            supplied_value=legacy_repo,
            deprecated=True,
        )

    return RepositorySelection(
        repository=None,
        mode="all",
        supplied_value=None,
    )
