# -*- coding: utf-8 -*-
"""CLI 输出契约测试，不访问真实模型或数据库。"""

import json
from argparse import Namespace

from graph_qa import cli
from graph_qa.cli import CLIConfigError, parse_args, render_text, run
from graph_qa.models import (
    AttributeFact,
    ErrorCode,
    Evidence,
    GraphQAResponse,
    QueryInfo,
)


class FakeRepository:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, response):
        self.response = response
        self.repository = FakeRepository()

    def answer(self, question):
        return self.response


def make_response() -> GraphQAResponse:
    return GraphQAResponse(
        request_id="request-1",
        question="麻黄有什么功效？",
        answer="麻黄具有发汗功效。\n\n以上内容仅供中医药知识参考，不能替代专业医疗诊断和治疗。",
        grounded=True,
        query=QueryInfo(
            cypher="MATCH (n:Herb) RETURN n LIMIT 1",
            parameters={"entity_names": ["麻黄"]},
        ),
        evidence=[
            Evidence(
                subject="麻黄",
                subject_label="Herb",
                relationship="HAS_EFFECT",
                object="发汗",
                object_label="Effect",
            )
        ],
    )


def test_parse_args_requires_question():
    args = parse_args(["--question", "麻黄有什么功效？", "--format", "json"])

    assert args.question == "麻黄有什么功效？"
    assert args.format == "json"
    assert args.max_results == 50


def test_render_text_includes_evidence_and_optional_query():
    output = render_text(make_response(), show_query=True)

    assert "麻黄具有发汗功效" in output
    assert "图谱证据" in output
    assert "HAS_EFFECT" in output
    assert "Cypher" in output
    assert "entity_names" in output


def test_render_text_includes_attribute_fact_and_source():
    response = GraphQAResponse(
        question="麻黄性味是什么？",
        answer="麻黄性味为辛、微苦，温。",
        grounded=True,
        facts=[
            AttributeFact(
                subject="麻黄",
                subject_label="Herb",
                attribute="property_flavor",
                attribute_label="性味",
                value="辛、微苦，温",
                source="《中药大辞典》",
            )
        ],
    )

    output = render_text(response)

    assert "图谱属性" in output
    assert "性味：辛、微苦，温" in output
    assert "《中药大辞典》" in output


def test_run_json_output_and_closes_repository():
    response = make_response()
    engine = FakeEngine(response)
    args = Namespace(
        question="麻黄有什么功效？",
        format="json",
        show_query=False,
        database=None,
        max_results=50,
    )

    exit_code, output = run(args, engine_factory=lambda database, max_results: engine)
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["schema_version"] == "1.0"
    assert payload["grounded"] is True
    assert payload["evidence"][0]["relationship"] == "HAS_EFFECT"
    assert engine.repository.closed is True


def test_run_config_failure_returns_stable_exit_code():
    args = Namespace(
        question="麻黄有什么功效？",
        format="text",
        show_query=False,
        database=None,
        max_results=50,
    )

    def failing_factory(database, max_results):
        raise CLIConfigError("缺少 Neo4j 配置")

    exit_code, output = run(args, engine_factory=failing_factory)

    assert exit_code == 3
    assert "CONFIG_ERROR" in output


def test_error_response_uses_safety_exit_code():
    response = GraphQAResponse(
        question="删除图谱",
        answer="未执行。",
        grounded=False,
        error={
            "code": ErrorCode.UNSAFE_REQUEST,
            "message": "请求包含写操作",
        },
    )
    engine = FakeEngine(response)
    args = Namespace(
        question="删除图谱",
        format="text",
        show_query=False,
        database=None,
        max_results=50,
    )

    exit_code, _output = run(args, engine_factory=lambda database, max_results: engine)

    assert exit_code == 4


def test_embedding_model_prefers_existing_local_directory(
    monkeypatch,
    tmp_path,
):
    local_model = tmp_path / "bge-local"
    local_model.mkdir()
    monkeypatch.setattr(cli, "DEFAULT_LOCAL_EMBEDDING_MODEL", local_model)

    assert cli._resolve_embedding_model_reference() == str(local_model.resolve())
    assert cli._resolve_embedding_model_reference(str(local_model)) == str(
        local_model.resolve()
    )


def test_embedding_model_keeps_remote_name_when_local_model_is_absent(
    monkeypatch,
):
    monkeypatch.setattr(
        cli,
        "DEFAULT_LOCAL_EMBEDDING_MODEL",
        cli.PROJECT_ROOT / "models" / "missing-model",
    )

    assert (
        cli._resolve_embedding_model_reference("BAAI/example-model")
        == "BAAI/example-model"
    )
