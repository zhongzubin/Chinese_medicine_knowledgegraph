# -*- coding: utf-8 -*-
"""Streamlit SSE 解析测试。"""

from graph_qa.streamlit_app import parse_sse_lines


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
