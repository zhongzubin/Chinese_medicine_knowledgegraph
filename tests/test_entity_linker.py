# -*- coding: utf-8 -*-
"""实体链接器测试。"""

from graph_qa.entity_linker import (
    EntityIndex,
    EntityRecord,
    normalize_question_boundaries,
)
from graph_qa.models import EntityCandidate, LinkedEntity


def make_index() -> EntityIndex:
    return EntityIndex.from_records(
        [
            EntityRecord("Herb", "麻黄", "龙沙、狗骨"),
            EntityRecord("Herb", "桂枝", "柳桂,川桂枝"),
            EntityRecord("Disease", "消渴"),
            EntityRecord("Symptom", "消渴"),
            EntityRecord("Effect", "发汗"),
        ]
    )


def test_alias_exact_match_returns_canonical_entity():
    linked = make_index().match(
        [EntityCandidate(text="龙沙", suggested_label="Herb")],
        "龙沙有什么功效？",
    )

    assert linked[0].label == "Herb"
    assert linked[0].name == "麻黄"
    assert linked[0].match_type == "alias"
    assert linked[0].score == 1.0


def test_direct_question_match_works_without_candidates():
    linked = make_index().match([], "柳桂能治疗什么疾病？")

    assert [(item.label, item.name) for item in linked] == [("Herb", "桂枝")]


def test_direct_question_match_prefers_longest_entity_term():
    index = EntityIndex.from_records(
        [
            EntityRecord("Disease", "风热咳嗽"),
            EntityRecord("Disease", "咳嗽"),
            EntityRecord("Symptom", "风热咳嗽"),
            EntityRecord("Symptom", "咳嗽"),
        ]
    )

    linked = index.match([], "风热咳嗽可以用什么缓解？")

    assert {(item.label, item.name) for item in linked} == {
        ("Disease", "风热咳嗽"),
        ("Symptom", "风热咳嗽"),
    }


def test_direct_question_match_finds_multiple_list_entities():
    index = EntityIndex.from_records(
        [
            EntityRecord("Herb", "人参"),
            EntityRecord("Herb", "白术"),
            EntityRecord("Herb", "茯苓"),
        ]
    )

    linked = index.match_exact("同时含有人参、白术和茯苓的方剂有哪些？")

    assert {(item.label, item.name) for item in linked} == {
        ("Herb", "人参"),
        ("Herb", "白术"),
        ("Herb", "茯苓"),
    }


def test_question_boundary_normalization_preserves_list_separators():
    assert normalize_question_boundaries("人参、白术和茯苓") == (
        "人参|白术和茯苓"
    )


def test_direct_question_match_rejects_short_entity_inside_unknown_compound():
    index = EntityIndex.from_records(
        [
            EntityRecord("Herb", "青果"),
            EntityRecord("Effect", "功效"),
        ]
    )

    linked = index.match([], "空青果有什么功效？")

    assert linked == []


def test_exact_prequestion_match_rejects_formula_component_names():
    index = EntityIndex.from_records(
        [
            EntityRecord("Formula", "麝香追风膏"),
            EntityRecord("Herb", "麝香"),
        ]
    )

    linked = index.match_exact("麝香追风羔由哪些药材组成？")

    assert linked == []


def test_direct_question_match_accepts_entity_at_question_boundary():
    linked = make_index().match_exact("麻黄有什么功效？")

    assert [(item.label, item.name) for item in linked] == [("Herb", "麻黄")]


def test_generic_entity_candidate_does_not_trigger_semantic_match():
    class RecordingSemanticMatcher:
        def __init__(self):
            self.calls = []

        def match(self, text):
            self.calls.append(text)
            return [
                LinkedEntity(
                    label="Effect",
                    name="清热解毒",
                    matched_text=text,
                    match_type="semantic",
                    score=0.8,
                )
            ]

    semantic = RecordingSemanticMatcher()
    index = EntityIndex.from_records(
        [EntityRecord("Effect", "功效")],
        semantic_index=semantic,
    )

    linked = index.match(
        [EntityCandidate(text="功效", suggested_label="Effect")],
        "九幽草有什么功效？",
    )

    assert linked == []
    assert semantic.calls == []


def test_unknown_named_herb_is_not_accepted_from_semantic_only_match():
    class NamedHerbSemanticMatcher:
        def match(self, text):
            return [
                LinkedEntity(
                    label="Herb",
                    name="梦花根",
                    matched_text=text,
                    match_type="semantic",
                    score=0.5823,
                )
            ]

    index = EntityIndex.from_records(
        [EntityRecord("Herb", "梦花根")],
        semantic_index=NamedHerbSemanticMatcher(),
    )

    linked = index.match(
        [EntityCandidate(text="云梦根", suggested_label="Herb")],
        "云梦根有什么功效？",
    )

    assert linked == []


