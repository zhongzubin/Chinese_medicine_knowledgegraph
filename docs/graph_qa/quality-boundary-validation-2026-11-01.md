# 未知实体、无依据回答与 200 条产品集验证

> 验证日期：2026-11-01
>
> 分支：`codex/product-quality-v2`
>
> 状态：未知实体、无依据、来源缺失、200 条产品集和 62 轮多轮集已完成

## 1. 实现范围

本轮完成以下用户可见边界：

- 具体名称形态的 Herb、Formula、Source 不允许仅凭 semantic 候选建立实体链接。
- 精确名称、别名和高置信 fuzzy 匹配保持原优先级。
- 明确的中医属性/关系问法即使模型误判为范围外，也先进入图谱实体确认流程。
- 未解析代词不进入 semantic 实体链接，避免把“它归经是什么”链接到文献实体。
- 未确认实体返回澄清，不生成医疗事实，也不执行图谱查询。
- 已确认实体但没有可验证信息时，明确说明“当前图谱没有找到足以回答该问题的依据”。
- 图谱属性没有来源时明确显示“未提供出处”。
- 保留 `NO_GRAPH_EVIDENCE` 作为查询数据无法构造合法证据时的完整性错误。

新增 `scripts/build_product_quality_cases.py`，从当前 Neo4j 只读查询生成固定产品集。生成器不写入图数据库，不调用模型。

## 2. 产品固定集

文件：`evaluation/product_quality_cases.jsonl`

| 类别 | 数量 |
| --- | ---: |
| 药材属性 | 32 |
| 方剂属性 | 13 |
| 正向关系 | 35 |
| 反向关系 | 10 |
| 聚合统计 | 20 |
| 多条件列表 | 10 |
| 白名单两跳 | 10 |
| semantic 同义正例 | 25 |
| 未知实体 | 20 |
| 已知实体但无证据 | 15 |
| 图谱无来源披露 | 10 |
| 合计 | 200 |

约束：

- 200 个 id 唯一。
- 200 个问题文本唯一。
- 正向事实、聚合和路径期望来自当前 Neo4j。
- 未知实体名称与当前图谱无名称冲突。
- 已知实体无证据样本排除名称被其他实体作为别名复用的歧义节点。

## 3. 自动测试

命令：

```powershell
uv run --no-cache python -m pytest tests/ -v
```

结果：

- `221 passed`
- `5 skipped`
- `3 warnings`

跳过项为默认未启用的真实 Redis/API/Neo4j 集成测试。

## 4. 200 条产品集

命令：

```powershell
uv run --no-cache python -m graph_qa.evaluate `
  --cases evaluation/product_quality_cases.jsonl `
  --output output/graph_qa/product-quality-200-final-2026-11-01.json
```

结果：

| 指标 | 结果 | 门槛 |
| --- | ---: | ---: |
| 用例数 | 200 | - |
| outcome 通过率 | 100% | 观察指标 |
| 意图准确率 | 100% | ≥ 90% |
| 实体 Recall@3 | 98.89% | ≥ 85% |
| 属性事实匹配率 | 98.18%，55 条 | ≥ 90% |
| 聚合匹配率 | 95%，20 条 | ≥ 90% |
| 答案包含匹配率 | 100%，20 条 | ≥ 90% |
| 结果对象匹配率 | 100%，10 条 | ≥ 95% |
| 受控两跳路径匹配率 | 100%，10 条 | ≥ 90% |
| Cypher 通过率 | 100% | ≥ 90% |
| 查询执行成功率 | 100% | ≥ 95% |
| grounded 证据覆盖率 | 100% | 100% |
| 未知实体误召回率 | 0%，20 条 | 0% |
| 未知实体澄清率 | 100%，20 条 | 100% |
| 无依据诚实率 | 100%，15 条 | 100% |
| 无来源披露率 | 100%，10 条指定用例 | 100% |
| P50 / P95 | 36 ms / 9,586 ms | 无 |

`acceptance.passed=true`。结果对象和路径匹配为 100%，答案包含为 100%。剩余 semantic 路径差异属于模型候选和 fuzzy/semantic 路径选择差异，没有造成 outcome 失败或错误 grounded。

## 5. 120 条回归

命令：

```powershell
uv run --no-cache python -m graph_qa.evaluate `
  --cases evaluation/qa_cases.jsonl `
  --output output/graph_qa/evaluation-120-unknown-boundary-final-2026-11-01.json
```

结果：

| 指标 | 结果 |
| --- | ---: |
| outcome | 119/120，99.17% |
| 意图准确率 | 100% |
| 实体 Recall@3 | 97.78% |
| Cypher 通过率 | 100% |
| 查询执行成功率 | 100% |
| 危险请求拦截率 | 100% |
| grounded 证据覆盖率 | 100% |
| 未知实体误召回率 | 0%，15 条 |
| 未知实体澄清率 | 100%，14 条 |

`acceptance.passed=true`。唯一 outcome 剩余项是既有 `semantic-001`：“热毒”被链接到 Disease 而非目标 Effect，但回答保持诚实且未伪造 grounded 证据。

## 6. 多轮回归

最终结果：

| 指标 | 结果 |
| --- | ---: |
| 场景数 | 20 |
| 轮数 | 62 |
| outcome 通过率 | 100% |
| 实体匹配率 | 100%，57 个实体检查 |
| 失败场景 | 0 |
| 验收 | `acceptance.passed=true` |

期间发现并修复了澄清后继续追问“它归经是什么”时 semantic 误链接《本经》的问题。未解析代词现在不会进入 semantic 实体链接。

## 7. 真实浏览器验收

使用本地 FastAPI、Streamlit、Redis、真实 Neo4j 和真实模型接口：

1. “玄霜参有什么功效？” 返回实体澄清，未链接其他药材。
2. “一匹绸有哪些禁忌？” 返回“已识别到相关图谱实体，但当前图谱没有找到足以回答该问题的依据”。
3. “一品红的性味是什么？” 返回真实属性并明确显示“未提供出处”。
4. “麻黄性味归经是什么？” 正常返回性味和归经，原有正向路径不回归。

截图：

```text
output/playwright/unknown-entity-clarification-2026-11-01.png
output/playwright/no-evidence-response-2026-11-01.png
output/playwright/source-missing-disclosure-2026-11-01.png
output/playwright/unknown-boundary-positive-regression-2026-11-01.png
```

## 8. 结论与边界

已达到：

- 显式未知实体不误链接。
- 已知实体无信息时不编造。
- 无来源事实明确披露。
- 200 条产品固定集接受门禁通过。
- 原 120 条安全、Cypher、执行和 grounded 不变量通过。

尚未完成：

- 50 条独立人工事实复核；当前完成的是图谱生成期望和自动逐项比对。
- 独立 Agent 拓扑或动态调度。
