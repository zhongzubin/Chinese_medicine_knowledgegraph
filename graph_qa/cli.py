# -*- coding: utf-8 -*-
"""知识图谱问答命令行入口。"""

import argparse
import json
import os
from pathlib import Path
from typing import Callable, Optional, Tuple

from neo4j import GraphDatabase

from common.config import Config
from graph_qa.cypher_guard import CypherGuard
from graph_qa.entity_linker import EntityIndex
from graph_qa.llm import LangChainQAReasoner
from graph_qa.models import ErrorCode, GraphQAResponse
from graph_qa.neo4j_repository import Neo4jRepository, RepositoryError
from graph_qa.schema import DEFAULT_METADATA_PATH, GraphSchema, SchemaError
from graph_qa.workflow import GraphQAEngine

EngineFactory = Callable[[Optional[str], int], GraphQAEngine]
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCAL_EMBEDDING_MODEL = (
    PROJECT_ROOT / "models" / "bge-large-zh-v1.5"
)
FALLBACK_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"


class CLIConfigError(RuntimeError):
    pass


def _read_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise CLIConfigError(f"{name} 必须是数字") from exc
    if value <= 0:
        raise CLIConfigError(f"{name} 必须大于 0")
    return value


def _read_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise CLIConfigError(f"{name} 必须是整数") from exc
    if value < 1:
        raise CLIConfigError(f"{name} 必须大于等于 1")
    return value


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise CLIConfigError(f"{name} 必须是 true 或 false")


def _read_probability(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise CLIConfigError(f"{name} 必须是数字") from exc
    if not 0 <= value <= 1:
        raise CLIConfigError(f"{name} 必须在 0 到 1 之间")
    return value


def _resolve_embedding_model_reference(value: Optional[str] = None) -> str:
    raw = (value or "").strip()
    if raw:
        model_path = Path(raw).expanduser()
        if not model_path.is_absolute():
            project_relative = PROJECT_ROOT / model_path
            if project_relative.exists():
                return str(project_relative.resolve())
        if model_path.exists():
            return str(model_path.resolve())
        return raw
    if DEFAULT_LOCAL_EMBEDDING_MODEL.exists():
        return str(DEFAULT_LOCAL_EMBEDDING_MODEL.resolve())
    return FALLBACK_EMBEDDING_MODEL


def _build_entity_index(repository: Neo4jRepository):
    entity_index = repository.load_entity_index()
    if not _read_bool("GRAPH_QA_VECTOR_ENABLED", False):
        return entity_index

    from graph_qa.semantic_index import (
        SemanticEntityIndex,
        SemanticIndexError,
        SentenceTransformerEncoder,
    )

    model_name = _resolve_embedding_model_reference(
        os.getenv("GRAPH_QA_EMBEDDING_MODEL")
    )
    top_k = _read_positive_int("GRAPH_QA_VECTOR_TOP_K", 3)
    threshold = _read_probability("GRAPH_QA_VECTOR_THRESHOLD", 0.56)
    try:
        semantic_index = SemanticEntityIndex.load_or_build(
            entity_index.entries,
            SentenceTransformerEncoder(model_name),
            cache_dir=PROJECT_ROOT / ".cache" / "graph_qa" / "faiss",
            cache_identity=model_name,
            top_k=top_k,
            threshold=threshold,
        )
    except SemanticIndexError as exc:
        raise CLIConfigError(f"实体向量索引不可用: {exc}") from exc
    return EntityIndex(entity_index.entries, semantic_index=semantic_index)


def build_engine_from_environment(
    database: Optional[str] = None,
    max_results: int = 50,
    metadata_path: Path = DEFAULT_METADATA_PATH,
) -> GraphQAEngine:
    """构建真实模型、Neo4j 和可选 FAISS 依赖。"""
    config = Config()
    missing = [
        name
        for name, value in {
            "NEO4J_URI": config.NEO4J_URI,
            "NEO4J_USER": config.NEO4J_USER,
            "NEO4J_PASSWORD": config.NEO4J_PASSWORD,
        }.items()
        if not value
    ]
    if missing:
        raise CLIConfigError(f"缺少 Neo4j 配置: {', '.join(missing)}")

    try:
        schema = GraphSchema.from_file(metadata_path)
    except SchemaError as exc:
        raise CLIConfigError(f"图元数据不可用: {exc}") from exc

    query_timeout = _read_positive_float("GRAPH_QA_QUERY_TIMEOUT_SECONDS", 8.0)
    query_attempts = _read_positive_int("GRAPH_QA_CYPHER_MAX_ATTEMPTS", 3)
    entity_limit = _read_positive_int("GRAPH_QA_ENTITY_LIMIT", 10)

    try:
        reasoner = LangChainQAReasoner()
    except Exception as exc:
        raise CLIConfigError("模型配置不完整或模型客户端初始化失败") from exc

    driver = GraphDatabase.driver(
        config.NEO4J_URI,
        auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
    )
    guard = CypherGuard(schema, max_results=max_results)
    repository = Neo4jRepository(
        driver=driver,
        schema=schema,
        guard=guard,
        database=database,
        query_timeout_seconds=query_timeout,
        max_results=max_results,
    )
    try:
        entity_index = _build_entity_index(repository)
        return GraphQAEngine(
            reasoner=reasoner,
            repository=repository,
            schema=schema,
            entity_index=entity_index,
            max_attempts=query_attempts,
            entity_limit=entity_limit,
        )
    except RepositoryError:
        driver.close()
        raise
    except Exception:
        driver.close()
        raise


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="中医知识图谱只读问答")
    parser.add_argument("--question", required=True, help="要查询的中医知识问题")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="输出格式",
    )
    parser.add_argument("--show-query", action="store_true", help="文本模式显示 Cypher")
    parser.add_argument("--database", help="Neo4j 数据库名称")
    parser.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="单次查询允许的最大记录数",
    )
    args = parser.parse_args(argv)
    if args.max_results < 1:
        parser.error("--max-results 必须大于等于 1")
    return args


