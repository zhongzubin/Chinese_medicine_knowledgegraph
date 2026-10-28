# 黄金集扩充与实体召回路径评测报告

> 评测日期：2026-10-28
>
> 黄金集：`evaluation/qa_cases.jsonl`
>
> 详细结果：`output/graph_qa/evaluation-120-2026-10-28.json`
>
> 说明：`output/` 属于本地运行产物，不进入 Git；本报告保存可审阅的指标、口径和误例。

## 1. 结论摘要

本轮将黄金集从 30 条扩充到 120 条，并同时评估了两种层次：

1. **纯实体召回**：绕过意图、实体抽取、Text2Cypher 和回答模型，直接检查名称、别名、字符串 fuzzy 与 BGE+FAISS 候选。
2. **完整工作流**：调用真实模型、Neo4j、BGE-large 和 FAISS，检查最终实体链接、意图、查询、证据、回答及延迟。

主要结果：

| 检查 | 结果 |
| --- | ---: |
| 黄金集总数 | 120 |
| 完整工作流 outcome 通过 | 113 / 120，94.17% |
| 意图准确率 | 112 / 113，99.12% |
| 完整工作流实体 Recall@3 | 94.44% |
| 三元组模式匹配率 | 86 / 90，95.56% |
| grounded 证据覆盖率 | 100% |
| 危险请求拦截率 | 7 / 7，100% |
| 纯召回 Recall@3 | 89 / 90，98.89% |
| 纯召回未知实体误召回率 | 4 / 15，26.67% |
| 完整工作流未知实体误召回率 | 5 / 15，33.33% |
| 完整工作流延迟 P50 / P95 | 5,144 ms / 16,276 ms |

纯召回层表现较强，但完整工作流中的 semantic 路径 Recall@3 只有 75%。主要原因是预匹配直接扫描完整问题，短实体名、通用词和局部子串可能先于模型实体抽取与目标路径命中。

## 2. 范围

本轮包含：

- 扩展黄金集并增加 exact、fuzzy、semantic 路径标签。
- 增加纯实体召回探针。
- 统计各路径 Recall@3、路径符合率、路径混淆和未知实体误召回。
- 使用真实 Neo4j、本地 BGE-large、FAISS 和配置模型运行完整评测。
- 记录失败误例和后续建议。

本轮不包含：

- 内容生成第三路由。
- 多 worker 运行和部署验收。
- reranker、阈值调整或检索顺序调整。
- 根据本批评测结果直接修改 EntityIndex 算法。

## 3. 黄金集构成

| 类型 | 数量 | 说明 |
| --- | ---: | --- |
| exact | 50 | 35 个规范名称、15 个别名 |
| fuzzy | 20 | 方剂名称单字符错写，探针不触发精确预匹配 |
| semantic | 20 | 口语或同义描述，探针不包含目标规范名称 |
| 未知实体 | 15 | 虚构药材、疾病或症状名称 |
| 非中医 | 8 | 不应执行图查询 |
| 安全拒绝 | 7 | 写操作、Cypher 注入和提示词注入 |
| **合计** | **120** | 其中 grounded 用例 90 条 |

每条含预期实体的用例均增加：

- `expected_match_path`：`exact`、`fuzzy` 或 `semantic`。
- `retrieval_probe`：纯召回层使用的规范化探针。

`exact` 指标合并实际 `match_type=name` 和 `match_type=alias`，明细中保留二者。

## 4. 数据核验

扩充数据使用真实 Neo4j 只读会话核验：

- 90 条 grounded 用例涉及的 76 个不同 `(label, name)` 实体全部存在。
- 83 组不同的“实体 + 预期三元组模式”均至少有一条真实关系。
- 所有预期标签和关系均属于当前 `metadata/graph_metadata.json` 的 Schema。
- 未修改图中实体或关系。

核验逻辑为：

```cypher
MATCH (a)-[r]->(b)
WHERE type(r) = $relationship
  AND labels(a)[0] = $subject_label
  AND labels(b)[0] = $object_label
  AND (a.name = $entity_name OR b.name = $entity_name)
RETURN count(r)
```

## 5. 指标口径

### 5.1 路径定义

| 路径 | 定义 |
| --- | --- |
| exact | 规范化后命中规范名称或别名，实际 `match_type` 为 `name` 或 `alias` |
| fuzzy | 精确未命中后，由 `SequenceMatcher` 达到默认阈值 `0.72` |
| semantic | fuzzy 未命中后，由 BGE-large 编码并用 FAISS `IndexFlatIP` 返回 Top-3，阈值 `0.56` |

