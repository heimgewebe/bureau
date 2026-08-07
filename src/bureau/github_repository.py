from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .legacy import GITHUB_REPOSITORY_SLUG_RE, BureauError, Registry

REPOSITORY_IDENTIFIER_DOES_NOT_ESTABLISH = [
    "GitHub availability",
    "pull-request binding health",
    "merge readiness",
    "write authority",
]

CANONICAL_IDENTITY_DOES_NOT_ESTABLISH = [
    "local checkout ownership or migration authority",
    "branch deletion or dirty-state authority",
    "merge readiness",
    "claim or queue authority",
]

CanonicalSlugResolver = Callable[[str], str]


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


class RepositoryCanonicalIdentityError(RepositoryIdentifierError):
    """Fail-closed failure while binding a slug to its provider canonical identity."""


@dataclass(frozen=True)
class CanonicalRepositoryIdentity:
    """A supplied GitHub slug bound to the provider's canonical ``nameWithOwner``."""

    supplied_slug: str
    canonical_slug: str

    @property
    def redirect(self) -> bool:
        return self.supplied_slug.casefold() != self.canonical_slug.casefold()

    def metadata(self) -> dict[str, Any]:
        return {
            "supplied_slug": self.supplied_slug,
            "canonical_slug": self.canonical_slug,
            "redirect": self.redirect,
            "does_not_establish": CANONICAL_IDENTITY_DOES_NOT_ESTABLISH,
        }


def canonical_identity_enabled() -> bool:
    return os.environ.get("BUREAU_GITHUB_CANONICAL_IDENTITY", "1") not in {
        "0",
        "false",
        "False",
    }


