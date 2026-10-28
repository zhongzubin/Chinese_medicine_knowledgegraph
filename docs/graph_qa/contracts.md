# 知识图谱问答数据与接口契约

> 文档状态：当前实现基线 v1.0
>
> 所属阶段：M1 问答内核、M2 API、M3 会话与流式、M4 Streamlit
> 最后更新：2026-10-28

本文定义 CLI、内部模块、FastAPI、Redis 会话、SSE 和 Streamlit 客户端必须遵守的当前契约。

## 1. 配置契约

运行问答需要以下环境变量：

| 变量 | 必填 | 含义 |
| --- | --- | --- |
| `MODEL_API_KEY` | 是 | OpenAI 兼容模型接口密钥 |
| `MODEL_BASE_URL` | 是 | 模型接口基础地址 |
| `MODEL_NAME` | 是 | 模型名称 |
| `NEO4J_URI` | 是 | Neo4j Bolt 地址 |
| `NEO4J_USER` | 是 | Neo4j 用户 |
| `NEO4J_PASSWORD` | 是 | Neo4j 密码 |

以下可选参数默认值由实现层统一定义：

| 变量 | 建议默认值 | 含义 |
| --- | ---: | --- |
| `GRAPH_QA_QUERY_TIMEOUT_SECONDS` | `8` | 单次图查询超时 |
| `GRAPH_QA_MAX_RESULTS` | `50` | 单次查询最多返回记录数 |
| `GRAPH_QA_CYPHER_MAX_ATTEMPTS` | `3` | 查询首次生成加重试的总次数 |
| `GRAPH_QA_ENTITY_LIMIT` | `10` | 每类实体最多保留的链接候选数 |
| `GRAPH_QA_VECTOR_ENABLED` | `false` | 是否构建 FAISS 实体语义索引 |
| `GRAPH_QA_EMBEDDING_MODEL` | `models/bge-large-zh-v1.5` | Sentence-Transformers 模型名或本地路径 |
| `GRAPH_QA_VECTOR_TOP_K` | `3` | 每次语义召回最多候选数 |
| `GRAPH_QA_VECTOR_THRESHOLD` | `0.56` | 语义相似度最低阈值 |
| `GRAPH_QA_SESSION_TTL_SECONDS` | `86400` | Redis 会话过期时间 |
| `GRAPH_QA_SESSION_MAX_TURNS` | `5` | 最多保留的对话轮数 |
| `GRAPH_QA_SESSION_LOCK_SECONDS` | `180` | 单会话分布式锁时间 |

配置缺失或非法时必须在发起外部请求前失败，错误信息不得包含密钥值。

## 2. 元数据契约

工作流启动时读取 `metadata/graph_metadata.json`，至少验证：

- `format_version` 存在。
- `node_labels` 是数组且包含 `label`、`properties`。
- `relationship_types` 是数组且包含 `type`。
- `triple_patterns` 是数组且包含 `subject_label`、`relationship_type`、`object_label`。
- 每个标签具有 `name` 属性或明确标识属性。

当前只接受以下六类实体：

- `Herb`
- `Formula`
- `Disease`
- `Symptom`
- `Effect`
- `Source`

如果数据库中存在额外标签，问答工作流不得自动将其暴露给模型。新增标签需要更新元数据、白名单和测试。

## 3. CLI 契约

### 3.1 命令

```powershell
uv run python -m graph_qa.cli --question "麻黄有什么功效？"
```

可选参数：

```text
--question TEXT       必填，用户问题
--format json|text    可选，默认 text
--show-query          可选，显示 Cypher；默认在 text 模式隐藏
```

CLI 不读取交互式标准输入。Streamlit 通过 FastAPI 服务接口接入，不复制问答工作流。

### 3.2 JSON 输出

成功回答：

```json
{
  "schema_version": "1.0",
  "request_id": "uuid",
  "question": "麻黄有什么功效？",
  "answer": "根据图谱，麻黄具有……",
  "grounded": true,
  "intent": {
    "is_tcm_question": true,
    "confidence": 0.96,
    "reason_code": "TCM_ENTITY_PRESENT"
  },
  "linked_entities": [
    {
      "label": "Herb",
      "name": "麻黄",
      "matched_text": "麻黄",
      "match_type": "name",
      "score": 1.0
    }
  ],
  "query": {
    "cypher": "MATCH ... RETURN ... LIMIT 50",
    "parameters": {
      "entity_names": ["麻黄"]
    }
  },
  "evidence": [
    {
      "subject": "麻黄",
      "subject_label": "Herb",
      "relationship": "HAS_EFFECT",
      "object": "发汗解表",
      "object_label": "Effect",
      "source": null
    }
  ],
  "warnings": [],
  "error": null
}
```