def render_text(response: GraphQAResponse, show_query: bool = False) -> str:
    lines = [response.answer]
    if response.facts:
        lines.extend(["", "图谱属性："])
        for item in response.facts:
            source = f"（来源：{item.source}）" if item.source else "（未提供出处）"
            lines.append(
                f"- ({item.subject}:{item.subject_label}) "
                f"{item.attribute_label}：{item.value}{source}"
            )
    if response.aggregates:
        lines.extend(["", "图谱统计："])
        for item in response.aggregates:
            lines.append(f"- {item.description}：{item.count}")
    if response.paths:
        lines.extend(["", "多跳路径："])
        for item in response.paths:
            source = f"（来源：{item.source}）" if item.source else ""
            lines.append(
                f"- ({item.root}:{item.root_label})"
                f"-[{item.first_relationship}]->"
                f"({item.intermediate}:{item.intermediate_label})"
                f"-[{item.second_relationship}]->"
                f"({item.target}:{item.target_label}){source}"
            )
    if response.evidence:
        lines.extend(["", "图谱证据："])
        for item in response.evidence:
            source = f"（来源：{item.source}）" if item.source else ""
            lines.append(
                f"- ({item.subject}:{item.subject_label})"
                f"-[{item.relationship}]->"
                f"({item.object}:{item.object_label}){source}"
            )
    if response.verification:
        status = (
            "已通过证据支持校验"
            if response.verification.status == "passed"
            else "已回退为证据化回答"
        )
        lines.extend(
            [
                "",
                "回答校验：",
                (
                    f"- {status}；覆盖 "
                    f"{response.verification.matched_items}/"
                    f"{response.verification.checked_items} 项，"
                    f"缺失来源 {response.verification.missing_sources} 项"
                ),
                f"- {response.verification.message}",
            ]
        )
    if show_query and response.query:
        lines.extend(
            [
                "",
                "Cypher：",
                response.query.cypher,
                "",
                "参数：",
                json.dumps(response.query.parameters, ensure_ascii=False, indent=2),
            ]
        )
    if response.warnings:
        lines.extend(["", "提示："])
        lines.extend(f"- {warning}" for warning in response.warnings)
    if response.error:
        lines.extend(["", f"错误：{response.error.code.value} - {response.error.message}"])
    return "\n".join(lines)


def _exit_code(response: GraphQAResponse) -> int:
    if response.error is None:
        return 0
    if response.error.code == ErrorCode.INVALID_REQUEST:
        return 2
    if response.error.code in {
        ErrorCode.CONFIG_ERROR,
        ErrorCode.MODEL_UNAVAILABLE,
        ErrorCode.QUERY_TIMEOUT,
    }:
        return 3
    if response.error.code in {
        ErrorCode.UNSAFE_REQUEST,
        ErrorCode.CYPHER_REJECTED,
        ErrorCode.CYPHER_INVALID,
    }:
        return 4
    return 5


def run(
    args: argparse.Namespace,
    engine_factory: EngineFactory = build_engine_from_environment,
) -> Tuple[int, str]:
    try:
        engine = engine_factory(args.database, args.max_results)
    except CLIConfigError as exc:
        response = GraphQAResponse(
            question=args.question,
            answer="配置错误，无法启动知识图谱问答。",
            grounded=False,
            error={"code": ErrorCode.CONFIG_ERROR, "message": str(exc)},
        )
    except RepositoryError:
        response = GraphQAResponse(
            question=args.question,
            answer="无法读取图谱实体，请检查 Neo4j 服务。",
            grounded=False,
            error={
                "code": ErrorCode.CONFIG_ERROR,
                "message": "Neo4j 连接或只读权限不可用",
            },
        )
    except Exception:
        response = GraphQAResponse(
            question=args.question,
            answer="系统初始化失败，请检查服务和配置。",
            grounded=False,
            error={
                "code": ErrorCode.CONFIG_ERROR,
                "message": "知识图谱问答服务初始化失败",
            },
        )
    else:
        try:
            response = engine.answer(args.question)
        finally:
            repository = getattr(engine, "repository", None)
            if repository is not None and hasattr(repository, "close"):
                repository.close()

    if args.format == "json":
        output = json.dumps(
            response.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    else:
        output = render_text(response, show_query=args.show_query)
    return _exit_code(response), output


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    exit_code, output = run(args)
    print(output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
