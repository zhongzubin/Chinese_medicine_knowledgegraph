# -*- coding: utf-8 -*-
"""基于图谱名称和别名的可解释实体链接。"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import (
    DefaultDict,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from graph_qa.models import EntityCandidate, LinkedEntity

NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)
QUESTION_SEPARATOR_RE = re.compile(r"[\s,，、;；/]+", re.UNICODE)
NON_WORD_KEEP_SEPARATOR_RE = re.compile(r"[^\w|]+", re.UNICODE)
ALIAS_SPLIT_RE = re.compile(r"[，,、；;/\n]+")
GENERIC_ENTITY_TERMS = frozenset(
    {
        "中药",
        "中草药",
        "药材",
        "方剂",
        "处方",
        "药品",
        "药物",
        "功效",
        "作用",
        "治疗",
        "缓解",
        "症状",
        "疾病",
        "文献",
        "出处",
        "来源",
        "成分",
        "组成",
        "用法",
        "用量",
        "禁忌",
        "注意事项",
        "资料",
        "信息",
        "知识",
        "图谱",
        "数据库",
    }
)
DIRECT_LEFT_BOUNDARY_MARKERS = (
    "请问",
    "查询",
    "查找",
    "关于",
    "针对",
    "治疗",
    "缓解",
    "用于",
    "服用",
    "含有",
    "使用",
    "选择",
    "了解",
    "想知道",
    "对于",
    "帮助",
    "用",
    "含",
    "有",
    "和",
    "与",
    "及",
    "或",
    "把",
    "对",
    "给",
    "让",
    "在",
    "从",
    "由",
    "是",
    "的",
    "问",
    "查",
    "找",
    "知",
    "治",
    "缓",
)
DIRECT_RIGHT_BOUNDARY_MARKERS = (
    "有什么",
    "有哪些",
    "是什么",
    "怎么样",
    "怎么办",
    "可以",
    "能否",
    "是否",
    "用于",
    "用来",
    "出自",
    "来自",
    "组成",
    "治疗",
    "缓解",
    "帮助",
    "属于",
    "包括",
    "所含",
    "含有",
    "性味",
    "归经",
    "功效",
    "主治",
    "用量",
    "剂量",
    "禁忌",
    "用法",
    "产地",
    "炮制",
    "性状",
    "别名",
    "怎么用",
    "怎么吃",
    "有",
    "是",
    "可",
    "能",
    "会",
    "应",
    "该",
    "的",
    "和",
    "与",
    "及",
    "或",
    "由",
    "含",
    "出",
    "来",
    "治",
    "缓",
    "用",
    "吗",
    "呢",
    "哪",
    "什",
    "怎",
    "如",
)
FUZZY_SUFFIX_CHARACTERS = frozenset(
    {
        "草",
        "参",
        "花",
        "叶",
        "根",
        "藤",
        "果",
        "子",
        "仁",
        "皮",
        "枝",
        "木",
        "石",
        "虫",
        "丹",
        "丸",
        "散",
        "膏",
        "汤",
        "饮",
        "片",
        "露",
        "酒",
        "茶",
    }
)
NAMED_ENTITY_SUFFIXES = frozenset(
    set(FUZZY_SUFFIX_CHARACTERS)
    | {"病", "症", "证", "芝", "藻", "菌"}
)
SEMANTIC_NAMED_LABELS = frozenset({"Herb", "Formula", "Source"})


def normalize_entity_text(value: str) -> str:
    """统一全半角、大小写和空白，同时保留中文及字母数字。"""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return NORMALIZE_RE.sub("", normalized)


def normalize_question_boundaries(value: str) -> str:
    """保留列表分隔符，供同一问题中的多实体匹配使用。"""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = QUESTION_SEPARATOR_RE.sub("|", normalized)
    return NON_WORD_KEEP_SEPARATOR_RE.sub("", normalized)


def split_aliases(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in ALIAS_SPLIT_RE.split(value) if part.strip()]


def _has_direct_question_boundary(text: str, start: int, end: int) -> bool:
    """要求实体词位于问题边界，避免长未知词中的短实体被直接命中。"""
    left_ok = (
        start == 0
        or text[start - 1] == "|"
        or any(
            text[:start].endswith(marker)
            for marker in DIRECT_LEFT_BOUNDARY_MARKERS
        )
    )
    right_ok = (
        end == len(text)
        or text[end] == "|"
        or any(
            text[end:].startswith(marker)
            for marker in DIRECT_RIGHT_BOUNDARY_MARKERS
        )
    )
    return left_ok and right_ok


def _semantic_question_text(question: str) -> str:
    normalized = normalize_entity_text(question)
    for term in sorted(GENERIC_ENTITY_TERMS, key=len, reverse=True):
        normalized = normalized.replace(term, "")
    return normalized


def looks_like_named_entity(text: str) -> bool:
    """判断文本是否像一个具体实体名称，而不是同义描述。"""
    normalized = normalize_entity_text(text)
    return (
        2 <= len(normalized) <= 20
        and normalized[-1] in NAMED_ENTITY_SUFFIXES
    )


def question_mentions_named_entity(question: str) -> bool:
    """判断问题中是否出现具体实体名称形态。"""
    normalized = normalize_entity_text(question)
    return any(
        normalized.find(suffix) > 0
        for suffix in NAMED_ENTITY_SUFFIXES
    )


def _is_plausible_fuzzy_variant(candidate: str, term: str) -> bool:
    """拒绝在未知名称中间插入字符后形成的伪模糊命中。"""
    if candidate == term:
        return True
    if len(candidate) == len(term):
        return True
    shorter, longer = sorted((candidate, term), key=len)
    matcher = SequenceMatcher(None, shorter, longer, autojunk=False)
    matched_length = sum(block.size for block in matcher.get_matching_blocks())
    if matched_length != len(shorter):
        return True
    if len(longer) - len(shorter) != 1:
        return False
    return (
        longer.startswith(shorter)
        and longer[-1] in FUZZY_SUFFIX_CHARACTERS
    )


@dataclass(frozen=True)
class EntityRecord:
    label: str
    name: str
    alias: Optional[str] = None


@dataclass(frozen=True)
class EntityIndexEntry:
    label: str
    name: str
    aliases: Tuple[str, ...]

    def terms(self) -> Iterable[Tuple[str, str]]:
        yield self.name, "name"
        for alias in self.aliases:
            yield alias, "alias"


class SemanticMatcher(Protocol):
    def match(self, text: str) -> List[LinkedEntity]:
        ...


class EntityIndex:
    def __init__(
        self,
        entries: Sequence[EntityIndexEntry],
        semantic_index: Optional[SemanticMatcher] = None,
    ):
        self.entries = tuple(entries)
        self.semantic_index = semantic_index
        self._entries_by_label: Dict[str, List[EntityIndexEntry]] = {}
        self._exact_terms: DefaultDict[str, List[Tuple[EntityIndexEntry, str]]] = DefaultDict(list)

        for entry in self.entries:
            self._entries_by_label.setdefault(entry.label, []).append(entry)
            for term, match_type in entry.terms():
                normalized_term = normalize_entity_text(term)
                if normalized_term:
                    self._exact_terms[normalized_term].append((entry, match_type))

    @classmethod
    def from_records(
        cls,
        records: Iterable[EntityRecord],
        semantic_index: Optional[SemanticMatcher] = None,
    ) -> "EntityIndex":
        entries = [
            EntityIndexEntry(
                label=record.label,
                name=record.name,
                aliases=tuple(split_aliases(record.alias)),
            )
            for record in records
            if record.name.strip()
        ]
        return cls(entries, semantic_index=semantic_index)

    def __len__(self) -> int:
        return len(self.entries)

    def _allowed_entries(self, suggested_label: Optional[str]) -> Iterable[EntityIndexEntry]:
        if suggested_label:
            return self._entries_by_label.get(suggested_label, [])
        return self.entries

    def _exact_matches(
        self,
        text: str,
    ) -> List[LinkedEntity]:
        normalized_text = normalize_entity_text(text)
        if not normalized_text:
            return []
        results = []
        for entry, exact_type in self._exact_terms.get(normalized_text, []):
            results.append(
                LinkedEntity(
                    label=entry.label,
                    name=entry.name,
                    matched_text=text,
                    match_type=exact_type,
                    score=1.0,
                )
            )
        return results

    def _direct_question_matches(
        self,
        normalized_question: str,
        suggested_label: Optional[str],
    ) -> List[LinkedEntity]:
        results: List[Tuple[LinkedEntity, str]] = []
        if not normalized_question:
            return []
        for entry in self._allowed_entries(suggested_label):
            for term, match_type in entry.terms():
                normalized_term = normalize_entity_text(term)
                if (
                    len(normalized_term) < 2
                    or normalized_term in GENERIC_ENTITY_TERMS
                ):
                    continue
                start = normalized_question.find(normalized_term)
                while start >= 0:
                    end = start + len(normalized_term)
                    if not _has_direct_question_boundary(
                        normalized_question,
                        start,
                        end,
                    ):
                        start = normalized_question.find(normalized_term, start + 1)
                        continue
                    results.append(
                        (
                            LinkedEntity(
                                label=entry.label,
                                name=entry.name,
                                matched_text=term,
                                match_type=match_type,
                                score=0.96 if match_type == "name" else 0.94,
                            ),
                            normalized_term,
                        )
                    )
                    break
        longest_matches = []
        for item, normalized_term in results:
            if any(
                len(other_term) > len(normalized_term)
                and normalized_term in other_term
                for _other_item, other_term in results
            ):
                continue
            longest_matches.append(item)
        return longest_matches

    def _fuzzy_matches(
        self,
        candidate: EntityCandidate,
        fuzzy_threshold: float,
    ) -> List[LinkedEntity]:
        normalized_candidate = normalize_entity_text(candidate.text)
        if len(normalized_candidate) < 2:
            return []

        results = []
        for entry in self._allowed_entries(candidate.suggested_label):
            best_score = 0.0
            for term, _match_type in entry.terms():
                normalized_term = normalize_entity_text(term)
                if len(normalized_term) < 2:
                    continue
                score = SequenceMatcher(None, normalized_candidate, normalized_term).ratio()
                if (
                    score > best_score
                    and _is_plausible_fuzzy_variant(
                        normalized_candidate,
                        normalized_term,
                    )
                ):
                    best_score = score
            if best_score >= fuzzy_threshold:
                results.append(
                    LinkedEntity(
                        label=entry.label,
                        name=entry.name,
                        matched_text=candidate.text,
                        match_type="fuzzy",
                        score=round(best_score, 4),
                    )
                )
        return results

    def match(
        self,
        candidates: Sequence[EntityCandidate],
        question: str,
        limit_per_label: int = 10,
        fuzzy_threshold: float = 0.72,
    ) -> List[LinkedEntity]:
        """合并模型候选、问句直接命中和模糊匹配，并保持标签隔离。"""
        if limit_per_label < 1:
            raise ValueError("limit_per_label 必须大于等于 1")

        normalized_question = normalize_question_boundaries(question)
        results: List[LinkedEntity] = []
        linkable_candidates = [
            candidate
            for candidate in candidates
            if normalize_entity_text(candidate.text) not in GENERIC_ENTITY_TERMS
        ]
        if candidates and not linkable_candidates:
            return []

        for candidate in linkable_candidates:
            result_count_before_candidate = len(results)
            exact_matches = self._exact_matches(candidate.text)
            if candidate.suggested_label:
                exact_matches.sort(
                    key=lambda item: (
                        item.label != candidate.suggested_label,
                        -item.score,
                    )
                )
            results.extend(exact_matches)
            if len(results) == result_count_before_candidate:
                fuzzy_matches = self._fuzzy_matches(candidate, fuzzy_threshold)
                if fuzzy_matches:
                    results.extend(fuzzy_matches)
                elif self.semantic_index is not None:
                    results.extend(
                        self._semantic_matches_for_candidate(
                            candidate.text,
                            self.semantic_index.match(candidate.text),
                        )
                    )

        if not results:
            results.extend(self._direct_question_matches(normalized_question, None))
        if not results and self.semantic_index is not None:
            semantic_question = _semantic_question_text(question)
            if semantic_question:
                semantic_matches = self.semantic_index.match(
                    semantic_question
                )
                if question_mentions_named_entity(question):
                    semantic_matches = [
                        item
                        for item in semantic_matches
                        if item.label not in SEMANTIC_NAMED_LABELS
                    ]
                results.extend(semantic_matches)

        return self._finalize(results, limit_per_label)

    @staticmethod
    def _semantic_matches_for_candidate(
        text: str,
        matches: Sequence[LinkedEntity],
    ) -> List[LinkedEntity]:
        if not looks_like_named_entity(text):
            return list(matches)
        return [
            item
            for item in matches
            if item.label not in SEMANTIC_NAMED_LABELS
        ]

    def match_exact(
        self,
        question: str,
        limit_per_label: int = 10,
    ) -> List[LinkedEntity]:
        """只执行问题中的名称和别名精确匹配，供模型调用前预匹配使用。"""
        if limit_per_label < 1:
            raise ValueError("limit_per_label 必须大于等于 1")
        results = self._direct_question_matches(
            normalize_question_boundaries(question),
            None,
        )
        return self._finalize(results, limit_per_label)

    @staticmethod
    def _finalize(
        results: Sequence[LinkedEntity],
        limit_per_label: int,
    ) -> List[LinkedEntity]:
        best_by_key: Dict[Tuple[str, str], LinkedEntity] = {}
        for result in results:
            key = (result.label, result.name)
            existing = best_by_key.get(key)
            if existing is None or result.score > existing.score:
                best_by_key[key] = result

        per_label_counts: DefaultDict[str, int] = DefaultDict(int)
        ordered = []
        for result in sorted(
            best_by_key.values(),
            key=lambda item: (-item.score, item.label, item.name),
        ):
            if per_label_counts[result.label] >= limit_per_label:
                continue
            per_label_counts[result.label] += 1
            ordered.append(result)
        return ordered
