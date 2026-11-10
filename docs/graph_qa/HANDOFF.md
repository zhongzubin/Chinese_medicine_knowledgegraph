# 知识图谱问答交接说明

> 代码基线：`c9c430b`
>
> 交接日期：2026-11-09
>
> 当前分支：`dev`

本文用于新对话快速恢复上下文。开始开发前仍须完整阅读 `AGENTS.md`、`docs/graph_qa/README.md`、`architecture.md`、`contracts.md`、`security-and-evaluation.md` 和 `implementation-challenges.md`。

## 1. 项目目标

项目是一个可验证的中医知识图谱问答系统：

- Python、LangGraph、LangChain、Neo4j、FAISS、Sentence-Transformers、FastAPI、Streamlit。
- 从非结构化中医药文本抽取 6 类实体和 6 类关系。
- 通过 Schema 约束 Text2Cypher 回答可被图谱证据支持的问题。
- 没有图谱依据时明确澄清或说明无依据，不编造医疗事实。

## 2. 当前阶段

| 阶段 | 状态 |
| --- | --- |
| M1 问答内核 | 已完成 |
| M2 FastAPI | 已完成，当前本地按单 worker 运行 |
| M3 5 轮会话、FAISS、SSE | 已完成 |
| M4 Streamlit | 已完成并通过真实浏览器测试 |
| 本地演示版 | 已完成；一键启动/停止和真实浏览器验收通过 |
| 专业问答质量 v1 | 首轮已完成；属性、确定性查询、多轮和答案支持校验通过 |
| 查询覆盖扩展 | 已完成首轮；多条件结果列表和四种 Schema 白名单两跳 |
| Agent 基础层 | 已启动；角色注册、受控检索 Agent 闭环和证据审核 Agent 已接入 |
| M5 微调/部署增强 | 未开始 |

明确不在当前范围：

- 内容生成第三路由。
- 多 worker 部署验收。
- LoRA、vLLM、模型合并。
- 自动发布、图谱写入和在线医疗诊断。

## 3. 已实现架构

LangGraph 当前包含 17 个节点，主要流程：

```text
规范化
  -> 结合最近 5 轮历史改写问题
  -> 意图识别
  -> 实体抽取
  -> 精确名称/别名匹配
     -> 高置信 fuzzy
     -> FAISS Top-3 + 阈值
  -> 受控两跳 / 属性模板 / 受控聚合 / 多条件列表
     / 唯一方向关系模板 / Schema 约束 Cypher
  -> 结构校验 / Guard / EXPLAIN
  -> Neo4j 只读查询
  -> Agent 观察结果并决定重规划 / 回答 / 无依据 / 安全终止
  -> 属性事实 + 聚合结果 + 关系证据 + 多跳路径
  -> 流式答案 + 证据支持校验
```

实体匹配顺序不能随意改成“语义优先”。真实测试中，“麻黄草”的 fuzzy 能正确命中麻黄，而 BGE 语义 Top-1 曾误排为天麻；当前策略是词形高置信匹配置信度高于语义召回。

Agent 基础层目前是固定 LangGraph 拓扑上的职责注册，已具备受控单 Agent 闭环，但还不是独立 Agent 子图：

- `dialogue`：规范化与多轮改写，改写模型通过角色对象调用。
- `intent`：实体预匹配与领域意图，意图模型通过角色对象调用。
- `entity`：候选抽取与实体链接，抽取模型通过角色对象调用，最终链接仍为确定性实现。
- `retrieval`：真实受控闭环 Agent；选择计划、观察校验/执行结果，并决定重规划、回答、无依据或安全终止。
- `answer`：图谱回答与直接回答，模型调用通过角色对象完成。
- `evidence_audit`：真实确定性 Agent，核对结构化 claim 的证据编号、主体/事实匹配、数字/单位、否定词和来源，并在失败时回退为证据化回答。

`RetrievalAgent` 使用独立 `RetrievalPlanningInput`/`RetrievalPlanningResult` 契约，输出受控两跳、属性、聚合、多条件列表、受控关系或 Schema 约束回退计划之一。它不持有 Cypher Guard 或仓储，不能执行查询；多跳仍固定为四种白名单两跳路径。

## 4. 会话与流式

