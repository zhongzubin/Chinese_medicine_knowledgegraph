# -*- coding: utf-8 -*-
"""extract_information.extract_info 的单元测试，不发起任何真实模型请求。"""

import json

import pytest

from extract_information import extract_info


# ======================
# 文本工具
# ======================
def test_normalize_text_treats_nan_as_empty():
    assert extract_info.normalize_text(float("nan")) == ""
    assert extract_info.normalize_text(None) == ""
    assert extract_info.normalize_text(" 麻黄 9 克 ") == "麻黄 9 克"


# ======================
# 组成切分与剂量清洗
# ======================
@pytest.mark.parametrize(
    "text, expected",
    [
        ("麻黄9克", "麻黄"),
        ("大枣十二枚", "大枣"),
        ("桂枝9克（去皮）", "桂枝"),
        ("生姜3片", "生姜"),
        ("细辛五分", "细辛"),
        ("三七9克", "三七"),
    ],
)
def test_clean_ingredient_strips_dosage(text, expected):
    assert extract_info.clean_ingredient(text) == expected


def test_extract_ingredients_splits_composition():
    text = "麻黄9克、桂枝6克、杏仁6克、炙甘草3克。"
    assert extract_info.extract_ingredients(text) == ["麻黄", "桂枝", "杏仁", "炙甘草"]


def test_extract_ingredients_drops_noise_segments():
    # 「麻黄汤原方加」是剂型说明，不能当药材；白术才是药材
    assert extract_info.extract_ingredients("麻黄汤原方加、白术12g。") == ["白术"]
    # 半角括号注释同样要清掉
    assert extract_info.extract_ingredients("葛根12克、麻黄9克(去节)") == ["葛根", "麻黄"]


# ======================
# 出处书名
# ======================
@pytest.mark.parametrize(
    "text, expected",
    [
        ("《伤寒论：辨太阳病脉证并治中》", "伤寒论"),
        ("《金匮要略》卷上。", "金匮要略"),
        ("", ""),
        ("无书名号的出处", ""),
    ],
)
def test_extract_source(text, expected):
    assert extract_info.extract_source(text) == expected


# ======================
# 功效短语
# ======================
def test_extract_effect_phrases_formula():
    assert extract_info.extract_effect_phrases("发汗解表，宣肺平喘。") == ["发汗解表", "宣肺平喘"]


def test_extract_effect_phrases_skips_origin_and_indication():
    # 中药材功效列混入植物学来源与主治描述，只保留最后一段真正的功效
    text = (
        "为伞形科植物羌活或宽叶羌活的根茎和根。"
        "用于阳痿遗精，遗尿尿频，腰膝冷痛。"
        "解表散寒，祛风胜湿，止痛。"
    )
    assert extract_info.extract_effect_phrases(text) == ["解表散寒", "祛风胜湿", "止痛"]


# ======================
# 规则抽取
# ======================
def test_parse_herb_row_builds_entity_and_effects():
    row = {
        "药材名称": "羌活",
        "别名": "羌青、退风使者",
        "性味": "辛苦，温。",
        "经脉": "归膀胱经",
        "功效": "解表散寒",
        "主治": "治感冒风寒",
        "用法用量": "煎汤，3~10g",
        "注意禁忌": "血虚痹痛忌服。",
    }
    entities, relations = extract_info.parse_herb_row(row)

    assert entities[0]["type"] == "Herb"
    assert entities[0]["name"] == "羌活"
    assert entities[0]["attributes"]["property_flavor"] == "辛苦，温。"
    assert entities[0]["attributes"]["meridian"] == "归膀胱经"
    assert {"name": "解表散寒", "type": "Effect"} in entities
    assert relations == [
        {
            "subject": "羌活",
            "subject_type": "Herb",
            "relation": "HAS_EFFECT",
            "object": "解表散寒",
            "object_type": "Effect",
        }
    ]


def test_parse_formula_row_builds_ingredients_and_source():
    row = {
        "方剂名称": "麻黄汤",
        "别名": "麻黄解表汤",
        "出处": "《伤寒论：辨太阳病脉证并治中》",
        "组成": "麻黄9克、桂枝6克、杏仁6克、炙甘草3克。",
        "功用": "发汗解表，宣肺平喘。",
        "主治": "外感风寒",
        "用法": "水煎服",
    }
    entities, relations = extract_info.parse_formula_row(row)

    assert entities[0]["type"] == "Formula"
    assert entities[0]["attributes"]["alias"] == "麻黄解表汤"
    # 组成里的药材、出处书名、功效短语都要成为实体
    assert {"name": "炙甘草", "type": "Herb"} in entities
    assert {"name": "伤寒论", "type": "Source"} in entities
    assert {"name": "宣肺平喘", "type": "Effect"} in entities

    relation_keys = {(item["relation"], item["object"]) for item in relations}
    assert ("HAS_INGREDIENT", "麻黄") in relation_keys
    assert ("FROM_SOURCE", "伤寒论") in relation_keys
    assert ("HAS_EFFECT", "发汗解表") in relation_keys