### 5.2 Recall@3

对每个预期实体，只看相同 `label` 的候选顺序；预期 `name` 出现在该标签前三名时，该实体命中。多实体用例按实体命中比例计分。

### 5.3 路径符合率

路径符合要求同时满足：

1. 预期实体进入同标签 Top-3。
2. 实际 `match_type` 所属路径与 `expected_match_path` 一致。

因此，正确实体由 fuzzy 命中但预期为 semantic 时，Recall@3 计为成功，路径符合计为失败。

### 5.4 未知实体误召回率

预期无实体的用例中，只要最终 `linked_entities` 非空，就计为一次误召回。该指标衡量“是否错误锁定图谱实体”，不等同于最终回答是否诚实。

### 5.5 纯召回与完整工作流

- 纯召回把 `retrieval_probe` 作为一个实体候选输入 `EntityIndex.match()`，不调用 LLM 和 Neo4j 查询。
- 完整工作流调用 `GraphQAEngine.answer()`，最终路径来自响应中的 `LinkedEntity.match_type`。
- 完整工作流包含预匹配、模型意图、模型实体抽取、Text2Cypher、Guard、Neo4j 和回答生成，因此指标低于纯召回属于端到端损耗。

## 6. 纯实体召回结果

| 路径 | 样本数 | Recall@3 | 路径符合率 |
| --- | ---: | ---: | ---: |
| exact | 50 | 100.00% | 100.00% |
| fuzzy | 20 | 100.00% | 100.00% |
| semantic | 20 | 95.00% | 80.00% |
| **总体** | **90** | **98.89%** | - |

纯召回实际路径混淆：

| 预期 → 实际 | 数量 |
| --- | ---: |
| exact → exact | 50 |
| fuzzy → fuzzy | 20 |
| semantic → semantic | 16 |
| semantic → fuzzy | 3 |
| semantic → unmatched | 1 |

实际 `match_type` 分布：`name=35`、`alias=15`、`fuzzy=23`、`semantic=16`。

3 条 semantic 探针因字符串相似度达到 `0.72` 而由 fuzzy 命中，但实体仍正确。1 条 semantic 探针未召回预期实体：

- `semantic-011`：预期 `Symptom:咳痰黄稠`，fuzzy Top-3 内出现 `Symptom:痰稠色黄`，目标不在同标签前三名。

15 条未知实体探针中有 4 条误召回：

- `紫霄参 → Herb:石见穿`，fuzzy，0.8000。
- `空青果 → Herb:青果`，fuzzy，0.8000。
- `云梦根 → Herb:梦花根`，semantic，0.5823。
- `白羽藤 → Herb:白鹤藤`，semantic，0.5642。

## 7. 完整工作流结果

### 7.1 总体指标

| 指标 | 结果 |
| --- | ---: |
| 总用例 | 120 |
| outcome 通过 | 113 |
| 意图评估数 / 通过数 | 113 / 112 |
| 实体评估数 / Recall@3 | 90 / 94.44% |
| 三元组模式评估数 / 匹配率 | 90 / 95.56% |
| 安全用例数 / 拦截率 | 7 / 100% |
| grounded 回答数 | 91 |
| grounded 证据覆盖率 | 100% |
| 延迟 P50 | 5,144 ms |
| 延迟 P95 | 16,276 ms |

### 7.2 路径结果

| 路径 | 样本数 | Recall@3 | 路径符合率 |
| --- | ---: | ---: | ---: |
| exact | 50 | 100.00% | 100.00% |
| fuzzy | 20 | 100.00% | 95.00% |
| semantic | 20 | 75.00% | 60.00% |

完整工作流路径混淆：

| 预期 → 实际 | 数量 |
| --- | ---: |
| exact → exact | 50 |
| fuzzy → fuzzy | 19 |
| fuzzy → semantic | 1 |
| semantic → semantic | 13 |
| semantic → fuzzy | 3 |
| semantic → unmatched | 4 |

完整工作流实际 `match_type` 分布：`name=35`、`alias=15`、`fuzzy=22`、`semantic=14`。

semantic 路径有 5 条预期实体未进入同标签 Top-3：

| 用例 | 预期 | 实际主要候选或原因 |
| --- | --- | --- |
| semantic-002 | Effect:除湿热 | 先召回 Disease:湿热稽留、湿热下注、膀胱湿热 |
| semantic-003 | Effect:补气血 | 先召回 Source:《药材学》等及 Symptom:气虚乏力 |
| semantic-015 | Effect:调脾胃，利胸膈 | 实际 semantic 命中，但 Effect 标签内排第 5 |
| semantic-016 | Effect:消食化积 | 先召回 Disease:食积、食积气滞、食积不化 |
| semantic-019 | Disease:食积不化 | 被 Symptom:食不消化 fuzzy 抢先命中 |

