# -*- coding: utf-8 -*-
"""Streamlit SSE 解析测试。"""

from graph_qa.streamlit_app import _stage_display_label, parse_sse_lines


def test_parse_sse_lines_handles_multiple_events_and_utf8():
    lines = iter(
        [
            "event: stage",
            'data: {"stage":"normalize","label":"规范化问题"}',
            "",
            "event: answer_delta",
            'data: {"delta":"麻黄"}',
            "",
        ]
    )

    events = list(parse_sse_lines(lines))

    assert events == [
        ("stage", {"stage": "normalize", "label": "规范化问题"}),
        ("answer_delta", {"delta": "麻黄"}),
    ]


def test_parse_sse_lines_accepts_attribute_fact_event():
    events = list(
        parse_sse_lines(
            iter(
                [
                    "event: facts",
                    (
                        'data: {"items":[{"subject":"麻黄","attribute_label":"性味",'
                        '"value":"辛、微苦，温"}]}'
                    ),
                    "",
                ]
            )
        )
    )

    assert events[0][0] == "facts"
    assert events[0][1]["items"][0]["attribute_label"] == "性味"


def test_parse_sse_lines_accepts_aggregate_event():
    events = list(
        parse_sse_lines(
            iter(
                [
                    "event: aggregates",
                    (
                        'data: {"items":[{"description":"关联到人参的方剂",'
                        '"count":128}]}'
                    ),
                    "",
                ]
            )
        )
    )

    assert events[0][0] == "aggregates"
    assert events[0][1]["items"][0]["count"] == 128


def test_parse_sse_lines_accepts_multi_hop_path_event():
    events = list(
        parse_sse_lines(
            iter(
                [
                    "event: paths",
                    (
                        'data: {"items":[{"root":"四君子汤",'
                        '"intermediate":"人参","target":"补气"}]}'
                    ),
                    "",
                ]
            )
        )
    )

    assert events[0][0] == "paths"
    assert events[0][1]["items"][0]["intermediate"] == "人参"


def test_parse_sse_lines_accepts_answer_audit_event():
    events = list(
        parse_sse_lines(
            iter(
                [
                    "event: audit",
                    (
                        'data: {"status":"passed","agent":"evidence_audit",'
                        '"checked_items":3,"missing_sources":1}'
                    ),
                    "",
                ]
            )
        )
    )

    assert events[0][0] == "audit"
    assert events[0][1]["checked_items"] == 3


def test_stage_display_label_only_adds_safe_retrieval_plan_type():
    plan_label = _stage_display_label(
        {
            "stage": "plan_cypher",
            "label": "生成图谱查询",
            "plan_kind": "controlled_multi_hop",
            "plan_label": "受控两跳",
        }
    )
    generic_label = _stage_display_label(
        {
            "stage": "validate_cypher",
            "label": "校验查询安全",
            "plan_label": "不应显示",
        }
    )
    observe_label = _stage_display_label(
        {
            "stage": "retrieve_observe",
            "label": "观察检索结果并决定下一步",
            "decision": "replan",
            "reason_code": "VALIDATION_RETRY",
        }
    )

    assert plan_label == "生成图谱查询（受控两跳）"
    assert generic_label == "校验查询安全"
    assert observe_label == "观察检索结果并决定下一步（重新规划）"