def test_parse_herb_row_without_name_returns_empty():
    assert extract_info.parse_herb_row({"药材名称": ""}) == ([], [])


# ======================
# 大模型抽取（打桩）
# ======================
def test_extract_record_uses_semantic_result(monkeypatch):
    monkeypatch.setattr(
        extract_info,
        "extract_semantic_knowledge",
        lambda text: (["感冒"], ["头痛", "无汗而喘"]),
    )
    record = extract_info.extract_record({"药材名称": "麻黄", "主治": "治感冒风寒，头痛"}, "herb")

    assert record["kind"] == "herb"
    assert {"name": "感冒", "type": "Disease"} in record["entities"]
    assert {"name": "头痛", "type": "Symptom"} in record["entities"]
    relation_keys = {(item["relation"], item["object"]) for item in record["relations"]}
    assert ("TREATS_DISEASE", "感冒") in relation_keys
    assert ("ALLEVIATES_SYMPTOM", "头痛") in relation_keys


def test_extract_record_no_llm_skips_semantic(monkeypatch):
    def fail(text):
        raise AssertionError("--no-llm 时不应调用大模型")

    monkeypatch.setattr(extract_info, "extract_semantic_knowledge", fail)
    record = extract_info.extract_record({"方剂名称": "麻黄汤", "组成": "麻黄9克"}, "formula", use_llm=False)

    assert {"name": "麻黄", "type": "Herb"} in record["entities"]
    assert all(item["relation"] != "TREATS_DISEASE" for item in record["relations"])


def test_clean_term_list_filters_noise():
    # 书名、空白、超长整句、非文本值都要被过滤
    terms = extract_info.clean_term_list(["感冒", "《伤寒论》", " ", "发汗解表是指通过发汗来解除表邪的一类治法", 3])
    assert terms == ["感冒"]


# ======================
# 合并与导出
# ======================
def test_merge_records_merges_attributes_and_dedupes_relations():
    records = [
        {
            "entities": [
                {"name": "麻黄", "type": "Herb", "attributes": {"meridian": "归肺经"}},
                {"name": "感冒", "type": "Disease"},
            ],
            "relations": [
                {
                    "subject": "麻黄", "subject_type": "Herb", "relation": "TREATS_DISEASE",
                    "object": "感冒", "object_type": "Disease",
                }
            ],
        },
        {
            "entities": [
                {"name": "麻黄", "type": "Herb", "attributes": {"meridian": "归膀胱经", "property_flavor": "辛"}},
            ],
            "relations": [
                {
                    "subject": "麻黄", "subject_type": "Herb", "relation": "TREATS_DISEASE",
                    "object": "感冒", "object_type": "Disease",
                }
            ],
        },
    ]
    entities, relations = extract_info.merge_records(records)

    assert len(entities) == 2
    herb = next(item for item in entities if item["name"] == "麻黄")
    # 同一属性先到先得，缺失的属性补齐
    assert herb["attributes"] == {"meridian": "归肺经", "property_flavor": "辛"}
    assert len(relations) == 1


def test_export_graph_writes_split_files(tmp_path):
    records_path = tmp_path / "herb_records.jsonl"
    entities_path = tmp_path / "herb_entities.jsonl"
    relations_path = tmp_path / "herb_relations.jsonl"
    record = {
        "kind": "herb",
        "name": "麻黄",
        "url": "https://example.com/麻黄",
        "entities": [{"name": "麻黄", "type": "Herb"}, {"name": "感冒", "type": "Disease"}],
        "relations": [
            {
                "subject": "麻黄", "subject_type": "Herb", "relation": "TREATS_DISEASE",
                "object": "感冒", "object_type": "Disease",
            }
        ],
    }
    records_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    entity_count, relation_count = extract_info.export_graph(records_path, entities_path, relations_path)

    assert (entity_count, relation_count) == (2, 1)
    assert entities_path.read_text(encoding="utf-8").strip()
    assert json.loads(relations_path.read_text(encoding="utf-8").strip())["relation"] == "TREATS_DISEASE"


def test_load_finished_reads_record_keys(tmp_path):
    records_path = tmp_path / "herb_records.jsonl"
    records_path.write_text(
        json.dumps({"kind": "herb", "name": "麻黄", "url": "u1"}, ensure_ascii=False) + "\n" + "半行坏数据\n",
        encoding="utf-8",
    )

    assert extract_info.load_finished(records_path) == {extract_info.record_key("herb", "麻黄", "u1")}
