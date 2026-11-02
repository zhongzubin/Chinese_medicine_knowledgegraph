# -*- coding: utf-8 -*-
"""Streamlit SSE 解析测试。"""

from graph_qa.streamlit_app import (
    _compact_session_id,
    _evidence_tab_specs,
    _progress_timeline_html,
    _stage_display_label,
    _verification_ratio,
    parse_sse_lines,
)


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


def test_compact_session_id_never_exposes_full_identifier():
    assert _compact_session_id(None) == "尚未建立会话"
    assert _compact_session_id(
        "12345678-90ab-cdef-1234-567890abcdef"
    ) == "会话 · 12345678"


def test_evidence_tab_specs_only_returns_available_groups():
    specs = _evidence_tab_specs(
        evidence=[{"subject": "麻黄"}],
        facts=[{"subject": "麻黄"}, {"subject": "桂枝"}],
        aggregates=[],
        paths=[{"root": "四君子汤"}],
    )

    assert specs == [
        ("relations", "关系证据", 1),
        ("facts", "属性事实", 2),
        ("paths", "多跳路径", 1),
    ]


def test_verification_ratio_handles_empty_and_complete_coverage():
    assert _verification_ratio(None) == 0.0
    assert _verification_ratio({"checked_items": 0}) == 0.0
    assert _verification_ratio(
        {"checked_items": 4, "matched_items": 3}
    ) == 0.75
    assert _verification_ratio(
        {"checked_items": 3, "matched_items": 5}
    ) == 1.0


def test_progress_timeline_escapes_public_stage_labels():
    markup = _progress_timeline_html(
        ["匹配实体", "<script>alert(1)</script>"],
        running=True,
    )

    assert "匹配实体" in markup
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in markup
    assert "<script>" not in markup
