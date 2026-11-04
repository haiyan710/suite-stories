"""Small common specialist protocol; specialists remain synchronous and debuggable."""

from __future__ import annotations

from typing import Any, Protocol


class SpecialistAgent(Protocol):
    name: str

    def run(self, task: Any) -> Any:
        ...
