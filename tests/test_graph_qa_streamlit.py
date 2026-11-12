# -*- coding: utf-8 -*-
"""Streamlit SSE 解析测试。"""

from graph_qa.streamlit_app import (
    _bounded_history,
    _compact_session_id,
    _evidence_tab_specs,
    _follow_up_questions,
    _path_evidence_card_html,
    _progress_timeline_html,
    _relation_label,
    _stage_display_label,
    _turn_evidence_count,
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
            "plan_label": "受控多跳",
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

    assert plan_label == "生成图谱查询（受控多跳）"
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


def test_three_step_path_card_renders_all_nodes_and_relations():
    markup = _path_evidence_card_html(
        {
            "root": "四君子汤",
            "root_label": "Formula",
            "steps": [
                {
                    "relationship": "HAS_INGREDIENT",
                    "object": "人参",
                    "object_label": "Herb",
                },
                {
                    "relationship": "TREATS_DISEASE",
                    "object": "气虚证",
                    "object_label": "Disease",
                },
                {
                    "relationship": "HAS_SYMPTOM",
                    "object": "乏力",
                    "object_label": "Symptom",
                },
            ],
            "source": None,
        }
    )

    assert markup.count("kg-path-node") == 4
    assert "包含 →" in markup
    assert "治疗疾病 →" in markup
    assert "包含症状 →" in markup
    assert "乏力" in markup


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


def test_relation_labels_translate_known_graph_edges():
    assert _relation_label("HAS_INGREDIENT") == "包含"
    assert _relation_label("HAS_EFFECT") == "具有功效"
    assert _relation_label("UNKNOWN") == "UNKNOWN"
    assert _relation_label(None) == "关联"


def test_bounded_history_keeps_latest_five_turns():
    turns = [{"question": str(index)} for index in range(5)]

    updated = _bounded_history(turns, {"question": "5"})

    assert [item["question"] for item in updated] == ["1", "2", "3", "4", "5"]


def test_bounded_history_can_replace_latest_retry():
    turns = [
        {"question": "第一问"},
        {"question": "第二问", "answer": "旧回答"},
    ]

    updated = _bounded_history(
        turns,
        {"question": "第二问", "answer": "新回答"},
        replace_latest=True,
    )

    assert len(updated) == 2
    assert updated[-1]["answer"] == "新回答"


def test_follow_up_questions_use_linked_entity_and_skip_current_question():
    result = {
        "question": "麻黄有哪些禁忌？",
        "linked_entities": [
            {"label": "Herb", "name": "麻黄"},
        ],
    }

    questions = _follow_up_questions(result)

    assert questions == [
        "含麻黄的方剂有哪些？",
        "麻黄的性味归经是什么？",
    ]


def test_follow_up_questions_require_a_linked_entity():
    assert _follow_up_questions({"linked_entities": []}) == []


def test_turn_evidence_count_sums_all_supported_evidence_types():
    assert _turn_evidence_count(
        {
            "evidence": [{}],
            "facts": [{}, {}],
            "aggregates": [{}],
            "paths": [{}, {}, {}],
        }
    ) == 7
