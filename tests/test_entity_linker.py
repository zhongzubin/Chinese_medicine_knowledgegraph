# -*- coding: utf-8 -*-
"""实体链接器测试。"""

from graph_qa.entity_linker import EntityIndex, EntityRecord
from graph_qa.models import EntityCandidate


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