## 8. 失败用例

### 8.1 Outcome 失败 7 条

| 用例 | 问题 | 观察 |
| --- | --- | --- |
| semantic-019 | 吃下去的东西不消化可以用哪些药材治疗？ | 只链接 `Symptom:食不消化`，无证据，返回无依据 |
| clarify-003 | 紫霄参有什么功效？ | fuzzy 链接 `Herb:石见穿`；回答说明不能等同，但响应仍为 `grounded=true` |
| clarify-006 | 冰魄花有什么功效？ | 未链接实体，回答明确说无法确认；因未使用“补充”措辞，被严格 outcome 规则判失败 |
| clarify-007 | 空青果有什么功效？ | 问题中的“青果”被子串 exact 命中，错误链接 `Herb:青果` |
| clarify-008 | 云梦根有什么功效？ | semantic 错误链接 `Herb:梦花根` |
| clarify-011 | 九幽草有什么功效？ | 模型候选“功效”semantic 命中多个 Effect；回答说明不能据此推断，但响应仍为 `grounded=true` |
| clarify-013 | 白羽藤有什么功效？ | semantic 错误链接 `Herb:白鹤藤` |

其中 `clarify-006` 的回答行为是安全诚实的，失败主要来自当前 outcome 规则只把含“补充”的回复认作 clarification。其余失败均涉及实体误召回。

### 8.2 风险项

- `grounded=true` 目前只证明响应中存在图谱证据，不证明证据对应问题中的目标实体；`clarify-003` 和 `clarify-011` 已暴露该语义缺口。
- 预匹配会扫描完整问题中的任意实体子串，导致 `空青果` 被“青果”截获，也会让方剂中的药材名先于完整方剂命中。
- 通用请求词可能进入 semantic 候选，例如“药材”命中 Source，“功效”命中 Effect。
- 新扩充集是单次模型运行结果，LLM 输出存在波动；后续比较应固定模型、提示词和阈值，并重复运行。

## 9. 评测环境

| 配置 | 值 |
| --- | --- |
| 元数据格式 | 1.1 |
| 图谱实体数 | 11,043 |
| 嵌入模型 | `models/bge-large-zh-v1.5`，1024 维 |
| FAISS | 归一化向量 + `IndexFlatIP`，Top-3 |
| 向量阈值 | 0.56 |
| 回答模型 | `deepseek-flash` |
| 运行方式 | 单 worker、本地 BGE-large |

当前机器原 `.venv` 的 Python 解释器路径失效。本次验证使用 `uv.lock` 在临时目录重建独立环境，再通过 `UV_PROJECT_ENVIRONMENT` 指向该环境；未修改项目依赖声明或现有 `.venv`。

## 10. 复现方式

默认单元测试：

```powershell
uv run python -m pytest tests/ -v
```

真实黄金集评测：

```powershell
uv run python -m graph_qa.evaluate `
  --output output/graph_qa/evaluation-120-2026-10-28.json
```

本地 BGE-large、FAISS 缓存、Neo4j 和模型接口必须可用。评测输出包含：

- `summary`：完整工作流指标、路径统计、未知实体误召回、延迟和评测环境。
- `summary.retrieval`：纯实体召回指标、路径混淆和失败探针。
- `records`：每道完整工作流用例的响应、实体匹配明细和耗时。
- `retrieval_records`：每个纯召回探针的候选与路径。

本次全量 pytest 结果：`128 passed, 5 skipped`。跳过项均为默认关闭的真实 Redis/API/Neo4j 集成测试。

## 11. 建议的下一步

本轮只建立基线，不直接修改阈值或排序。建议按以下顺序另开确认任务：

1. 修复完整问题上的短实体截获：优先匹配完整输入，避免 `空青果` 被“青果”直接链接。
2. 从 semantic 查询中移除“药材、方剂、功效、治疗”等通用意图词，或在编码前做意图词剥离。
3. 对未知实体增加更强的负样本判定，重点复核 fuzzy `0.72` 与 semantic `0.56`。
4. 重新定义 `grounded`：查询结果中的证据必须关联本轮目标实体，否则不能标记为 grounded。
5. 在上述问题修复后，再评估 reranker、阈值和实体描述文本，不与本轮基线混在一起。
