# 知识图谱问答数据与接口契约

> 文档状态：当前实现基线 v1.2
>
> 所属阶段：M1 问答内核、M2 API、M3 会话与流式、M4 Streamlit、查询扩展与 Agent 基础层
> 最后更新：2026-11-01

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
    },
    "plan_kind": "controlled_relationship"
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
  "facts": [],
  "aggregates": [],
  "paths": [],
  "verification": null,
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
  "facts": [],
  "aggregates": [],
  "paths": [],
  "verification": null,
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
- match_type: name | alias | fuzzy | semantic
- score: float
```

同一 `(label, name)` 只保留分数最高的结果。不同标签的同名实体不得去重。

具体名称形态的 Herb、Formula、Source 不得仅凭 semantic 候选通过链接。未解析代词不得进入 semantic 实体链接。Effect、Disease、Symptom 的描述性同义召回不受该限制。

未成功链接实体时返回澄清并包含“补充或确认具体名称”要求。已链接实体但无对应属性、关系或路径时返回固定无依据说明，`grounded=false`、`verification=null`，不得由模型补写医疗事实。查询返回记录但无法构造合法证据时仍返回 `NO_GRAPH_EVIDENCE`。

### 4.4 检索规划

`RetrievalAgent` 使用独立输入和输出契约。输入只包含已确认的上下文数据，不包含仓储、Cypher Guard 或执行权限：

```text
RetrievalPlanningInput
- question: str
- linked_entities: list[LinkedEntity], 至少 1 个
- validation_errors: list[str]
- attribute_query_attempted: bool
- query_attempts: int
```

```text
RetrievalPlanningResult
- plan_kind: controlled_multi_hop | controlled_attribute |
             controlled_aggregate | controlled_multi_condition_list |
             controlled_relationship | schema_constrained_cypher | rejected
- plan: 以下现有计划对象之一或 null
- query_attempts: int
- validation_errors: list[str]
- last_error_code: ErrorCode | null
```

非 `rejected` 结果必须携带且只携带一个与 `plan_kind` 匹配的计划对象。模板计划不增加 Text2Cypher 尝试次数；只有 Schema 约束回退会增加。Agent 只能调用受限模型生成器，不能调用 Guard、`EXPLAIN` 或仓储。

固定规划优先级为：

1. 白名单两跳。
2. 白名单属性。
3. 受控计数。
4. 多条件结果列表。
5. 唯一方向关系模板。
6. Schema 约束 Text2Cypher。

`rejected` 必须包含稳定错误码和可重试的具体校验错误。任何结果都不得绕过后续 Guard、只读仓储和确定性证据校验。

检索 Agent 使用最大三次 `plan → validate/execute → observe` 循环：

```text
RetrievalLoopObservation
- step: int
- max_steps: int
- plan_kind: str | null
- is_valid: bool
- error_code: str | null
- attribute_relation_fallback: bool
- has_context: bool
- row_count: int
```

```text
RetrievalLoopDecision
- next_action: replan | answer_from_graph |
               no_evidence | safe_fail | finish
- reason_code: str
```

`replan` 只能回到既有受控规划目录。执行错误直接结束，校验失败或属性空回退在预算内重新规划，有可验证上下文时进入回答，无上下文时进入无依据，预算耗尽进入安全失败。循环轨迹不包含模型隐藏推理。

### 4.5 Cypher 计划

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

### 4.6 查询结果

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

### 4.7 证据

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

查询执行后和答案生成前都执行目标证据校验：证据至少要与一个
`used_entities` 的 `(label, name)` 相同；与全部计划目标无关的证据必须丢弃。
没有目标证据时返回 `NO_GRAPH_EVIDENCE`，不得标记为 `grounded=true`。

### 4.8 属性事实

节点属性使用独立事实结构，不伪装成关系证据：

```text
AttributeFact
- subject: str
- subject_label: str
- attribute: str
- attribute_label: str
- value: str
- source: str | null
```

属性查询只能使用白名单字段。当前支持：

- `Herb`：别名、功效、主治、用量、归经、产地、炮制、性味、禁忌、性状、用法。
- `Formula`：别名、功效、主治、组成字段、禁忌、用法。

属性值、标签、实体名称和来源必须来自实际 Neo4j 查询结果。`source` 为空时，回答和前端必须明确显示“当前图谱未提供出处”，不能编造文献。

`GraphQAResponse.facts` 为新增可选数组，默认空。该变化属于向后兼容的字段增加，当前 `schema_version` 仍为 `1.0`。

### 4.9 聚合事实

受控一跳计数使用独立结构：

```text
AggregateFact
- anchor: str
- anchor_label: str
- relationship: str
- result_label: str
- direction: outgoing | incoming
- count: int
- description: str
```

聚合查询只允许已存在的 Schema 三元组模式和单跳 `count(DISTINCT ...)`。`require_all_entities=true` 时，每个已链接实体都必须满足同一条 Schema 关系，用于交集计数。`GraphQAResponse.aggregates` 默认空数组，不把计数伪装成节点属性或关系证据。

### 4.10 多条件结果列表

“同时含有人参和白术的方剂有哪些”等非计数问题使用独立受控计划：

```text
MultiConditionQueryPlan
- subject_label: str
- relationship: str
- object_label: str
- used_entities: list[UsedEntityRef], 至少 2 个
- description: str
```

执行时每个必需实体都必须通过固定的 `EXISTS` 条件，结果取多个条件的交集。返回记录仍转换为 `Evidence`，以展示结果实体与每个命中条件的关系和来源。该模式不调用模型自由排序、补写结果或生成任意聚合。

### 4.11 受控多跳路径

两跳查询使用显式、Schema 白名单内的路径计划：

```text
MultiHopStep
- subject_label: str
- relationship: str
- object_label: str

