from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Observation:
    state: str
    detail: dict[str, Any]


class ExternalAdapter(Protocol):
    system: str
    aliases: tuple[str, ...]

    def dispatch(self, request: dict[str, Any]) -> str: ...

    def recover(self, request_id: str) -> str | None: ...

    def observe(self, external_id: str) -> Observation: ...

    def cancel(self, external_id: str) -> dict[str, Any]: ...

    def resume(self, external_id: str) -> dict[str, Any]: ...


class AdapterRegistry:
    def __init__(self, adapters: list[ExternalAdapter] | None = None):
        self._adapters: dict[str, ExternalAdapter] = {}
        self._unavailable: dict[str, dict[str, str | bool]] = {}
        self._systems_by_name: dict[str, frozenset[str]] = {}
        for adapter in adapters or []:
            self.add(adapter)

    @staticmethod
    def _systems(adapter: ExternalAdapter) -> frozenset[str]:
        return frozenset((adapter.system, *getattr(adapter, "aliases", ())))

    def add(self, adapter: ExternalAdapter) -> None:
        systems = self._systems(adapter)
        for system in systems:
            registered = self._adapters.get(system)
            if registered is not None and registered is not adapter:
                raise ValueError(f"external adapter system already registered: {system}")
        for system in systems:
            self._adapters[system] = adapter
            self._systems_by_name[system] = systems
            self._unavailable.pop(system, None)

    def mark_unavailable(self, system: str, error: Exception) -> None:
        adapter = self._adapters.get(system)
        systems = self._systems_by_name.get(system)
        if systems is None and adapter is not None:
            systems = self._systems(adapter)
        if systems is None:
            systems = frozenset((system,))

        unavailable = {
            "available": False,
            "error_type": type(error).__name__,
            "detail": str(error),
        }
        for adapter_system in systems:
            self._adapters.pop(adapter_system, None)
            self._unavailable[adapter_system] = unavailable.copy()

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
