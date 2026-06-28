from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Observation:
    state: str
    detail: dict[str, Any]


class ExternalAdapter(Protocol):
    system: str

    def dispatch(self, request: dict[str, Any]) -> str: ...

    def recover(self, request_id: str) -> str | None: ...

    def observe(self, external_id: str) -> Observation: ...

    def cancel(self, external_id: str) -> dict[str, Any]: ...

    def resume(self, external_id: str) -> dict[str, Any]: ...


class AdapterRegistry:
    def __init__(self, adapters: list[ExternalAdapter] | None = None):
        self._adapters = {adapter.system: adapter for adapter in adapters or []}
        self._unavailable: dict[str, dict[str, str | bool]] = {}

    def add(self, adapter: ExternalAdapter) -> None:
        self._adapters[adapter.system] = adapter
        self._unavailable.pop(adapter.system, None)

    def mark_unavailable(self, system: str, error: Exception) -> None:
        self._adapters.pop(system, None)
        self._unavailable[system] = {
            "available": False,
            "error_type": type(error).__name__,
            "detail": str(error),
        }

    def get(self, system: str) -> ExternalAdapter | None:
        return self._adapters.get(system)

    def unavailable_reason(self, system: str) -> str | None:
        detail = self._unavailable.get(system)
        if detail is None:
            return None
        return f"{detail['error_type']}: {detail['detail']}"

    def status(self) -> dict[str, dict[str, str | bool]]:
        systems = sorted(set(self._adapters) | set(self._unavailable))
        return {
            system: ({"available": True} if system in self._adapters else self._unavailable[system])
            for system in systems
        }
