# -*- coding: utf-8 -*-
"""图谱回答的证据编号工具。"""

from typing import Dict, Sequence, Union

from graph_qa.models import AttributeFact, Evidence

SupportItem = Union[Evidence, AttributeFact]


def support_items_by_id(
    evidence: Sequence[Evidence],
    facts: Sequence[AttributeFact],
) -> Dict[str, SupportItem]:
    """按提示词中的固定顺序生成关系证据和属性事实编号。"""
    items: Dict[str, SupportItem] = {}
    for index, item in enumerate(evidence, start=1):
        items[f"E{index}"] = item
    for index, item in enumerate(facts, start=1):
        items[f"F{index}"] = item
    return items