- `session_id` 与 `client_id` 共同构成会话作用域。
- Redis 保存最近 5 轮问题、回答、已链接实体、grounded 状态和时间戳。
- 默认会话 TTL 为 86400 秒。
- 同一会话使用 Redis 锁串行执行。
- 不保存 Cypher、完整查询行、模型提示词和错误堆栈。
- API 请求不传 `session_id` 时自动创建，并在响应中返回。
- `DELETE /api/v1/sessions/{session_id}` 主动删除会话。

SSE 是单向的服务端到客户端流，客户端先发送一次普通 POST 请求，服务端在同一 HTTP 响应中持续发送：

- `stage`
- `evidence`
- `facts`
- `aggregates`
- `paths`
- `audit`
- `answer_delta`
- `warning`
- `error`
- `done`

`stage` 同时携带对应 `agent`。SSE 不承担客户端到服务端的后续消息。下一轮问题重新 POST，并复用 `session_id`。

## 5. 本地向量检索

本地模型：

```text
models/bge-large-zh-v1.5
```

- 已在离线模式验证，可输出 1024 维向量。
- 当前阈值：`0.56`。
- 真实实体数量：11,043。
- FAISS 使用归一化向量和 `IndexFlatIP`。
- 完整索引首次构建约 588 秒。
- 索引缓存在 `.cache/graph_qa/faiss/`，约 43MB。
- 缓存命中启动约 6.6 秒，单次 FAISS 查询约 0.06 秒。

缓存键包含实体集合和模型路径。已知限制：模型文件在同一路径被原地替换时，缓存不会因文件内容变化自动失效。

`models/Qwen2.5-0.5B-Instruct` 是本地生成模型权重，但当前问答模型仍通过 OpenAI 兼容的 `MODEL_BASE_URL` 调用，不会自动加载 Qwen 权重。

## 6. Redis

当前代码使用 Redis 实现：

- API Key 维度的滑动窗口限流。
- 全局并发租约。
- 5 轮会话。
- 单会话分布式锁。

交接环境中的现有 Redis 地址为 `redis://127.0.0.1:6379/0`，无需强制启动 Docker。`compose.redis.yml` 是可选辅助配置。

## 7. 主要文件

| 文件 | 作用 |
| --- | --- |
| `graph_qa/workflow.py` | 17 节点 LangGraph 工作流、受控 Agent 循环、改写与 SSE 事件 |
| `graph_qa/agents/` | Agent 描述、注册表和模型调用角色 |
| `graph_qa/agents/evidence_audit.py` | 结构化 claim 核验、来源披露和证据化回退 |
| `graph_qa/human_fact_audit.py` | 50 条人工事实审核抽样、填写模板和汇总门禁 |
| `graph_qa/entity_linker.py` | 精确、fuzzy 和语义召回融合 |
| `graph_qa/semantic_index.py` | BGE 编码、FAISS 和索引缓存 |
| `graph_qa/session_store.py` | Redis 会话、TTL、删除和会话锁 |
| `graph_qa/redis_limits.py` | Redis 限流和全局并发租约 |
| `graph_qa/api.py` | FastAPI、鉴权、会话和 SSE 路由 |
| `graph_qa/api_security.py` | 多 API Key 配置和校验 |
| `graph_qa/streamlit_app.py` | Streamlit 双栏客户端 |
| `evaluation/qa_cases.jsonl` | 120 条黄金问题 |
| `evaluation/product_quality_cases.jsonl` | 200 条产品固定集，覆盖属性、关系、聚合、多条件、多跳、semantic、未知实体、无依据和无来源 |
| `evaluation/multi_turn_cases.jsonl` | 20 组 62 轮多轮固定集 |
| `graph_qa/multi_turn_evaluate.py` | 多轮固定集评测入口 |

## 8. 必要配置

本地 `.env` 不提交，至少需要：

```dotenv
GRAPH_QA_AUTH_REQUIRED=true
GRAPH_QA_API_KEYS_JSON={"local-client":"至少32字符的本地密钥"}
GRAPH_QA_REDIS_URL=redis://127.0.0.1:6379/0
GRAPH_QA_REDIS_PREFIX=graph_qa
GRAPH_QA_RATE_LIMIT_REQUESTS=10
GRAPH_QA_RATE_LIMIT_WINDOW_SECONDS=60
GRAPH_QA_MAX_CONCURRENT_REQUESTS=4
GRAPH_QA_SESSION_TTL_SECONDS=86400
GRAPH_QA_SESSION_MAX_TURNS=5
GRAPH_QA_VECTOR_ENABLED=true
GRAPH_QA_EMBEDDING_MODEL=models/bge-large-zh-v1.5
GRAPH_QA_VECTOR_TOP_K=3
GRAPH_QA_VECTOR_THRESHOLD=0.56
GRAPH_QA_STREAMLIT_API_URL=http://127.0.0.1:8000
GRAPH_QA_STREAMLIT_API_KEY=至少32字符的本地密钥
```

