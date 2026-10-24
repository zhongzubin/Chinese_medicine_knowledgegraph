# -*- coding: utf-8 -*-
"""从爬取到的中药、方剂数据中抽取中医知识图谱实体与关系。

数据来源: crawler/data/中药大全.xlsx、crawler/data/方剂大全.xlsx
输出目录: output/（中药材与方剂分开存放，各自输出实体、关系文件）

用法:
    uv run python -m extract_information.extract_info --source all
    uv run python -m extract_information.extract_info --source herbs --limit 3
    uv run python -m extract_information.extract_info --source formulas --no-llm
    uv run python -m extract_information.extract_info --source herbs --refresh

抽取策略:
    1. 规则抽取：表格里已成列的字段（名称/别名/性味/归经/组成/出处等）直接映射，
       零成本、结果可复现；
    2. 大模型抽取：只把「主治」这类长文本交给模型，识别疾病与症状，
       关系的头尾实体由程序确定，避免模型编造。

代码结构（建议按此顺序阅读）:
    1. 数据结构    EntityType / RelationType / 属性模型 / Entity / Relation
    2. 文本工具    normalize_text / clip / dedupe
    3. 规则抽取    clean_ingredient / extract_ingredients / extract_source
                   / extract_effect_phrases / parse_herb_row / parse_formula_row
    4. 模型抽取    SEMANTIC_PROMPT / extract_semantic_knowledge
    5. 单条组装    extract_record
    6. 断点续跑    load_finished / append_jsonl / append_failed
    7. 合并导出    merge_records / export_graph
    8. 主流程      parse_args / run_source / main
"""

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import pandas as pd
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel, Field
from tqdm import tqdm

from common.llm import my_llm

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = PROJECT_ROOT / "crawler" / "data"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"

HERB_XLSX = DATA_DIR / "中药大全.xlsx"
FORMULA_XLSX = DATA_DIR / "方剂大全.xlsx"

# 单条「主治」送给模型前的长度上限，超长部分截断，避免上下文过长
SEMANTIC_TEXT_LIMIT = 1200
# 模型调用失败时的重试次数与等待秒数
MAX_RETRIES = 2
RETRY_DELAY = 3.0

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 组成里的括号注释，如「桂枝9克（去皮）」
PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
# 组成里的剂量：阿拉伯数字（9克 / 12g）或中文数字加单位（十二枚 / 五分）
DOSAGE_CUT_RE = re.compile(
    r"[0-9０-９][0-9０-９.．]*"
    r"|[零一二三四五六七八九十百半]+(?=[克两钱分斤升枚片个粒只具条握把寸])"
)
# 从「《伤寒论：辨太阳病脉证并治中》」里取出书名
BOOK_RE = re.compile(r"《([^》]{1,40})》")
# 分句 / 分词用的标点
CLAUSE_SPLIT_RE = re.compile(r"[。\n]")
ITEM_SPLIT_RE = re.compile(r"[，,、；;]")

# 中药材「功效」列混入了大量植物学描述，带这些前缀或关键词的句子不是功效
NOISE_PREFIXES = ("为", "本品", "用于", "主", "治", "含", "《", "【")
NOISE_KEYWORDS = ("科植物", "全草", "根茎", "干燥", "生境", "分布", "药用部位")
# 组成里误切出来的剂型词、操作词，不应被当成药材名
INGREDIENT_BAN_CHARS = "加减汤散丸膏丹饮"

# 表格列 -> 实体属性名
HERB_FIELD_MAP = {
    "alias": "别名",
    "origin": "来源",
    "property_flavor": "性味",
    "processing": "炮制",
    "traits": "性状",
    "effect": "功效",
    "meridian": "经脉",
    "indication": "主治",
    "dosage": "用法用量",
    "taboo": "注意禁忌",
}

FORMULA_FIELD_MAP = {
    "alias": "别名",
    "effect": "功用",
    "indication": "主治",
    "taboo": "注意",
    "usage": "用法",
}


# ======================
# 数据结构
# ======================
EntityType = Literal["Symptom", "Disease", "Formula", "Herb", "Effect", "Source"]

