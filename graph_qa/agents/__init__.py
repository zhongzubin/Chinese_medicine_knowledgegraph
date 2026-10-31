# -*- coding: utf-8 -*-
"""Graph QA 工作流中的角色 Agent。"""

from graph_qa.agents.base import AgentDescriptor, AgentRegistry
from graph_qa.agents.roles import (
    AnswerAgent,
    DialogueAgent,
    EntityAgent,
    IntentAgent,
    QAAgentTeam,
)

__all__ = [
    "AgentDescriptor",
    "AgentRegistry",
    "AnswerAgent",
    "DialogueAgent",
    "EntityAgent",
    "IntentAgent",
    "QAAgentTeam",
]