运行：

```powershell
uv run uvicorn graph_qa.api:app --host 127.0.0.1 --port 8000
uv run streamlit run graph_qa/streamlit_app.py
```

## 9. 验证基线

- 当前全量 pytest：`255 passed, 5 skipped`。
- 真实 Neo4j + 模型集成：本地演示和历史验收已通过；本轮默认 pytest 中按配置跳过。
- Redis 限流、并发、会话集成：历史验收已通过；本轮默认 pytest 中按配置跳过。
- 真实跨实例 SSE 会话：历史验收已通过；本轮未重跑。
- 120 条黄金集完整工作流 outcome：`120/120`。
- 纯召回 Recall@3：`100%`；完整工作流实体 Recall@3：`98.89%`。
- 未知实体误召回率：`0%`；120 条语义路径完整工作流 Recall@3：`95%`，路径匹配率 `90%`。
- 产品固定集：200/200 outcome 通过，接受门禁通过。
- 未知实体误召回率 0%，无依据诚实率和无来源披露率均为 100%。
- 多轮固定集：20 组 62 轮，outcome 和实体匹配均为 100%。
- 修复前基线：[retrieval-evaluation-2026-10-28.md](retrieval-evaluation-2026-10-28.md)。
- 本轮修复记录：[retrieval-boundary-fix-2026-10-28.md](retrieval-boundary-fix-2026-10-28.md)。
- 验收门槛记录：[evaluation-acceptance-2026-10-28.md](evaluation-acceptance-2026-10-28.md)。
- Streamlit 已回答、进度、属性、聚合、多条件列表、多跳路径、追问和清空会话的真实浏览器测试。
- 浏览器截图：`output/playwright/graph-qa-streamlit.png`。

默认测试不访问真实外部依赖。真实测试分别使用：

```powershell
$env:GRAPH_QA_RUN_INTEGRATION = "1"
$env:GRAPH_QA_RUN_API_INTEGRATION = "1"
$env:GRAPH_QA_REDIS_TEST_URL = "redis://127.0.0.1:6379/0"
uv run python -m pytest tests/ -v
```

## 10. 已知限制

- 120 条黄金集仍不足以代表开放用户表达；semantic 路径完整工作流 Recall@3 为 95%，路径匹配率为 90%。
- 显式名称型未知实体的 semantic 误召回已降为 0；描述性 semantic 仍有路径与目标标签差异。
- 长未知词子串、通用词语义候选和无关证据 grounded 已完成首轮修复。
- 描述性 semantic 候选已使用建议标签优先和全局回退；模型标签错误仍可能造成稀有边界波动。
- 属性查询已进入回答上下文；当前覆盖白名单属性、一跳正向/反向关系、单跳和多条件计数、多条件结果列表以及四种白名单两跳。
- 任意深度、任意关系组合或由模型动态规划的多跳仍未实现。
- 当前没有显式 `no_evidence` 黄金样本，空结果诚实率标记为未评估。
- Redis 5 轮会话已通过 20 组、62 轮固定集；更长上下文和跨端用户身份仍不在当前范围。
- 新会话初始化和失败轮次会话 ID 问题已修复。
- 图谱回答已使用 `AnswerClaim` + `support_ids` 结构化草稿，审核覆盖未知编号、遗漏证据、主体/事实不匹配、否定词、数字/单位和未引用文献；仍不等价于通用语义事实推理。
- 产品固定集已达到 200 条；50 条人工审核工具已完成，实际独立领域人工结论仍待完成。
- 历史浏览器上下文追问曾等待约 70 秒；2026-11-09 产品集 P95 为 8.9 秒，但真实交互仍需继续建立分阶段延迟基线。
- 会话作用域是 API 客户端，不是最终登录用户。
- 当前本地运行目标为单 worker；每个 worker 会重复加载约 1.3GB BGE 模型。
- 没有可靠的 API 请求级硬取消，仍依赖模型和 Neo4j 组件超时。
- 没有面向最终用户的多用户认证、监控和正式部署收尾。
- 不实现内容生成第三路由。
- `retrieval` 已具备主图内的 `plan → act → observe → replan` 闭环；独立 Agent 子图和动态调度仍未完成。
- 澄清后未解析代词已阻止进入 semantic 链接；最终 62 轮全量通过。