RelationType = Literal[
    "TREATS_DISEASE",
    "ALLEVIATES_SYMPTOM",
    "HAS_EFFECT",
    "HAS_INGREDIENT",
    "HAS_SYMPTOM",
    "FROM_SOURCE",
]


class FormulaAttributes(BaseModel):
    alias: Optional[str] = Field(default=None, description="方剂别名")
    effect: Optional[str] = Field(default=None, description="方剂功效")
    indication: Optional[str] = Field(default=None, description="方剂主治")
    taboo: Optional[str] = Field(default=None, description="方剂禁忌")
    usage: Optional[str] = Field(default=None, description="方剂用法用量")


class HerbAttributes(BaseModel):
    alias: Optional[str] = Field(default=None, description="药材别名")
    dosage: Optional[str] = Field(default=None, description="药材用法用量")
    effect: Optional[str] = Field(default=None, description="药材功效")
    indication: Optional[str] = Field(default=None, description="药材主治")
    meridian: Optional[str] = Field(default=None, description="药材归经")
    origin: Optional[str] = Field(default=None, description="药材来源")
    place: Optional[str] = Field(default=None, description="药材产地或生境分布")
    processing: Optional[str] = Field(default=None, description="药材炮制方法")
    property_flavor: Optional[str] = Field(default=None, description="药材性味")
    taboo: Optional[str] = Field(default=None, description="药材禁忌")
    traits: Optional[str] = Field(default=None, description="药材性状特征")


class Entity(BaseModel):
    name: str = Field(description="实体名称")
    type: EntityType = Field(description="实体类型（只能从给定集合中选择）")
    attributes: Optional[Dict[str, Any]] = Field(default=None, description="实体属性")


class Relation(BaseModel):
    subject: str = Field(description="关系主体实体名称")
    subject_type: EntityType = Field(description="关系主体实体类型（只能从给定集合中选择）")
    relation: RelationType = Field(description="关系类型（只能从给定集合中选择）")
    object: str = Field(description="关系客体实体名称")
    object_type: EntityType = Field(description="关系客体实体类型（只能从给定集合中选择）")


class SemanticExtraction(BaseModel):
    """大模型只负责识别疾病与症状，关系的头尾实体由程序补全。"""

    diseases: List[str] = Field(default_factory=list, description="文本中明确出现的疾病名称")
    symptoms: List[str] = Field(default_factory=list, description="文本中明确出现的症状名称")


# ======================
# 文本工具
# ======================
def normalize_text(value: Any) -> str:
    """把单元格值转成干净文本：nan 归空，压缩空白，中文间的排版空格去掉。"""
    if value is None:
        return ""
    text = str(value)
    if text.strip().lower() in ("nan", "none"):
        return ""
    # 全角空格、零宽字符先换成普通空格
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    # 去掉不可见控制字符
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    # 连续空白合并成一个空格
    text = re.sub(r"\s+", " ", text).strip()
    # 中文与中文之间的空格是排版造成的，直接去掉
    text = re.sub(
        r"(?<=[\u4e00-\u9fff、，。；：（）「」“”‘’])\s+(?=[\u4e00-\u9fff、，。；：（）「」“”‘’])",
        "",
        text,
    )
    return text


def clip(text: str, limit: int = SEMANTIC_TEXT_LIMIT) -> str:
    """按字数截断长文本，保留前段核心内容。"""
    if len(text) <= limit:
        return text
    return text[:limit]


def dedupe(items: List[str]) -> List[str]:
    """去重并保留原有顺序。"""
    seen = set()
    ordered = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


# ======================
# 规则抽取
# ======================
def clean_ingredient(segment: str) -> str:
    """把「桂枝9克（去皮）」清洗成药名「桂枝」。"""
    text = PAREN_RE.sub("", segment)
    text = normalize_text(text)
    # 从第一个剂量处截断，后面的克数、枚数都不要
    text = DOSAGE_CUT_RE.split(text)[0]
    return text.strip(" 、,，;；。.")


def is_valid_ingredient(name: str) -> bool:
    """过滤组成里切出来的非药名碎片（剂型词、操作词、单字残片）。"""
    if not 2 <= len(name) <= 6:
        return False
    if not CJK_RE.search(name):
        return False
    return not any(char in name for char in INGREDIENT_BAN_CHARS)


