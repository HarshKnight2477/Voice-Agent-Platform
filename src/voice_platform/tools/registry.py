"""The registry of tool implementations, and per-agent toolsets.

The model only sees tools that (a) exist, (b) the tenant may use, and (c) the agent version
explicitly grants. Anything else is invisible, and the gateway refuses it anyway.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from voice_platform.agents.config_schema import ToolGrant
from voice_platform.runtime.llm import ToolSpec
from voice_platform.tools.base import ToolDefinition


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolDefinition] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for t in tools:
            self.register(t)

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    @staticmethod
    def available_to(tool: ToolDefinition, organization_id: uuid.UUID) -> bool:
        return tool.owner_organization_id in (None, organization_id)

    def toolset(
        self,
        organization_id: uuid.UUID,
        grants: Iterable[ToolGrant],
        always: Iterable[str] = (),
    ) -> tuple[tuple[ToolSpec, ...], list[str]]:
        """Tool specs to show the model, plus the names of granted tools that are not
        available (unknown, or owned by another tenant) so they can be flagged at publish."""
        specs: list[ToolSpec] = []
        unavailable: list[str] = []
        names = [*always, *(g.name for g in grants)]
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            tool = self._tools.get(name)
            if tool is None or not self.available_to(tool, organization_id):
                unavailable.append(name)
                continue
            specs.append(ToolSpec(tool.name, tool.description, tool.json_schema()))
        # Sorted so the tool list (part of the cached prompt prefix) is stable.
        return tuple(sorted(specs, key=lambda s: s.name)), unavailable
