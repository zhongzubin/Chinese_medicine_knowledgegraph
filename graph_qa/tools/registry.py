# -*- coding: utf-8 -*-
"""按工具 ID 和版本管理只读 Tool。"""

from typing import Dict, List, Optional, Tuple

from graph_qa.tools.contracts import ToolDefinition, ToolSpec


def _version_key(version: str) -> Tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[Tuple[str, str], ToolSpec] = {}

    def __len__(self) -> int:
        return len(self._tools)

    def register(self, spec: ToolSpec) -> None:
        definition = spec.definition
        key = (definition.tool_id, definition.version)
        if key in self._tools:
            raise ValueError(
                f"Tool 已注册: {definition.tool_id}@{definition.version}"
            )
        self._tools[key] = spec

    def get(
        self,
        tool_id: str,
        version: Optional[str] = None,
    ) -> Optional[ToolSpec]:
        if version is not None:
            return self._tools.get((tool_id, version))
        candidates = [
            spec
            for (registered_id, _registered_version), spec
            in self._tools.items()
            if registered_id == tool_id
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda spec: _version_key(spec.definition.version),
        )

    def list_definitions(self) -> List[ToolDefinition]:
        return [
            spec.definition
            for _key, spec in sorted(
                self._tools.items(),
                key=lambda item: (
                    item[0][0],
                    _version_key(item[0][1]),
                ),
            )
        ]
