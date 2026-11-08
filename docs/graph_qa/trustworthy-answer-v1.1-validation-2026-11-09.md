# 可信回答 v1.1 验证记录

> 验证日期：2026-11-09
>
> 分支：`dev`
>
> 环境：Windows、Python 3.12、Neo4j 5.26.8、Redis 6.2、BGE-large、OpenAI 兼容模型

## 1. 本轮改动

- 图谱回答模型改为返回 `GraphAnswerDraft`，每条 `AnswerClaim` 使用 `support_ids` 引用本轮证据。
- 关系证据编号为 `E1...`，属性事实编号为 `F1...`，提示词和审核器共用同一编号目录。
- 审核检查未知编号、遗漏证据、主体/事实不匹配、否定词、数字/单位和不支持文献标题。
- 未通过审核的图谱回答不会发送给前端，系统回退到确定性证据回答。
- 缺失来源由程序统一补充“当前图谱未提供出处”。
- SSE 图谱 `answer_delta` 调整为审核完成后发送，公开事件名称和响应 schema 不变。
- 新增 `graph_qa.human_fact_audit`，支持 50 条分层抽样、人工填写模板和门禁汇总。

## 2. 自动化测试

```text
251 passed, 5 skipped
```

跳过项仅包含需要显式启用的真实 Neo4j、Redis 和 API 集成测试；本轮已分别单独执行。

真实依赖验证：

| 测试 | 结果 |
| --- | --- |
| Neo4j + 模型 + BGE 问答集成 | 1 passed |
| Redis 限流、并发租约和会话共享 | 3 passed |
| 两个 API 实例共享同一 Redis 会话 | 1 passed |
| 真实结构化关系回答 | 4/4 证据审核通过 |

## 3. 200 条产品固定集

详细结果：本地 `output/graph_qa/product-quality-200-trustworthy-v1.1-2026-11-09.json`。

| 指标 | 当前结果 |
| --- | ---: |
| outcome | 198/200，99% |
| intent accuracy | 100% |
| entity Recall@3 | 98.33% |
| query pattern match | 90.59% |
| Cypher pass | 100% |
| query execution | 100% |
| grounded evidence coverage | 100% |
| fact match | 98.18% |
| aggregate match | 95% |
| no-evidence honesty | 100% |
| source-missing disclosure | 100% |
| unknown entity false recall | 0% |
| P50 latency | 297 ms |
| P95 latency | 8,905 ms |

验收门禁 `acceptance.passed=true`。两个失败用例是同一类 semantic 表达“清除体内热毒”，实体链接到 Disease 候选后查询无证据，系统诚实返回无依据，没有编造回答。该问题属于语义实体/路径选择，不是本轮结构化答案审核回归。

### 3.1 120 条黄金集

- outcome：119/120，99.17%。
- 完整工作流实体 Recall@3：96.67%。
- Cypher 通过率和查询执行成功率：100%。
- 安全拦截率：100%，7/7。
- grounded 证据覆盖率：100%。
- P50：3,854 ms；P95：14,682 ms。
- `acceptance.passed=true`。

### 3.2 多轮固定集

- 首轮完整运行：20 组、62 轮，outcome 56/62，90.32%。
- 实体匹配：94.74%。
- 5 个失败用例中，4 个直接原因是外部模型瞬时不可用，1 个是未知实体澄清后的主题继承问题。
- 单独重跑这 5 个用例：15/15 轮通过，outcome 和实体匹配均为 100%。

这说明验收门禁通过，但外部模型可用性会明显影响多轮固定集的单次结果，后续仍需重试、超时和失败轮次统计。

## 4. 浏览器验收

使用 Playwright 实测：

1. 点击“四君子汤由哪些药材组成？”，页面显示甘草、茯苓、白术、人参四项关系证据。
2. 回答审核显示 4/4 通过，来源缺失为 0。
3. 追问“它有什么禁忌？”，成功继承“四君子汤”实体。
4. 图谱没有对应属性依据时，页面返回“没有找到足以回答该问题的依据”，未编造禁忌。

截图：

- `output/playwright/trustworthy-answer-v1.1-2026-11-08.png`
- `output/playwright/trustworthy-answer-history-2026-11-08.png`

## 5. 人工审核状态

已从当前 200 条评测结果分层生成 50 条待审核记录：

```text
output/graph_qa/human-fact-audit-50-pending-2026-11-09.jsonl
```

该文件不包含自动期望结果，当前全部为 `pending`。必须由独立领域审核者填写
`fact_verdict`、`unsupported_claims` 和 `source_disclosure_correct` 后，才能判断
人工事实准确率是否达到 95% 门槛。本轮不宣称该人工审核已经完成。

## 6. 剩余风险

- 结构化审核覆盖已定义的确定性检查，不等价于通用语义事实推理。
- semantic 热毒表达仍有 2 条产品集失败，需要后续单独优化实体和路径选择。
- 50 条独立人工事实结论尚未完成。
- 浏览器验收使用 `GRAPH_QA_VECTOR_ENABLED=false` 加快启动；向量召回已在 200 条评测中启用验证。