## 11. 当前实现与验证状态

- 初始 120 条评测提交：`793f7d2`、`c09a6e1`。
- 边界与 grounded 修复提交：`c99cb6d`。
- 验收门槛提交：`688d868`。
- 本地演示版提交：`7be7f3d`（已快进合并到 `main`）。
- 专业问答质量 v1 提交：`a0f0f42`。
- 多条件聚合查询提交：`0244552`。
- 查询扩展与 Agent 基础层提交：`9c48400`。
- 证据审核 Agent 与回答校验提交：`b0dc5d2`。
- 检索规划 Agent 提交：`80ddc7b`。
- 未知实体、无依据与 200 条产品集提交：`dd55231`。
- 受控检索 Agent 闭环提交：`f55d963`。
- 避免过度防御性编程规范提交：`0082857`。
- 结构化图谱回答审核提交：`3aae452`。
- 人工事实审核工具提交：`1014ead`。
- 语义召回边界修复提交：`c9c430b`。
- 全量 pytest：`255 passed, 5 skipped`。
- 产品固定集：200/200 outcome 通过；未知实体误召回 0%，无依据与无来源披露 100%。
- 真实 Redis 双实例、Neo4j/模型和 API 跨实例会话集成本轮均通过。
- 最新完整记录：[trustworthy-answer-v1.1-validation-2026-11-09.md](trustworthy-answer-v1.1-validation-2026-11-09.md)。
- semantic 边界最新记录：[语义召回边界验证记录](语义召回边界验证记录-2026-11-09.md)。
- 真实 120 条 outcome：`120/120`；实体 Recall@3：98.89%；grounded 证据覆盖率：100%。
- 多轮全量：20 组、62 轮，outcome 和实体匹配均为 100%。
- Cypher 通过率：100%；查询执行成功率：100%；`acceptance.passed=true`。
- 专业问答质量 v1 完整记录：[product-quality-validation-2026-10-31.md](product-quality-validation-2026-10-31.md)。
- 检索规划 Agent 完整记录：[retrieval-agent-validation-2026-11-01.md](retrieval-agent-validation-2026-11-01.md)。
- 未知实体、无依据与 200 条产品集记录：[quality-boundary-validation-2026-11-01.md](quality-boundary-validation-2026-11-01.md)。
- 完整逐题 JSON 保存在本地
  `output/graph_qa/evaluation-120-retrieval-boundaries-final-2026-10-28.json`；
  `output/` 被 Git 忽略，不应依赖该文件恢复结论。
- 本地演示启动：`scripts/start_local_demo.ps1`。
- 本地演示停止：`scripts/stop_local_demo.ps1`。
- 本地演示完整说明：[local-demo.md](local-demo.md)。
- 本地演示一键启动、API/Streamlit 健康检查、浏览器交互和停止脚本均已实测通过。
- 浏览器已验证问题回答、处理阶段、图谱证据、上下文追问和清空会话。
- 验收截图：`output/playwright/local-demo-2026-10-29.png`。
- 新浏览器验收已验证属性区、来源状态和“它有什么禁忌”多轮追问。
- 新验收截图：`output/playwright/product-quality-final-2026-10-31.png`。
- 新浏览器验收已验证多条件结果列表及“图谱证据”展开。
- 多条件截图：`output/playwright/multi-condition-final-2026-10-31.png`。
- 新浏览器验收已验证四君子汤两跳功效和“多跳证据”展开。
- 多跳截图：`output/playwright/multi-hop-final-2026-10-31.png`。
- 新浏览器验收已验证“回答校验”区域、`2/2` 证据覆盖、属性计数和缺失来源提示。
- 回答校验截图：`output/playwright/answer-audit-final-2026-11-01.png`。
- 检索规划 Agent 浏览器验收已验证属性、统计和受控两跳策略标签。
- 检索规划截图：
  `output/playwright/retrieval-agent-attribute-2026-11-01.png`、
  `output/playwright/retrieval-agent-aggregate-2026-11-01.png`、
  `output/playwright/retrieval-agent-multi-hop-2026-11-01.png`。

## 12. 下一步