def extract_ingredients(text: Any) -> List[str]:
    """从方剂「组成」列切出药材名列表。"""
    names = []
    for segment in ITEM_SPLIT_RE.split(normalize_text(text)):
        name = clean_ingredient(segment)
        if is_valid_ingredient(name):
            names.append(name)
    return dedupe(names)


def extract_source(text: Any) -> str:
    """从「出处」列取出书名，如《伤寒论：辨太阳病脉证并治中》-> 伤寒论。"""
    matched = BOOK_RE.search(normalize_text(text))
    if not matched:
        return ""
    # 书名号里可能带篇目，按冒号、间隔号切开只留书名
    book = re.split(r"[：:·]", matched.group(1))[0].strip()
    return book


def extract_effect_phrases(text: Any) -> List[str]:
    """从「功效/功用」列切出功效短语，并过滤掉来源、主治等混入的句子。"""
    phrases = []
    for clause in CLAUSE_SPLIT_RE.split(normalize_text(text)):
        clause = clause.strip()
        if not clause:
            continue
        # 整句是植物学来源描述、主治描述的直接跳过
        if clause.startswith(NOISE_PREFIXES):
            continue
        if any(keyword in clause for keyword in NOISE_KEYWORDS):
            continue
        for phrase in ITEM_SPLIT_RE.split(clause):
            phrase = phrase.strip(" 。；;：:")
            if not 2 <= len(phrase) <= 10:
                continue
            if not CJK_RE.search(phrase):
                continue
            # 仍带「治/用于」的是主治，不是功效
            if any(char in phrase for char in "治用于科"):
                continue
            phrases.append(phrase)
    return dedupe(phrases)