def github_canonical_slug(slug: str) -> str:
    """Ask the provider for the canonical ``nameWithOwner`` of ``slug``."""
    binary = os.environ.get("BUREAU_GH_BIN", "gh")
    try:
        result = subprocess.run(
            [binary, "repo", "view", slug, "--json", "nameWithOwner"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepositoryCanonicalIdentityError(
            "canonical-identity-unavailable",
            f"cannot resolve canonical GitHub identity for {slug}: {exc}",
            details={"github_slug": slug},
        ) from exc
    if result.returncode != 0:
        detail = "\n".join(
            part for part in (result.stdout.strip(), result.stderr.strip()) if part
        )
        raise RepositoryCanonicalIdentityError(
            "canonical-identity-unavailable",
            f"gh repo view failed for {slug}: {detail or 'no diagnostic'}",
            details={"github_slug": slug},
        )
    try:
        value = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RepositoryCanonicalIdentityError(
            "canonical-identity-invalid",
            f"gh repo view returned invalid JSON for {slug}: {exc}",
            details={"github_slug": slug},
        ) from exc
    canonical = value.get("nameWithOwner") if isinstance(value, dict) else None
    if not isinstance(canonical, str) or not GITHUB_REPOSITORY_SLUG_RE.fullmatch(canonical):
        raise RepositoryCanonicalIdentityError(
            "canonical-identity-missing",
            f"gh repo view returned no usable nameWithOwner for {slug}",
            details={"github_slug": slug, "name_with_owner": canonical},
        )
    return canonical


def resolve_canonical_repository_identity(
    slug: str,
    *,
    resolver: CanonicalSlugResolver | None = None,
    cache: dict[str, CanonicalRepositoryIdentity] | None = None,
) -> CanonicalRepositoryIdentity:
    """Bind ``slug`` to its canonical identity, failing closed on alias loops.

    A redirect is only accepted when the canonical target resolves to itself.
    Chained or contradictory alias resolution is rejected instead of guessed.
    """
    slug = validate_github_repository_slug(slug)
    if cache is not None and slug in cache:
        return cache[slug]
    resolve = resolver or github_canonical_slug
    canonical = validate_github_repository_slug(resolve(slug))
    if canonical.casefold() != slug.casefold():
        settled = validate_github_repository_slug(resolve(canonical))
        if settled.casefold() != canonical.casefold():
            raise RepositoryCanonicalIdentityError(
                "canonical-identity-unstable",
                f"canonical GitHub identity for {slug} does not settle",
                details={
                    "github_slug": slug,
                    "canonical_slug": canonical,
                    "resolved_again": settled,
                },
            )
    identity = CanonicalRepositoryIdentity(supplied_slug=slug, canonical_slug=canonical)
    if cache is not None:
        cache[slug] = identity
        cache.setdefault(canonical, CanonicalRepositoryIdentity(canonical, canonical))
    return identity


@dataclass(frozen=True)
class RepositoryIdentityClassification:
    """Which resources actively observe a canonical repo, and which are aliases."""

    canonical_by_resource: Mapping[str, str]
    alias_by_resource: Mapping[str, dict[str, Any]]
    blocked_by_resource: Mapping[str, str]
    absorbed_aliases: Mapping[str, tuple[str, ...]]


def canonical_slug_from_pull_requests(pull_requests: Iterable[Mapping[str, Any]]) -> str | None:
    """Derive the provider canonical slug from observed pull-request URLs.

    Every observed URL already carries the provider's canonical ``nameWithOwner``
    even when the repository was queried through a historical redirect slug.
    Disagreeing URLs yield ``None`` so the caller can fail closed.
    """
    slugs: set[str] = set()
    marker = "github.com/"
    for pull_request in pull_requests:
        url = pull_request.get("url") if isinstance(pull_request, Mapping) else None
        if not isinstance(url, str) or marker not in url:
            continue
        path = url.split(marker, 1)[1].strip("/")
        parts = path.split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        slugs.add(f"{parts[0]}/{parts[1]}")
    if len(slugs) != 1:
        return None
    return slugs.pop()


def classify_repository_identities(
    slug_by_resource: Mapping[str, str],
    observed: Mapping[str, list[dict[str, Any]]],
    *,
    resolver: CanonicalSlugResolver | None = None,
) -> RepositoryIdentityClassification:
    """Collapse redirect slugs onto exactly one active canonical observer.

    Observed pull-request URLs reveal, without any extra provider call, when two
    Bureau resources are really observing the same canonical repository. Only
    such an observed collision consults ``gh repo view`` to bind the resources to
    the provider's authoritative ``nameWithOwner``. The redirect resource is then
    classified as a historical alias and removed from active GitHub observation,
    so one pull request cannot produce two reservations. Ambiguity, provider
    failure and unstable alias chains fail closed with a machine-readable
    diagnostic instead of a guessed deduplication.
    """
    canonical_by_resource = dict(slug_by_resource)
    alias_by_resource: dict[str, dict[str, Any]] = {}
    blocked_by_resource: dict[str, str] = {}
    absorbed_aliases: dict[str, tuple[str, ...]] = {}

    if not canonical_identity_enabled():
        return RepositoryIdentityClassification(
            canonical_by_resource=canonical_by_resource,
            alias_by_resource=alias_by_resource,
            blocked_by_resource=blocked_by_resource,
            absorbed_aliases=absorbed_aliases,
        )

    observed_canonical: dict[str, str] = {}
    for slug in set(slug_by_resource.values()):
        derived = canonical_slug_from_pull_requests(observed.get(slug, []))
        observed_canonical[slug] = derived or slug

    groups: dict[str, list[str]] = {}
    for resource_id, slug in sorted(slug_by_resource.items()):
        groups.setdefault(observed_canonical[slug].casefold(), []).append(resource_id)

    cache: dict[str, CanonicalRepositoryIdentity] = {}
    for _, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        identities: dict[str, CanonicalRepositoryIdentity] = {}
        failed = False
        for resource_id in members:
            try:
                identities[resource_id] = resolve_canonical_repository_identity(
                    slug_by_resource[resource_id], resolver=resolver, cache=cache
                )
            except RepositoryIdentifierError as exc:
                blocked_by_resource[resource_id] = (
                    f"cannot bind {slug_by_resource[resource_id]} to a canonical "
                    f"GitHub identity: {exc.code}: {exc.message}"
                )
                failed = True
        if failed:
            for resource_id in members:
                blocked_by_resource.setdefault(
                    resource_id,
                    "canonical GitHub identity is unresolved for a colliding "
                    f"repository group: {', '.join(members)}",
                )
            continue
        canonical_slugs = {
            identity.canonical_slug.casefold() for identity in identities.values()
        }
        self_canonical = [
            resource_id for resource_id in members if not identities[resource_id].redirect
        ]
        if len(canonical_slugs) != 1 or len(self_canonical) != 1:
            diagnostic = (
                "observed pull requests map "
                f"{len(members)} Bureau resources ({', '.join(members)}) onto the same "
                "repository without exactly one canonical owner"
            )
            for resource_id in members:
                blocked_by_resource[resource_id] = diagnostic
            continue
        owner = self_canonical[0]
        canonical_by_resource[owner] = identities[owner].canonical_slug
        aliases: list[str] = []
        for resource_id in members:
            if resource_id == owner:
                continue
            identity = identities[resource_id]
            alias_by_resource[resource_id] = {
                "supplied_slug": identity.supplied_slug,
                "canonical_slug": identity.canonical_slug,
                "canonical_resource_id": owner,
                "does_not_establish": CANONICAL_IDENTITY_DOES_NOT_ESTABLISH,
            }
            canonical_by_resource.pop(resource_id, None)
            aliases.append(identity.supplied_slug)
        if aliases:
            absorbed_aliases[owner] = tuple(sorted(aliases))

    return RepositoryIdentityClassification(
        canonical_by_resource=canonical_by_resource,
        alias_by_resource=alias_by_resource,
        blocked_by_resource=blocked_by_resource,
        absorbed_aliases=absorbed_aliases,
    )


def contradictory_canonical_identities(
    canonical_slug: str, observed_urls: Iterable[str]
) -> tuple[str, ...]:
    """Return observed PR-URL slugs that contradict ``canonical_slug``."""
    conflicting: set[str] = set()
    marker = "github.com/"
    for url in observed_urls:
        if not isinstance(url, str) or marker not in url:
            continue
        path = url.split(marker, 1)[1].strip("/")
        parts = path.split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        slug = f"{parts[0]}/{parts[1]}"
        if slug.casefold() != canonical_slug.casefold():
            conflicting.add(slug)
    return tuple(sorted(conflicting))


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
