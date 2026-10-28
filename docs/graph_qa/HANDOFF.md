# 知识图谱问答交接说明

> 交接基线：`6c0b07c`（120 条评测工作区完成后更新）
>
> 交接日期：2026-10-28
>
> 当前分支：`main`

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
| M5 微调/部署增强 | 未开始 |

明确不在当前范围：

- 内容生成第三路由。
- 多 worker 部署验收。
- LoRA、vLLM、模型合并。
- 自动发布、图谱写入和在线医疗诊断。

## 3. 已实现架构

LangGraph 当前包含 15 个节点，主要流程：

```text
规范化
  -> 结合最近 5 轮历史改写问题
  -> 意图识别
  -> 实体抽取
  -> 精确名称/别名匹配
     -> 高置信 fuzzy
     -> FAISS Top-3 + 阈值
  -> Schema 约束 Cypher
  -> Guard / EXPLAIN
  -> Neo4j 只读查询
  -> 流式答案 + 证据
```

实体匹配顺序不能随意改成“语义优先”。真实测试中，“麻黄草”的 fuzzy 能正确命中麻黄，而 BGE 语义 Top-1 曾误排为天麻；当前策略是词形高置信匹配置信度高于语义召回。

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
- `answer_delta`
- `warning`
- `error`
- `done`

SSE 不承担客户端到服务端的后续消息。下一轮问题重新 POST，并复用 `session_id`。

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
| `graph_qa/workflow.py` | 15 节点 LangGraph 工作流、改写、SSE 事件 |
| `graph_qa/entity_linker.py` | 精确、fuzzy 和语义召回融合 |
| `graph_qa/semantic_index.py` | BGE 编码、FAISS 和索引缓存 |
| `graph_qa/session_store.py` | Redis 会话、TTL、删除和会话锁 |
| `graph_qa/redis_limits.py` | Redis 限流和全局并发租约 |
| `graph_qa/api.py` | FastAPI、鉴权、会话和 SSE 路由 |
| `graph_qa/api_security.py` | 多 API Key 配置和校验 |
| `graph_qa/streamlit_app.py` | Streamlit 双栏客户端 |
| `evaluation/qa_cases.jsonl` | 120 条黄金问题 |

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

- 当前全量 pytest：`128 passed, 5 skipped`。
- 真实 Neo4j + 模型集成：通过。
- Redis 限流、并发、会话集成：通过。
- 真实跨实例 SSE 会话：通过。
- 120 条黄金集完整工作流 outcome：`113/120`。
- 纯召回 Recall@3：`98.89%`；完整工作流实体 Recall@3：`94.44%`。
- 评测报告：[retrieval-evaluation-2026-10-28.md](retrieval-evaluation-2026-10-28.md)。
- Streamlit 已回答、进度、证据、追问和清空会话的真实浏览器测试。
- 浏览器截图：`output/playwright/graph-qa-streamlit.png`。

默认测试不访问真实外部依赖。真实测试分别使用：

```powershell
$env:GRAPH_QA_RUN_INTEGRATION = "1"
$env:GRAPH_QA_RUN_API_INTEGRATION = "1"
$env:GRAPH_QA_REDIS_TEST_URL = "redis://127.0.0.1:6379/0"
uv run python -m pytest tests/ -v
```

## 10. 已知限制

- 120 条黄金集仍不足以代表开放用户表达；semantic 路径完整工作流 Recall@3 为 75%。
- `0.56` 阈值在扩充集上暴露未知实体误召回，需后续单独校准。
- 完整问题上的短实体子串和通用词可能先于目标实体命中。
- 当前存在证据不关联目标实体但响应仍标记为 `grounded=true` 的风险。
- 会话作用域是 API 客户端，不是最终登录用户。
- 当前本地运行目标为单 worker；每个 worker 会重复加载约 1.3GB BGE 模型。
- 没有可靠的 API 请求级硬取消，仍依赖模型和 Neo4j 组件超时。
- 没有面向最终用户的多用户认证、监控和正式部署收尾。
- 不实现内容生成第三路由。

## 11. 下一步

优先修复评测暴露的检索与证据边界，再继续扩功能：

1. 修复完整问题上的短实体截获，避免“空青果”被“青果”直接命中。
2. 从 semantic 查询中处理“药材、方剂、功效、治疗”等通用意图词。
3. 收紧未知实体判定，并重新定义“证据必须关联目标实体”的 grounded 条件。
4. 修复后重复 120 条评测，再评估阈值、实体描述和 reranker。
5. 补充 5 轮追问评测，再进入 API 超时、监控和部署收尾。

## 12. 新对话建议开场

```text
继续开发 D:\code\github-clong\Chinese_medicine_knowledgegraph。
先读取 AGENTS.md 和 docs/graph_qa/HANDOFF.md，再检查 git status。
当前不实现内容生成，也不以多 worker 为运行目标。
先读取 docs/graph_qa/retrieval-evaluation-2026-10-28.md。
下一步优先修复完整问题上的短实体截获、通用词语义误召回和 grounded 证据边界，
修复后重复运行 120 条黄金集。
开发前按 AGENTS.md 给出方案并等待确认。
```
