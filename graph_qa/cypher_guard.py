# -*- coding: utf-8 -*-
"""Cypher 只读安全校验和输入危险指令检测。"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Set

from graph_qa.models import CypherPlan, ErrorCode
from graph_qa.schema import EVIDENCE_RETURN_ALIASES, GraphSchema, SchemaError

DENIED_KEYWORDS = (
    "CREATE",
    "MERGE",
    "DELETE",
    "DETACH DELETE",
    "SET",
    "REMOVE",
    "DROP",
    "LOAD CSV",
    "FOREACH",
    "CALL",
    "USE",
    "UNION",
)

NODE_LABEL_RE = re.compile(
    r"\(\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*)?:\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
RELATIONSHIP_TYPE_RE = re.compile(
    r"\[\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*)?:\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
TRIPLE_PATTERN_RE = re.compile(
    (
        r"\(\s*(?P<subject_var>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
        r"(?P<subject>[A-Za-z_][A-Za-z0-9_]*)\s*\)"
        r"\s*-\s*\[\s*(?P<relationship_var>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
        r"(?P<relationship>[A-Za-z_][A-Za-z0-9_]*)\s*\]\s*->\s*"
        r"\(\s*(?P<object_var>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
        r"(?P<object>[A-Za-z_][A-Za-z0-9_]*)\s*\)"
    ),
    re.IGNORECASE,
)
PROPERTY_ACCESS_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b"
)
MAP_PROPERTY_RE = re.compile(r"\{[^{}]*?\b([A-Za-z_][A-Za-z0-9_]*)\s*:")
PARAMETER_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
LIMIT_RE = re.compile(r"\bLIMIT\s+([0-9]+)\b", re.IGNORECASE)


class CypherGuardError(ValueError):
    """查询不符合只读或结构约束。"""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedCypher:
    plan: CypherPlan
    limit: int


def _mask_strings_and_reject_comments(query: str) -> str:
    """屏蔽字符串内容，同时拒绝注释、反引号和内嵌分号。"""
    output = []
    index = 0
    quote: Optional[str] = None
    while index < len(query):
        char = query[index]
        next_char = query[index + 1] if index + 1 < len(query) else ""

        if quote is not None:
            output.append(" ")
            if char == "\\":
                if index + 1 < len(query):
                    output.append(" ")
                    index += 2
                    continue
                raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "查询字符串未正确结束")
            if char == quote:
                quote = None
            index += 1
            continue

        if char in ("'", '"'):
            quote = char
            output.append(" ")
            index += 1
            continue

        if char == "`":
            raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "M1 不允许使用反引号标识符")

        if char == "/" and next_char in ("/", "*"):
            raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "M1 不允许在 Cypher 中使用注释")

        output.append(char)
        index += 1

    if quote is not None:
        raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "查询字符串未正确结束")

    masked = "".join(output)
    semicolon_positions = [match.start() for match in re.finditer(";", masked)]
    if len(semicolon_positions) > 1:
        raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "M1 只允许单条 Cypher 查询")
    if semicolon_positions and masked[semicolon_positions[0] + 1 :].strip():
        raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "M1 只允许单条 Cypher 查询")
    if semicolon_positions:
        position = semicolon_positions[0]
        masked = f"{masked[:position]} {masked[position + 1:]}"

    return masked


def _check_unknown_identifiers(masked: str, schema: GraphSchema) -> list[re.Match[str]]:
    labels: Set[str] = set()
    for match in NODE_LABEL_RE.finditer(masked):
        label = match.group(1)
        try:
            schema.assert_label(label)
        except SchemaError as exc:
            raise CypherGuardError(ErrorCode.CYPHER_REJECTED, str(exc)) from exc
        labels.add(label)
    if not labels:
        raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "查询必须明确使用图谱实体标签")

    relationships: Set[str] = set()
    for match in RELATIONSHIP_TYPE_RE.finditer(masked):
        relationship_type = match.group(1)
        try:
            schema.assert_relationship(relationship_type)
        except SchemaError as exc:
            raise CypherGuardError(ErrorCode.CYPHER_REJECTED, str(exc)) from exc
        relationships.add(relationship_type)
    if not relationships:
        raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "查询必须明确使用图谱关系类型")

    patterns = list(TRIPLE_PATTERN_RE.finditer(masked))
    if not patterns:
        raise CypherGuardError(
            ErrorCode.CYPHER_REJECTED,
            "M1 只允许使用形如 (s:Label)-[r:TYPE]->(o:Label) 的显式三元组模式",
        )
    for match in patterns:
        try:
            schema.assert_pattern(
                match.group("subject"),
                match.group("relationship"),
                match.group("object"),
            )
        except SchemaError as exc:
            raise CypherGuardError(ErrorCode.CYPHER_REJECTED, str(exc)) from exc

    for match in PROPERTY_ACCESS_RE.finditer(masked):
        property_name = match.group(1)
        if property_name not in schema.property_keys and property_name != "name":
            raise CypherGuardError(
                ErrorCode.CYPHER_REJECTED,
                f"查询使用了元数据之外的属性: {property_name}",
            )
    return patterns


def _split_return_expressions(return_clause: str) -> list[str]:
    expressions = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0}
    closing = {")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(return_clause):
        if char in depths:
            depths[char] += 1
        elif char in closing:
            opener = closing[char]
            depths[opener] = max(0, depths[opener] - 1)
        elif char == "," and all(depth == 0 for depth in depths.values()):
            expressions.append(return_clause[start:index].strip())
            start = index + 1
    expressions.append(return_clause[start:].strip())
    return [expression for expression in expressions if expression]


def _validate_evidence_expressions(
    masked: str,
    patterns: list[re.Match[str]],
) -> None:
    return_match = re.search(r"\bRETURN\b", masked, re.IGNORECASE)
    limit_matches = list(LIMIT_RE.finditer(masked))
    if return_match is None or not limit_matches:
        raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "查询必须包含 RETURN 和 LIMIT")
    return_clause = masked[return_match.end() : limit_matches[-1].start()]
    bindings: dict[str, str] = {}
    for expression in _split_return_expressions(return_clause):
        alias_match = re.search(
            r"^(?P<expression>.+?)\s+AS\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*$",
            expression,
            re.IGNORECASE | re.DOTALL,
        )
        if alias_match:
            bindings[alias_match.group("alias").lower()] = alias_match.group(
                "expression"
            ).strip()

    missing_aliases = EVIDENCE_RETURN_ALIASES - set(bindings)
    if missing_aliases:
        raise CypherGuardError(
            ErrorCode.CYPHER_REJECTED,
            "查询缺少证据字段: " + ", ".join(sorted(missing_aliases)),
        )

    subject_match = re.search(
        r"\b(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*name\b",
        bindings["subject"],
        re.IGNORECASE,
    )
    subject_label_match = re.search(
        r"\blabels\s*\(\s*(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*\)",
        bindings["subject_label"],
        re.IGNORECASE,
    )
    relationship_match = re.search(
        r"\btype\s*\(\s*(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*\)",
        bindings["relationship"],
        re.IGNORECASE,
    )
    object_match = re.search(
        r"\b(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*name\b",
        bindings["object"],
        re.IGNORECASE,
    )
    object_label_match = re.search(
        r"\blabels\s*\(\s*(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*\)",
        bindings["object_label"],
        re.IGNORECASE,
    )
    expression_matches = (
        subject_match,
        subject_label_match,
        relationship_match,
        object_match,
        object_label_match,
    )
    if not all(expression_matches):
        raise CypherGuardError(
            ErrorCode.CYPHER_REJECTED,
            "证据字段必须来自节点 name、labels(...) 和关系 type(...) 的实际值",
        )

    variable_tuple = (
        subject_match.group("variable"),
        relationship_match.group("variable"),
        object_match.group("variable"),
    )
    for pattern in patterns:
        pattern_tuple = (
            pattern.group("subject_var"),
            pattern.group("relationship_var"),
            pattern.group("object_var"),
        )
        if variable_tuple == pattern_tuple and (
            subject_label_match.group("variable") == subject_match.group("variable")
            and object_label_match.group("variable") == object_match.group("variable")
        ):
            source_expression = bindings["source"]
            if re.search(r"\bNULL\b", source_expression, re.IGNORECASE):
                return
            if re.search(
                r"\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*name\b",
                source_expression,
                re.IGNORECASE,
            ):
                return
    raise CypherGuardError(
        ErrorCode.CYPHER_REJECTED,
        "证据字段没有引用同一条合法三元组路径",
    )
    for match in MAP_PROPERTY_RE.finditer(masked):
        property_name = match.group(1)
        if property_name not in schema.property_keys and property_name != "name":
            raise CypherGuardError(
                ErrorCode.CYPHER_REJECTED,
                f"查询使用了元数据之外的属性: {property_name}",
            )


def detect_unsafe_question(question: str) -> Optional[str]:
    """识别明确要求修改图数据库或执行危险命令的用户输入。"""
    normalized = unicodedata.normalize("NFKC", question).strip().lower()
    chinese_actions = r"(删除|清空|清掉|覆盖|修改|写入|更新|插入|创建)"
    chinese_targets = r"(节点|关系|图谱|数据库|neo4j)"
    if re.search(chinese_actions + r".{0,30}" + chinese_targets, normalized):
        return "请求包含不允许的图数据库写操作"
    if re.search(chinese_targets + r".{0,30}" + chinese_actions, normalized):
        return "请求包含不允许的图数据库写操作"
    if re.search(
        r"\b(detach\s+delete|delete\s+(all\s+)?(nodes?|relationships?)|"
        r"drop\s+(database|constraint|index)|load\s+csv)\b",
        normalized,
    ):
        return "请求包含不允许的 Cypher 写操作"
    if "ignore previous instructions" in normalized and any(
        keyword in normalized for keyword in ("delete", "merge", "create", "drop")
    ):
        return "请求试图绕过只读查询规则"
    return None


class CypherGuard:
    def __init__(self, schema: GraphSchema, max_results: int = 50):
        if max_results < 1:
            raise ValueError("max_results 必须大于等于 1")
        self.schema = schema
        self.max_results = max_results

    def validate(self, plan: CypherPlan) -> ValidatedCypher:
        query = plan.cypher.strip()
        if not query:
            raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "Cypher 不能为空")

        masked = _mask_strings_and_reject_comments(query)
        stripped = masked.strip()
        if not re.match(r"^MATCH\b", stripped, re.IGNORECASE):
            raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "M1 Cypher 必须以 MATCH 开始")
        if not re.search(r"\bRETURN\b", masked, re.IGNORECASE):
            raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "Cypher 必须包含 RETURN")
        if "*" in masked:
            raise CypherGuardError(
                ErrorCode.CYPHER_REJECTED,
                "M1 不允许变量长度路径或其他 * 表达式",
            )
        if re.search(r":\s*\$", masked) or re.search(r"\[\s*\$", masked):
            raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "不允许动态标签或动态属性访问")

        for keyword in DENIED_KEYWORDS:
            pattern = r"\b" + keyword.replace(" ", r"\s+") + r"\b"
            if re.search(pattern, masked, re.IGNORECASE):
                raise CypherGuardError(
                    ErrorCode.CYPHER_REJECTED,
                    f"查询包含被禁止的 Cypher 关键字: {keyword}",
                )

        limit_matches = list(LIMIT_RE.finditer(masked))
        if not limit_matches:
            raise CypherGuardError(ErrorCode.CYPHER_REJECTED, "M1 查询必须包含明确 LIMIT")
        limit = int(limit_matches[-1].group(1))
        if limit < 1 or limit > self.max_results:
            raise CypherGuardError(
                ErrorCode.CYPHER_REJECTED,
                f"LIMIT 必须在 1 到 {self.max_results} 之间",
            )

        referenced_parameters = set(PARAMETER_RE.findall(masked))
        missing_parameters = referenced_parameters - set(plan.parameters)
        if missing_parameters:
            raise CypherGuardError(
                ErrorCode.CYPHER_REJECTED,
                "查询参数未提供: " + ", ".join(sorted(missing_parameters)),
            )

        patterns = _check_unknown_identifiers(masked, self.schema)
        _validate_evidence_expressions(masked, patterns)
        return ValidatedCypher(plan=plan, limit=limit)