def make_entity(name: str, entity_type: EntityType, attributes: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """构造实体字典，并借 Pydantic 校验实体类型合法。"""
    return Entity(name=name, type=entity_type, attributes=attributes or None).model_dump(exclude_none=True)


def make_relation(
    subject: str,
    subject_type: EntityType,
    relation: RelationType,
    obj: str,
    object_type: EntityType,
) -> Dict[str, Any]:
    """构造关系字典，并借 Pydantic 校验类型、关系名合法。"""
    return Relation(
        subject=subject,
        subject_type=subject_type,
        relation=relation,
        object=obj,
        object_type=object_type,
    ).model_dump()


def build_attributes(row: Dict[str, Any], field_map: Dict[str, str], model: type) -> Dict[str, str]:
    """按「属性名 -> 表格列」映射取非空值，再用属性模型规整一遍。"""
    values = {}
    for field, column in field_map.items():
        value = normalize_text(row.get(column))
        if value:
            values[field] = value
    return model(**values).model_dump(exclude_none=True)


def parse_herb_row(row: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """规则部分：单味中药材 -> 实体与关系。"""
    name = normalize_text(row.get("药材名称"))
    if not name:
        return [], []

    entities = [make_entity(name, "Herb", build_attributes(row, HERB_FIELD_MAP, HerbAttributes))]
    relations = []
    for effect in extract_effect_phrases(row.get("功效")):
        entities.append(make_entity(effect, "Effect"))
        relations.append(make_relation(name, "Herb", "HAS_EFFECT", effect, "Effect"))
    return entities, relations


def parse_formula_row(row: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """规则部分：单首方剂 -> 实体与关系。"""
    name = normalize_text(row.get("方剂名称"))
    if not name:
        return [], []

    entities = [make_entity(name, "Formula", build_attributes(row, FORMULA_FIELD_MAP, FormulaAttributes))]
    relations = []

    # 组成 -> 方剂包含药材
    for ingredient in extract_ingredients(row.get("组成")):
        entities.append(make_entity(ingredient, "Herb"))
        relations.append(make_relation(name, "Formula", "HAS_INGREDIENT", ingredient, "Herb"))

    # 出处 -> 方剂出自某文献
    source = extract_source(row.get("出处"))
    if source:
        entities.append(make_entity(source, "Source"))
        relations.append(make_relation(name, "Formula", "FROM_SOURCE", source, "Source"))

    # 功用 -> 功效
    for effect in extract_effect_phrases(row.get("功用")):
        entities.append(make_entity(effect, "Effect"))
        relations.append(make_relation(name, "Formula", "HAS_EFFECT", effect, "Effect"))

    return entities, relations


# ======================
# 大模型抽取
# ======================
parser = JsonOutputParser(pydantic_object=SemanticExtraction)

SEMANTIC_PROMPT = PromptTemplate(
    template=(
        "你是一名中医文献信息抽取助手。请从给定的「主治」文本中识别疾病与症状。\n\n"
        "【疾病 Disease】\n"
        "- 可以独立成立的病名、病证，如：感冒、肺痈、痢疾、消渴、痹证、水肿、痈疽\n\n"
        "【症状 Symptom】\n"
        "- 患者的主观感受或客观体征，如：头痛、恶寒发热、咳嗽、无汗而喘、脉浮紧、舌苔薄白\n\n"
        "【必须忽略】\n"
        "- 治法与功效术语：发汗解表、祛风胜湿、活血化瘀、宣肺平喘\n"
        "- 书名或文献：《本经》《伤寒论》《金匮要略》\n"
        "- 方剂名：麻黄汤、桂枝汤\n"
        "- 药材名：人参、黄芪\n\n"
        "【要求】\n"
        "1. 只保留文本中明确出现的词，不要自行补充、推断或改写；\n"
        "2. 沿用原文用词，不要翻译成现代医学病名；\n"
        "3. 无法判断是疾病还是症状的，直接丢弃；\n"
        "4. 同一个词不要同时出现在疾病和症状里；\n"
        "5. 没有识别到内容时返回空列表。\n\n"
        "严格按以下 JSON 格式输出：\n"
        "{format_instructions}\n\n"
        "输入文本：{text}"
    ),
    input_variables=["text"],
    partial_variables={"format_instructions": parser.get_format_instructions()},
)

semantic_chain = SEMANTIC_PROMPT | my_llm | parser


def clean_term_list(values: Any, limit: int = 12) -> List[str]:
    """清理模型返回的词表：去空、去标点、去书名号、限制长度并去重。"""
    if not isinstance(values, list):
        return []
    cleaned = []
    for value in values:
        raw = normalize_text(value)
        # 书名、文献不是疾病或症状，整条丢掉
        if "《" in raw or "》" in raw:
            continue
        term = raw.strip(" 。，、；;：:“”‘’")
        if not term or len(term) > limit or not CJK_RE.search(term):
            continue
        cleaned.append(term)
    return dedupe(cleaned)


def extract_semantic_knowledge(text: Any) -> Tuple[List[str], List[str]]:
    """调用大模型，从主治文本中识别 (疾病列表, 症状列表)。"""
    content = clip(normalize_text(text))
    if not content:
        return [], []

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = semantic_chain.invoke({"text": content})
            diseases = clean_term_list(result.get("diseases"))
            symptoms = clean_term_list(result.get("symptoms"))
            # 疾病与症状互斥，避免同一个词两边都出现
            symptoms = [item for item in symptoms if item not in diseases]
            return diseases, symptoms
        except Exception as exc:  # 网络抖动、模型输出不合规都走重试
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"大模型抽取失败: {last_error}")


# ======================
# 单条组装
# ======================
def extract_record(row: Dict[str, Any], kind: str, use_llm: bool = True) -> Dict[str, Any]:
    """抽取一条中药材 / 方剂数据，返回可直接落盘的记录。"""
    if kind == "herb":
        name = normalize_text(row.get("药材名称"))
        entities, relations = parse_herb_row(row)
        subject_type: EntityType = "Herb"
    else:
        name = normalize_text(row.get("方剂名称"))
        entities, relations = parse_formula_row(row)
        subject_type = "Formula"

    # 主治文本 -> 疾病 / 症状
    if use_llm and name:
        diseases, symptoms = extract_semantic_knowledge(row.get("主治"))
        for disease in diseases:
            entities.append(make_entity(disease, "Disease"))
            relations.append(make_relation(name, subject_type, "TREATS_DISEASE", disease, "Disease"))
        for symptom in symptoms:
            entities.append(make_entity(symptom, "Symptom"))
            relations.append(make_relation(name, subject_type, "ALLEVIATES_SYMPTOM", symptom, "Symptom"))

    return {
        "kind": kind,
        "name": name,
        "url": normalize_text(row.get("详情页链接")),
        "entities": entities,
        "relations": relations,
        "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ======================
# 断点续跑
# ======================
def record_key(kind: str, name: str, url: str) -> str:
    """断点续跑的判重键：类型 + 名称 + 详情页链接。"""
    return f"{kind}|{name}|{url}"


def load_finished(path: Path) -> set:
    """读取已完成记录的键，用于跳过已抽取的条目。"""
    finished = set()
    if not path.exists():
        return finished
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # 上一轮异常退出可能留下半行，跳过即可
                continue
            finished.add(record_key(record.get("kind", ""), record.get("name", ""), record.get("url", "")))
    return finished


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """每条结果立即追加落盘，中断后可续跑。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
        file.flush()


def append_failed(path: Path, kind: str, name: str, url: str, reason: str) -> None:
    """记录抽取失败的条目，便于后续补抽。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{kind}\t{name}\t{url}\t{reason}\n")
        file.flush()


# ======================
# 合并导出
# ======================
def merge_records(records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把逐条结果合并成全局实体表与关系表，同名实体合并属性、关系去重。"""
    entities: Dict[Tuple[str, str], Dict[str, Any]] = {}
    relations: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    for record in records:
        for entity in record.get("entities") or []:
            key = (entity["type"], entity["name"])
            merged = entities.setdefault(key, {"name": entity["name"], "type": entity["type"]})
            attributes = entity.get("attributes") or {}
            if attributes:
                existing = merged.setdefault("attributes", {})
                for field, value in attributes.items():
                    # 先出现的非空值优先，保证同一字段只保留一份内容
                    existing.setdefault(field, value)

        for relation in record.get("relations") or []:
            key = (relation["subject"], relation["relation"], relation["object"])
            relations.setdefault(key, relation)

    return list(entities.values()), list(relations.values())


def export_graph(records_path: Path, entities_path: Path, relations_path: Path) -> Tuple[int, int]:
    """从逐条记录文件重新生成实体表与关系表。"""
    if not records_path.exists():
        return 0, 0

    # 同一个键可能因重跑写了多条，以最后一条为准
    latest = {}
    with records_path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            latest[record_key(record.get("kind", ""), record.get("name", ""), record.get("url", ""))] = record

    entities, relations = merge_records(list(latest.values()))
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    with entities_path.open("w", encoding="utf-8") as file:
        for entity in sorted(entities, key=lambda item: (item["type"], item["name"])):
            file.write(json.dumps(entity, ensure_ascii=False) + "\n")
    with relations_path.open("w", encoding="utf-8") as file:
        for relation in sorted(relations, key=lambda item: (item["subject"], item["relation"], item["object"])):
            file.write(json.dumps(relation, ensure_ascii=False) + "\n")
    return len(entities), len(relations)


# ======================
# 主流程
# ======================
# 数据源 -> 表格文件、记录文件、实体文件、关系文件
SOURCE_FILES = {
    "herb": {"xlsx": HERB_XLSX, "prefix": "herb", "label": "中药材"},
    "formula": {"xlsx": FORMULA_XLSX, "prefix": "formula", "label": "方剂"},
}


def build_paths(output_dir: Path) -> Dict[str, Dict[str, Path]]:
    """按输出目录生成两类数据各自的文件路径。"""
    paths = {}
    for kind, config in SOURCE_FILES.items():
        prefix = config["prefix"]
        paths[kind] = {
            "records": output_dir / f"{prefix}_records.jsonl",
            "entities": output_dir / f"{prefix}_entities.jsonl",
            "relations": output_dir / f"{prefix}_relations.jsonl",
        }
    return paths


def parse_args() -> argparse.Namespace:
    argparse_parser = argparse.ArgumentParser(description="抽取中药、方剂知识图谱实体与关系")
    argparse_parser.add_argument("--source", choices=("herbs", "formulas", "all"), default="all", help="抽取哪部分数据")
    argparse_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="输出目录")
    argparse_parser.add_argument("--limit", type=int, default=0, help="每类只抽取前 N 条（0 表示全部），用于试跑")
    argparse_parser.add_argument("--workers", type=int, default=4, help="大模型并发线程数")
    argparse_parser.add_argument("--no-llm", action="store_true", help="只做规则抽取，不调用大模型")
    argparse_parser.add_argument("--refresh", action="store_true", help="忽略已有结果，重新抽取")
    args = argparse_parser.parse_args()

    if args.workers < 1:
        argparse_parser.error("--workers 必须大于等于 1")
    if args.workers > 8:
        argparse_parser.error("--workers 过大会给模型接口造成压力，请设置为不大于 8")
    return args


def run_source(kind: str, args: argparse.Namespace, paths: Dict[str, Path]) -> Tuple[int, int, int]:
    """抽取一类数据，返回 (本次新增条数, 失败条数, 实体数)。"""
    config = SOURCE_FILES[kind]
    label = config["label"]
    xlsx = config["xlsx"]
    if not xlsx.exists():
        print(f"[跳过] 未找到表格: {xlsx}")
        return 0, 0, 0

    dataframe = pd.read_excel(xlsx).fillna("")
    rows = dataframe.to_dict("records")
    if args.limit > 0:
        rows = rows[: args.limit]

    # --refresh 时清掉旧结果，从头抽取
    if args.refresh:
        for path in paths[kind].values():
            if path.exists():
                path.unlink()
        print(f"      已按 --refresh 清空 {label} 旧结果")

    finished = load_finished(paths[kind]["records"])
    pending = [
        row for row in rows
        if record_key(kind, normalize_text(row.get("药材名称" if kind == "herb" else "方剂名称")),
                      normalize_text(row.get("详情页链接"))) not in finished
    ]
    print(f"[{label}] 共 {len(rows)} 条，已完成 {len(rows) - len(pending)} 条，本次待抽取 {len(pending)} 条")
    if not pending:
        entities, relations = export_graph(paths[kind]["records"], paths[kind]["entities"], paths[kind]["relations"])
        print(f"      合并结果: 实体 {entities} 个，关系 {relations} 条")
        return 0, 0, entities

    failed = 0
    bar = tqdm(total=len(pending), desc=f"{label}抽取", unit="条", ncols=100, dynamic_ncols=True)
    # 大模型调用是主要耗时，用线程池并发；写文件统一在主线程做，避免并发写乱序
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(extract_record, row, kind, not args.no_llm): row
            for row in pending
        }
        for future in as_completed(futures):
            row = futures[future]
            name = normalize_text(row.get("药材名称" if kind == "herb" else "方剂名称"))
            try:
                record = future.result()
                append_jsonl(paths[kind]["records"], record)
            except Exception as exc:  # 单条失败不影响整体
                failed += 1
                append_failed(args.output_dir / "extract_failed.txt", kind, name,
                              normalize_text(row.get("详情页链接")), str(exc))
                bar.write(f"[失败] {name} -> {exc}")
            bar.update(1)
            bar.set_postfix_str(name)
    bar.close()

    entities, relations = export_graph(paths[kind]["records"], paths[kind]["entities"], paths[kind]["relations"])
    print(f"      合并结果: 实体 {entities} 个，关系 {relations} 条")
    return len(pending) - failed, failed, entities


def main() -> None:
    args = parse_args()
    started_at = time.time()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    paths = build_paths(args.output_dir)

    kinds = ["herb", "formula"] if args.source == "all" else ["herb" if args.source == "herbs" else "formula"]
    total_failed = 0
    for kind in kinds:
        _, failed, _ = run_source(kind, args, paths)
        total_failed += failed

    print("-" * 60)
    print(f"输出目录: {args.output_dir}")
    for kind in kinds:
        config = SOURCE_FILES[kind]
        print(
            f"{config['label']}: {paths[kind]['records'].name} / "
            f"{paths[kind]['entities'].name} / {paths[kind]['relations'].name}"
        )
    if total_failed:
        print(f"失败记录: {args.output_dir / 'extract_failed.txt'}（再次运行会自动补抽）")
    print(f"耗时: {(time.time() - started_at) / 60:.1f} 分钟")


if __name__ == "__main__":
    main()
