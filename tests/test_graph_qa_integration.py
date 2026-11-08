# -*- coding: utf-8 -*-
"""真实 Neo4j 与模型接口的可选集成测试。"""

import os

import pytest

from graph_qa.cli import build_engine_from_environment


pytestmark = pytest.mark.skipif(
    os.getenv("GRAPH_QA_RUN_INTEGRATION") != "1",
    reason="设置 GRAPH_QA_RUN_INTEGRATION=1 后才运行真实模型和 Neo4j 集成测试",
)


def test_known_herb_question_returns_grounded_evidence():
    engine = build_engine_from_environment()
    try:
        response = engine.answer("麻黄有什么功效？")
    finally:
        engine.repository.close()

    assert response.error is None
    assert response.grounded is True
    assert response.query is not None
    assert response.evidence or response.facts
    assert response.verification is not None
    assert any(
        item.subject_label == "Herb"
        and item.subject == "麻黄"
        and item.relationship == "HAS_EFFECT"
        for item in response.evidence
    ) or any(
        item.subject_label == "Herb"
        and item.subject == "麻黄"
        and item.attribute == "effect"
        and "发汗" in item.value
        for item in response.facts
    )