安全失败：

```json
{
  "schema_version": "1.0",
  "request_id": "uuid",
  "question": "删除所有药材",
  "answer": "该请求未执行。系统只支持知识图谱只读查询。",
  "grounded": false,
  "intent": null,
  "linked_entities": [],
  "query": null,
  "evidence": [],
  "warnings": [],
  "error": {
    "code": "CYPHER_REJECTED",
    "message": "请求包含不允许的写操作"
  }
}
```

### 3.3 退出码

| 退出码 | 含义 |
| ---: | --- |
| `0` | 成功返回答案或明确的“无图谱依据” |
| `2` | 参数或输入错误 |
| `3` | 配置或外部依赖不可用 |
| `4` | 查询被安全策略拒绝 |
| `5` | 内部错误 |

错误码必须稳定，CLI 文本可以调整，错误码不能随提示词变化。

## 4. 内部数据模型

### 4.1 意图

```text
IntentResult
- is_tcm_question: bool
- confidence: float, 范围 0 到 1
- matched_domain_terms: list[str]
- reason_code: str
```

模型没有返回合法 JSON 时视为分类失败，不使用自然语言关键字猜测结果。

### 4.2 实体候选

```text
EntityCandidate
- text: str
- suggested_label: Herb | Formula | Disease | Symptom | Effect | Source | unknown
```

模型只能提出候选文本和标签建议。最终 `label` 和 `name` 必须通过图谱索引确认。

### 4.3 链接实体

```text
LinkedEntity
- label: str
- name: str
- matched_text: str
- match_type: name | alias | fuzzy
- score: float
```

同一 `(label, name)` 只保留分数最高的结果。不同标签的同名实体不得去重。

### 4.4 Cypher 计划

```text
CypherPlan
- cypher: str
- parameters: dict[str, scalar | list[scalar]]
- used_entities: list[UsedEntityRef]
- expected_result_shape: str
```

参数只允许 Neo4j 支持的标量和标量数组。禁止嵌套对象、任意 Python 对象和可直接执行的字符串表达式。

```text
UsedEntityRef
- label: str
- name: str
```

`used_entities` 只能引用已经链接成功的实体。工作流负责把 `UsedEntityRef` 与完整 `LinkedEntity` 对应，不要求模型重复生成匹配分数。

### 4.5 查询结果

```text
QueryResult
- cypher: str
- parameters: dict
- rows: list[dict]
- row_count: int
- elapsed_ms: int
- limit_applied: int
```

`rows` 必须全部可 JSON 序列化。节点和关系在进入答案层前转换为明确字段，不传入驱动对象。

### 4.6 证据

```text
Evidence
- subject: str
- subject_label: str
- relationship: str
- object: str
- object_label: str
- source: str | null
```

证据只能来自实际返回的图记录。答案生成节点不能自行创造证据。

## 5. 大模型输出契约

### 5.1 意图分类

模型输出必须符合上面的 `IntentResult`。只输出“是”或“否”的自然语言方案不再使用，因为无法稳定区分低置信度、格式错误和范围外问题。

### 5.2 实体抽取

模型返回候选数组。候选不出现在图谱中时被链接器过滤；链接器不能因为候选来自模型就直接信任其标签。

### 5.3 Cypher 生成

模型必须返回单个 JSON 对象：

```json
{
  "cypher": "MATCH ... RETURN ... LIMIT 20",
  "parameters": {},
  "used_entities": [
    {
      "label": "Herb",
      "name": "麻黄"
    }
  ],
  "expected_result_shape": "herb_effect_pairs"
}
```

说明性文字、Markdown 代码围栏和多查询数组都视为格式错误。重试提示应指出具体错误，不重复原始模型隐藏推理。

查询结果必须返回以下固定别名：

- `subject`
- `subject_label`
- `relationship`
- `object`
- `object_label`
- `source`，图谱没有来源时返回 `null`

这些字段只用于构造可验证证据，不允许由答案模型自行补写。

