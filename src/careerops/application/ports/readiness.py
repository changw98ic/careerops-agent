from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ReadinessState(StrEnum):
    OK = "ok"
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    checks: Mapping[str, ReadinessState]

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(
            state is ReadinessState.OK for state in self.checks.values()
        )


class ReadinessProbe(Protocol):
    async def check(self) -> ReadinessReport: ...

    async def close(self) -> None: ...
