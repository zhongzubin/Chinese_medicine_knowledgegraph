# -*- coding: utf-8 -*-
"""EntityId 生成和映射测试。"""

from graph_qa.application.entity_ids import EntityIdMapper, entity_id_for
from graph_qa.entity_linker import EntityRecord


def test_entity_id_is_deterministic_for_same_business_key():
    first = entity_id_for("Herb", "麻黄")
    second = entity_id_for("Herb", "麻黄")

    assert first == second
    assert first.startswith("eid_")
    assert len(first) == 36


def test_entity_id_separates_same_name_with_different_labels():
    herb_id = entity_id_for("Herb", "同名")
    formula_id = entity_id_for("Formula", "同名")

    assert herb_id != formula_id


def test_entity_id_mapper_resolves_known_entity_and_rejects_unknown_id():
    mapper = EntityIdMapper(
        [
            EntityRecord(label="Herb", name="麻黄"),
            EntityRecord(label="Formula", name="麻黄"),
        ]
    )
    herb_id = entity_id_for("Herb", "麻黄")

    resolved = mapper.resolve(herb_id)

    assert resolved is not None
    assert resolved.label == "Herb"
    assert resolved.name == "麻黄"
    assert mapper.resolve("eid_" + "0" * 32) is None


def test_entity_id_mapper_returns_ref_for_index_neighbor():
    mapper = EntityIdMapper([EntityRecord(label="Herb", name="麻黄")])

    ref = mapper.entity_ref("Effect", "发汗")

    assert ref.label == "Effect"
    assert ref.name == "发汗"
    assert ref.entity_id == entity_id_for("Effect", "发汗")
