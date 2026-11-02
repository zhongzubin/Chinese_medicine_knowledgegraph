# 检索规划 Agent 实现与验收记录

> 验证日期：2026-11-01
>
> 代码基线：`80ddc7b`
>
> 分支：`codex/product-quality-v2`

## 1. 实现范围

本轮将 `retrieval` 从声明式角色升级为真实、可测试的确定性检索规划 Agent：

- 新增 `RetrievalAgent` 和独立 `RetrievalPlanningInput`、`RetrievalPlanningResult` 契约。
- 计划类型固定为受控两跳、属性、聚合、多条件列表、受控关系和 Schema 约束回退。
- 保持既有规划优先级和查询计划对象，不改变属性、关系、聚合或路径证据结构。
- `RetrievalAgent` 只接收问题、已链接实体、Schema、重试状态和受限模型生成器，不持有 Cypher Guard 或仓储。
- `plan_cypher` 节点改为 Agent 结果映射，`validate_cypher`、`execute_cypher`、只读仓储和确定性证据校验保持原职责。
- 多跳查询仍固定为四种白名单两跳路径，每步由 Schema 校验且固定两个步骤。
- `QueryInfo.plan_kind` 公开稳定策略代码；SSE `plan_cypher` 增加 `plan_kind` 和中文 `plan_label`。
- Streamlit 处理进度展示“图谱属性”“受控统计”“受控两跳”等安全策略名称，不展示 Cypher 或模型隐藏推理。

## 2. 自动化测试

命令：

```powershell
uv run --no-cache python -m pytest tests/ -v
```

结果：

- `213 passed`
- `5 skipped`
- `3 warnings`

跳过项仍是需要显式启用的真实 Redis/API/Neo4j 集成测试。

新增测试覆盖：

- 六种正常检索计划和固定规划优先级。
- 输出最多携带一个且类型匹配的计划对象。
- Text2Cypher 回退只能引用已链接实体，参数必须包含链接实体名称。
- 模型错误和 3 次尝试使用稳定错误码收口。
- 四种白名单两跳目录固定、节点连续且只能包含两个步骤。
- Agent 不持有 Guard 或仓储。
- 工作流委托、API `query.plan_kind`、SSE 安全策略字段和 Streamlit 标签展示。

## 3. 产品固定集

命令：

```powershell
uv run --no-cache python -m graph_qa.evaluate `
  --cases evaluation/product_quality_cases.jsonl `
  --output output/graph_qa/retrieval-agent-product-2026-11-01.json
```

结果：

| 指标 | 结果 |
| --- | ---: |
| 用例数 | 19 |
| outcome 通过率 | 100% |
| 实体 Recall@3 | 100% |
| 属性事实匹配率 | 100%，10 条 |
| 单跳/多条件聚合匹配率 | 100%，6 条 |
| 答案包含匹配率 | 100%，3 条 |
| 结果对象匹配率 | 100%，1 条 |
| 受控多跳路径匹配率 | 100%，2 条 |
| Cypher 通过率 | 100% |
| 查询执行成功率 | 100% |
| grounded 覆盖率 | 100% |
| 回答审核通过 | 19/19 |
| P50 / P95 | 105 ms / 624 ms |

最终计划类型分布：

| `plan_kind` | 数量 |
| --- | ---: |
| `controlled_attribute` | 10 |
| `controlled_aggregate` | 6 |
| `controlled_multi_condition_list` | 1 |
| `controlled_multi_hop` | 2 |

`acceptance.passed=true`。

## 4. 多轮固定集

命令：

```powershell
uv run --no-cache python -m graph_qa.multi_turn_evaluate `
  --cases evaluation/multi_turn_cases.jsonl `
  --output output/graph_qa/retrieval-agent-multi-turn-final-2026-11-01.json
```

最终结果：

| 指标 | 结果 |
| --- | ---: |
| 场景数 | 20 |
| 轮数 | 62 |
| outcome 通过率 | 100% |
| 实体匹配率 | 100%，57 个实体检查 |
| 失败场景 | 0 |
| 验收 | `acceptance.passed=true` |

在受限网络首次重放时，需要外网模型调用的少数轮次返回 `MODEL_UNAVAILABLE`，完整集 outcome 为 90.32%，仍达到文档门槛。按环境规则在授权网络下重跑后恢复为 62/62，确认失败来自模型调用网络边界，不是检索规划差异。

## 5. 真实浏览器验收

使用本地 FastAPI + Streamlit 和真实 Neo4j 完成浏览器测试：

1. “麻黄性味归经是什么？”显示“生成图谱查询（图谱属性）”，返回性味、归经和来源缺失提示。
2. “含有人参的方剂有多少种？”显示“生成图谱查询（受控统计）”，返回 128。
3. “四君子汤所含药材有什么功效？”显示“生成图谱查询（受控两跳）”，返回四种药材功效和“多跳证据”。
4. 三轮复用同一会话；回答校验、属性、统计和多跳证据区域正常。
5. 页面未展示 Cypher、模型提示词或隐藏推理。

截图：

```text
output/playwright/retrieval-agent-attribute-2026-11-01.png
output/playwright/retrieval-agent-aggregate-2026-11-01.png
output/playwright/retrieval-agent-multi-hop-2026-11-01.png
```

输出目录被 Git 忽略，不作为恢复基线依赖。

## 6. 边界与结论

本轮没有新增：

- 任意 Cypher 或模型自治执行。
- 动态、任意深度多跳路径。
- 图谱写入或数据库权限放宽。
- 内容生成、多 worker 运行或公网部署能力。
- 独立 Agent 子图或动态调度。

检索规划已具备独立、可序列化、可单元测试的输入输出契约，同时保留现有安全、只读和证据边界。

## 7. 最小 Agent 闭环补充

后续补充了主图内的受控 Agent 循环：

- 新增 `retrieve_observe` 节点。
- Agent 观察校验是否通过、是否执行出错、是否发生属性空回退、是否已有证据和返回行数。
- 观察结果映射为 `replan`、`answer_from_graph`、`no_evidence`、`safe_fail` 或 `finish`。
- 每个循环步骤有稳定 `reason_code`，最多三次。
- SSE 和 Streamlit 展示步数、决策和原因码，不暴露隐藏推理。
- Guard、`EXPLAIN`、只读仓储和证据审核职责未改变。

相关与全量测试最终为 `227 passed, 5 skipped`。当前仍是固定主图内的单 Agent 闭环，不是独立 Agent 子图或动态多 Agent 调度。