本地演示版已完成。专业问答质量 v1 和首轮查询扩展已于 2026-10-31 完成，目标是以真实图谱事实为依据，形成专业、可靠、可核对的中医药知识问答产品。

完整规划见
[product-quality-roadmap.md](product-quality-roadmap.md)。

当前优先级：

1. 使用 `graph_qa.human_fact_audit` 完成 50 条独立领域人工事实复核。
2. 建立分阶段耗时基线并优化模型和追问延迟。
3. 将真实 Redis/API/Neo4j 集成测试纳入可重复 CI。
4. 保持 semantic 边界、结构化回答审核、FastAPI、Streamlit、Redis、LangGraph、Neo4j、BGE + FAISS 和 Cypher Guard 不回归。
5. 质量门禁完成后进入独立 Agent 拓扑或动态调度。
6. 内部试用、多用户认证、监控和多 worker 继续延后。

### 12.1 当前质量基线

- Neo4j 实际有 11,043 个节点和 26,324 条关系。
- 6 类实体、6 类关系、14 种元数据三元组模式。
- 当前 120 条评测 outcome 为 120/120。
- 当前产品固定集 200/200；多轮集 20 组 62 轮全部通过。
- 产品回答审核覆盖率和逐项证据覆盖率均为 100%。
- 原 120 条仍没有显式 `no_evidence` 用例；属性集规模也需继续扩大。
- `Herb` 中约有 1,466 个性味、1,236 个归经、1,219 个用量和 851 个禁忌字段。
- `Formula` 中约有 741 个主治、619 个功效字段和 604 个用法字段。
- 来源关系覆盖：药材 412/2,359，方剂 720/1,020。

属性事实、单跳聚合、多条件列表、白名单两跳、检索规划 Agent、结构化证据审核、semantic 标签边界、200 条产品集和负样本语义边界已完成首轮落地。下一阶段重点是独立人工事实复核、CI 和延迟优化。

### 12.2 多轮上下文范围

多轮上下文已通过首轮产品固定集。保留 Redis 最近 5 轮存储和会话锁，当前行为包括：

- “它、该药、这个方剂”等指代。
- 无歧义时的省略主语继承。
- 同一实体上的关系切换。
- 新明确实体触发的主题切换。
- 用户纠正实体。
- 歧义时澄清，不猜测。
- 失败轮次不污染下一轮。
- 前端不会保留服务端未成功创建的 `session_id`。
- 第 6 轮裁剪、TTL、主动清空和跨会话隔离。

当前固定集为 20 组、62 轮，最近全量 outcome 和实体匹配均为 100%。真实 Redis/API 跨实例测试已在本轮通过。

### 12.3 产品验收重点

- 属性事实能够从图谱进入回答上下文。
- 关系问答和现有安全边界不回归。
- grounded 回答引用覆盖率为 100%。
- 无证据诚实率为 100%。
- 图谱无来源时必须明确披露。
- 答案关键事实不得超出本轮证据。
- 涉及页面时完成真实浏览器多轮交互验证。
- 全量 pytest 通过，新增属性和多轮测试通过。
- 不新增内容生成、自动发布或多 worker 运行目标。

### 12.4 实现约束与回滚

- 新增查询模式必须先更新元数据、契约、测试和评测。
- Text2Cypher 只能作为受控回退，不能替代属性/关系统一证据。
- 安全校验、只读执行和证据校验保持确定性实现。
- Agent 演进先从角色边界、契约和可观测性开始；确定性查询与审核边界不得削弱。
- 从 `main` 创建独立功能分支。
- 出现不可接受回归时回退到本地演示基线或 `18382f6`。

## 13. 新对话建议开场

```text
继续开发 D:\code\github-clong\Chinese_medicine_knowledgegraph。
先读取 AGENTS.md、docs/graph_qa/HANDOFF.md、local-demo.md 和
product-quality-roadmap.md，再检查 git status。
当前不实现内容生成，也不以多 worker 为运行目标。
当前主线是查询契约已稳定的前提下推进 Agent 演进：
保持属性和关系证据、多轮上下文、来源披露和固定集评测不回归，
检索规划、证据审核 Agent、未知实体边界和 200 条产品集已完成；下一阶段重点是最终多轮全量、人工事实复核，然后进入独立 Agent 拓扑或动态调度。
内部试用和公开上线工程化后续再做。
开发前按 AGENTS.md 给出方案并等待确认。
```