def test_named_question_does_not_fall_back_to_semantic_herb_match():
    class NamedHerbSemanticMatcher:
        def match(self, text):
            return [
                LinkedEntity(
                    label="Herb",
                    name="白鹤藤",
                    matched_text=text,
                    match_type="semantic",
                    score=0.5642,
                )
            ]

    index = EntityIndex.from_records(
        [EntityRecord("Herb", "白鹤藤")],
        semantic_index=NamedHerbSemanticMatcher(),
    )

    linked = index.match([], "白羽藤有什么功效？")

    assert linked == []


def test_descriptive_phrase_still_uses_semantic_effect_match():
    class EffectSemanticMatcher:
        def match(self, text):
            return [
                LinkedEntity(
                    label="Effect",
                    name="清热解毒",
                    matched_text=text,
                    match_type="semantic",
                    score=0.68,
                )
            ]

    index = EntityIndex.from_records(
        [EntityRecord("Effect", "清热解毒")],
        semantic_index=EffectSemanticMatcher(),
    )

    linked = index.match(
        [EntityCandidate(text="清除体内热毒", suggested_label="Effect")],
        "哪些方剂能清除体内热毒？",
    )

    assert [(item.label, item.name) for item in linked] == [
        ("Effect", "清热解毒")
    ]


def test_fuzzy_match_keeps_supported_entity_suffix_variant():
    index = EntityIndex.from_records([EntityRecord("Herb", "麻黄")])

    linked = index.match(
        [EntityCandidate(text="麻黄草", suggested_label="Herb")],
        "麻黄草有什么功效？",
    )

    assert [(item.label, item.name, item.match_type) for item in linked] == [
        ("Herb", "麻黄", "fuzzy")
    ]


def test_fuzzy_match_rejects_inserted_unknown_name_characters():
    index = EntityIndex.from_records(
        [
            EntityRecord("Herb", "青果"),
            EntityRecord("Herb", "石见穿", "紫参"),
        ]
    )

    linked = index.match(
        [
            EntityCandidate(text="空青果", suggested_label="Herb"),
            EntityCandidate(text="紫霄参", suggested_label="Herb"),
        ],
        "这些药材有什么功效？",
    )

    assert linked == []


def test_candidate_linking_does_not_scan_unrequested_entities_from_full_question():
    linked = make_index().match(
        [EntityCandidate(text="麻黄", suggested_label="Herb")],
        "麻黄和柳桂分别有什么功效？",
    )

    assert [(item.label, item.name) for item in linked] == [("Herb", "麻黄")]


def test_same_name_entities_with_different_labels_are_not_merged():
    linked = make_index().match(
        [EntityCandidate(text="消渴")],
        "消渴是什么？",
    )

    assert {(item.label, item.name) for item in linked} == {
        ("Disease", "消渴"),
        ("Symptom", "消渴"),
    }


def test_suggested_label_is_hint_not_a_hard_filter_for_same_name_entities():
    linked = make_index().match(
        [EntityCandidate(text="消渴", suggested_label="Disease")],
        "消渴在方剂中作为症状可以怎样缓解？",
    )

    assert {(item.label, item.name) for item in linked} == {
        ("Disease", "消渴"),
        ("Symptom", "消渴"),
    }
    assert linked[0].label == "Disease"


def test_unknown_suggested_label_falls_back_to_all_labels():
    candidate = EntityCandidate(
        text="消渴",
        suggested_label="Unknown",  # type: ignore[arg-type]
    )

    linked = make_index().match([candidate], "消渴是什么？")

    assert {item.label for item in linked} == {"Disease", "Symptom"}


def test_normalization_ignores_width_and_spacing():
    linked = make_index().match(
        [EntityCandidate(text=" 麻　黄 ", suggested_label="Herb")],
        "麻　黄有什么功效？",
    )

    assert linked[0].name == "麻黄"


def test_limit_is_applied_per_label():
    index = EntityIndex.from_records(
        [
            EntityRecord("Herb", f"麻黄{i}", None)
            for i in range(1, 6)
        ]
    )

    linked = index.match(
        [EntityCandidate(text="麻黄", suggested_label="Herb")],
        "麻黄草有什么功效？",
        limit_per_label=2,
        fuzzy_threshold=0.5,
    )

    assert len(linked) <= 2
    assert all(item.label == "Herb" for item in linked)
