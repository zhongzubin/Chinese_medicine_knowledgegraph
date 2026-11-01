# -*- coding: utf-8 -*-
"""Agent 角色描述与阶段注册表。"""

from dataclasses import dataclass
from typing import Dict, Iterable, Tuple


@dataclass(frozen=True)
class AgentDescriptor:
    name: str
    responsibility: str
    stages: Tuple[str, ...]
    tools: Tuple[str, ...] = ()

    def public_dict(self) -> dict:
        return {
            "name": self.name,
            "responsibility": self.responsibility,
            "stages": list(self.stages),
            "tools": list(self.tools),
        }


class AgentRegistry:
    """将固定工作流阶段映射到职责 Agent。"""

    def __init__(self, descriptors: Iterable[AgentDescriptor]):
        self._by_name: Dict[str, AgentDescriptor] = {}
        self._by_stage: Dict[str, AgentDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.name in self._by_name:
                raise ValueError(f"重复的 Agent 名称: {descriptor.name}")
            self._by_name[descriptor.name] = descriptor
            for stage in descriptor.stages:
                if stage in self._by_stage:
                    raise ValueError(f"阶段被多个 Agent 注册: {stage}")
                self._by_stage[stage] = descriptor

    @property
    def descriptors(self) -> Tuple[AgentDescriptor, ...]:
        return tuple(self._by_name.values())

    def get(self, name: str) -> AgentDescriptor:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError(f"未知 Agent: {name}") from exc

    def for_stage(self, stage: str) -> AgentDescriptor:
        try:
            return self._by_stage[stage]
        except KeyError as exc:
            raise KeyError(f"阶段未注册 Agent: {stage}") from exc