### 5.4 回答

回答节点输出纯文本。引用内容由程序根据 `Evidence` 渲染，不由模型自行编造引用编号。涉及来源时使用图谱中的 `Source.name`。

## 6. Neo4j 仓储接口

计划接口：

```text
load_entity_index() -> EntityIndex
explain(cypher, parameters) -> None | raises QueryValidationError
execute_read(cypher, parameters, timeout, max_rows) -> QueryResult
```

仓储责任：

- 读取并缓存名称和别名的只读快照。
- 在只读事务中执行查询。
- 应用超时和结果上限。
- 将驱动返回对象转换为可序列化数据。
- 不拼接用户输入，不生成 Cypher，不调用大模型。

仓储测试使用 fake session。真实 Neo4j 集成测试必须显式启用，服务不可用时跳过并给出原因。

## 7. API 契约

FastAPI 当前提供以下接口：

- `GET /health`
- `POST /api/v1/qa`
- `POST /api/v1/qa/stream`
- `DELETE /api/v1/sessions/{session_id}`

请求至少包含：

```text
question: str, 1 到 500 字符
session_id: optional uuid
```

不传 `session_id` 时服务创建新会话，并在 `GraphQAResponse.session_id` 中返回。后续请求可携带该 ID 使用最近 5 轮上下文。会话不存在、已过期或属于其他 API 客户端时统一返回 404。

响应额外包含：

- `session_id`：本轮会话 ID。
- `rewritten_question`：结合历史改写后的独立问题，便于调试指代。

成功响应和业务错误继续使用 `GraphQAResponse`。推荐 HTTP 状态映射：

| HTTP | 场景 |
| ---: | --- |
| `200` | 正常回答、澄清、查询无记录 |
| `400` | 输入请求或用户指令不安全 |
| `401` | API Key 缺失或无效 |
| `404` | 会话不存在、已过期或不属于当前客户端 |
| `409` | 同一会话已有请求正在执行 |
| `422` | 请求结构或问题长度不符合契约 |
| `429` | 超过客户端限流额度 |
| `500` | 未预期内部错误 |
| `502` | 模型输出、Cypher 或证据校验失败 |
| `503` | 配置、模型、限流服务不可用或全局并发已满 |
| `504` | 图查询超时 |

API 使用 `X-API-Key` 静态凭据。Redis 保存滑动窗口、全局并发租约和会话状态；Redis 不可用时失败关闭。当前本地运行目标为单 worker，多实例协调代码保留但暂不作为本轮部署验收条件。

HTTP 响应不得默认暴露原始提示词、模型隐藏推理和完整数据库异常。输出仍使用本节定义的答案、证据和错误码。

`POST /api/v1/qa/stream` 返回 `text/event-stream`。每个事件包含 `event` 名称和 JSON `data`：

| 事件 | 数据 |
| --- | --- |
| `stage` | `stage`、中文 `label` |
| `evidence` | `items` 数组 |
| `answer_delta` | `delta` 文本 |
| `warning` | `message` |
| `error` | 稳定错误码与消息 |
| `done` | 完整 `response` |

阶段事件只表示公开处理进度，不表示模型隐藏思维链。

流式接口不得改变非流式接口的最终答案、证据和错误语义。

## 8. 会话契约

当前区分：

- `client_id`：API Key 对应的安全主体。
- `session_id`：一段独立对话。
- `request_id`：单次问答。

会话实际作用域为 `(client_id, session_id)`。不同 API Key 不能互相读取或删除会话。当前静态 API Key 不提供同一客户端内部的端用户隔离；需要该能力时应接入 JWT/OIDC 等用户身份。

Redis 只保存最近 5 轮问题、回答、已链接实体、grounded 状态和时间戳。默认 TTL 为 86400 秒，不保存 Cypher、完整查询行、模型提示词或错误堆栈。单个会话使用 Redis 分布式锁，禁止并发覆盖历史。

M1 CLI 和评测继续使用无会话调用，不依赖 Redis 会话状态。

## 9. 兼容性要求

- 结果对象增加字段允许向后兼容，删除或重命名字段需要升级 `schema_version`。
- 安全错误码不得改为“成功但无结果”。
- 图元数据版本变化时，必须运行实体索引和黄金问题集回归。
- 测试和日志中不得出现真实模型密钥、数据库密码或完整用户健康文本。