MultiHopQueryPlan
- steps: list[MultiHopStep], 当前固定为 2 步
- used_entities: list[UsedEntityRef]
- description: str

PathEvidence
- root: str
- root_label: str
- first_relationship: str
- intermediate: str
- intermediate_label: str
- second_relationship: str
- target: str
- target_label: str
- source: str | null
```

当前只允许以下两跳模式：

1. `Formula -HAS_INGREDIENT-> Herb -HAS_EFFECT-> Effect`
2. `Formula -HAS_INGREDIENT-> Herb -TREATS_DISEASE-> Disease`
3. `Formula -HAS_INGREDIENT-> Herb -ALLEVIATES_SYMPTOM-> Symptom`
4. `Herb -TREATS_DISEASE-> Disease -HAS_SYMPTOM-> Symptom`

两步节点链必须连续，每一步都必须通过 Schema 校验。`GraphQAResponse.paths` 默认空数组；任意深度、变量长度路径和模型自由规划不在当前契约内。

### 4.12 结构化回答

图谱回答模型不直接返回自由文本，而是返回内部结构化回答草稿：

```text
GraphAnswerDraft
- claims: list[AnswerClaim], 1 到 50 条

AnswerClaim
- text: str
- support_ids: list[str], 至少 1 个
```

当前关系证据按 `E1`、`E2` 编号，属性事实按 `F1`、`F2` 编号。提示词和审核器使用同一编号目录。每个 claim 只能引用本轮实际存在的编号，每个关系证据和属性事实必须至少被一个 claim 引用。

claim 文本必须包含对应证据的主体和对象，或属性事实的主体和值；claim 中出现的数字、单位、否定词和文献标题必须能在其引用的事实中找到。未通过核验的回答不得作为用户答案发送，系统直接回退到确定性证据化回答。

`AnswerClaim` 和 `GraphAnswerDraft` 是工作流内部契约，不进入 `GraphQAResponse` 或公开 API。

### 4.13 回答校验

图谱回答经过确定性证据审核后返回：

```text
AnswerVerification
- status: passed | fallback
- agent: evidence_audit
- checked_items: int
- matched_items: int
- evidence_items: int
- fact_items: int
- aggregate_items: int
- path_items: int
- missing_sources: int
- fallback_used: bool
- message: str
```

- `checked_items` 是本轮可核对的关系、属性、统计和路径条目总数。
- `matched_items` 是原回答逐项覆盖的条目数。
- 只有结构化 claim 全部通过编号核验，且 `matched_items == checked_items`、存在待核对条目时，`status` 才为 `passed`。
- 覆盖不完整、空回答或缺少待核对条目时，系统使用证据化回答，`status=fallback`、`fallback_used=true`。
- `missing_sources` 统计关系、属性和路径条目中 `source=null` 的数量；统计结果不单独计入来源缺失。
- 存在缺失来源时，程序在最终回答中统一补充“当前图谱未提供出处”，不依赖模型自行生成来源声明。

该审核只覆盖已定义的编号、结构、关键字段、数字/单位、否定词和来源披露检查，不是通用自然语言事实推理，也不等价于完整医学事实审核。`GraphQAResponse.verification` 默认为空；只有进入图谱回答审核的工作流才设置。

## 5. 大模型输出契约

### 5.1 意图分类

模型输出必须符合上面的 `IntentResult`。只输出“是”或“否”的自然语言方案不再使用，因为无法稳定区分低置信度、格式错误和范围外问题。

### 5.2 实体抽取

模型返回候选数组。候选不出现在图谱中时被链接器过滤；链接器不能因为候选来自模型就直接信任其标签。
“药材、方剂、功效、治疗”等通用意图词不进入 exact、fuzzy 或 semantic 候选。

### 5.3 Cypher 生成

查询规划优先级固定为：

1. 同时要求两个受控关系且匹配唯一白名单路径时，使用两跳模板。
2. 属性问题使用白名单属性模板，不调用模型生成属性查询。
3. 明确的“多少、几个、几种”问题在 Schema 方向和问题指向唯一时使用受控计数模板。
4. 非计数且同时要求多个实体时，使用多条件交集结果列表模板。
5. 高频关系问题在 Schema 方向和问题指向唯一时使用固定关系模板，支持正向和反向查询。
6. 受控模板无法覆盖时，才调用模型生成受 Schema 约束的 Text2Cypher。
7. 所有关系模板、多条件、多跳和 Text2Cypher 结果都必须经过对应结构校验。

当属性字段为空且问题涉及“功效”或“主治”时，允许受控回退到对应关系查询；其他属性为空时直接返回无可验证依据。

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

回答模型输出 `GraphAnswerDraft`。输入可以同时包含关系 `Evidence` 和属性 `AttributeFact`，模型只能通过 `support_ids` 引用已提供的事实。引用编号由程序分配和核对，不由模型自行创建。

回答生成后必须执行逐项证据支持检查。结构化回答需要覆盖全部本轮关系证据和属性事实；统计描述和路径节点继续由确定性答案审核。部分覆盖、未知编号、事实不匹配或无效结构化输出应替换为基于证据的简化回答，并返回警告及结构化 `verification`。

图谱回答只有在审核完成后才发送 `answer_delta`。直接回答等非图谱路径仍可流式输出。

## 6. Neo4j 仓储接口

计划接口：

```text
load_entity_index() -> EntityIndex
explain(plan) -> None | raises QueryValidationError
execute_read(plan) -> QueryResult
execute_attributes(attribute_plan) -> QueryResult
execute_aggregate(aggregate_plan) -> QueryResult
execute_multi_condition_list(multi_condition_plan) -> QueryResult
execute_multi_hop(multi_hop_plan) -> QueryResult
```

仓储责任：

- 读取并缓存名称和别名的只读快照。
- 在只读事务中执行查询。
- 应用超时和结果上限。
- 属性查询使用固定 Cypher 模板，不接受模型生成的任意属性查询。
- 聚合查询使用固定单跳/交集计数模板，不允许聚合表达式或标签由模型任意生成。
- 多条件列表使用固定 `EXISTS` 交集模板，不允许模型自由拼接条件或结果。
- 多跳查询只接受两步、节点连续且每一步都通过 Schema 白名单的计划。
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
- `query.plan_kind`：本轮选中的稳定检索策略代码；查询未形成时为 `null`。

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
| `stage` | `stage`、中文 `label`、对应 `agent`；`plan_cypher` 另含 `plan_kind`/`plan_label`，`retrieve_observe` 另含 `step`/`decision`/`reason_code` |
| `evidence` | `items` 数组 |
| `facts` | `items` 数组，属性事实 |
| `aggregates` | `items` 数组，受控计数结果 |
| `paths` | `items` 数组，受控多跳路径 |
| `audit` | 完整 `AnswerVerification` 对象 |
| `answer_delta` | `delta` 文本；图谱回答仅在对应 `audit` 完成后发送 |
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

新会话在首次请求处理前创建空历史。若本轮失败、安全拒绝或未保存成功：

- 失败回答不得加入历史。
- 空会话仍然有效，调用方可以继续使用返回的 `session_id`。
- 调用方不得保留服务端未创建成功的会话 ID。

显式出现新的已知实体时，该实体切换为当前主题；只有在指代、省略主语或明确追问上文的场景下才调用上下文改写。多轮改写不得添加历史中不存在的事实。

M1 CLI 和评测继续使用无会话调用，不依赖 Redis 会话状态。

## 9. 兼容性要求

- 结果对象增加字段允许向后兼容，删除或重命名字段需要升级 `schema_version`。
- `GraphQAResponse.paths` 和 SSE `paths` 是向后兼容的新增字段。
- `GraphQAResponse.verification` 和 SSE `audit` 是向后兼容的新增字段。
- SSE `stage.data.agent` 是向后兼容的新增字段，调用方不应依赖固定阶段数量。
- `query.plan_kind`、SSE `stage.data.plan_kind` 和 `plan_label` 是向后兼容的新增字段，不包含 Cypher 或模型推理。
- SSE `retrieve_observe` 的 `step`、`decision` 和 `reason_code` 是向后兼容的安全循环状态，不包含隐藏推理。
- 安全错误码不得改为“成功但无结果”。
- 图元数据版本变化时，必须运行实体索引和黄金问题集回归。
- 测试和日志中不得出现真实模型密钥、数据库密码或完整用户健康文本。
