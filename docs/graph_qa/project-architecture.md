# 项目架构图

> 当前阶段：受控单 Agent 闭环 MVP
>
> 图示范围：本地单 worker、固定 LangGraph 拓扑、只读 Neo4j 查询

![中医知识图谱问答系统架构](assets/project-architecture.png)

## 1. 总体架构

```mermaid
flowchart TB
    subgraph Clients["用户入口"]
        UI["Streamlit 双栏界面"]
        CLI["CLI"]
        APIClient["API 客户端"]
    end

    subgraph Access["FastAPI 接入、安全与会话"]
        API["FastAPI\nPOST /qa / QA Stream / Session Delete"]
        Security["API Key · 限流 · 并发租约"]
        Redis[("Redis\n5 轮会话 · TTL · 会话锁")]
    end

    subgraph Workflow["固定 LangGraph 工作流 · 17 节点"]
        Dialogue["dialogue\n规范化与历史改写"]
        Intent["intent\n实体预匹配与领域意图"]
        Entity["entity\n候选抽取与实体链接"]
        Retrieval["retrieval\n受控循环 Agent\nplan → act → observe → replan"]
        Answer["answer\n证据回答或直接回答"]
        Audit["evidence_audit\n确定性证据覆盖校验"]
    end

    subgraph RetrievalLayer["受控检索与证据链"]
        Catalog["查询规划目录\n两跳 → 属性 → 聚合\n多条件 → 关系 → Schema Cypher"]
        Guard["Cypher Guard\n只读 · 白名单 · 参数 · LIMIT"]
        Explain["EXPLAIN"]
        Repository["Neo4jRepository\n只读事务 · 超时 · 结果上限"]
        Evidence["统一证据\nEvidence · AttributeFact\nAggregateFact · PathEvidence"]
    end

    subgraph Dependencies["外部依赖与本地资源"]
        Neo4j[("Neo4j\n11,043 实体 · 26,324 关系")]
        Model["OpenAI 兼容模型接口"]
        Vector["BGE-large + FAISS\nTop-3 + 阈值召回"]
    end

    UI --> API
    CLI --> API
    APIClient --> API
    API --> Security
    Security <--> Redis
    Security --> Dialogue
    Dialogue --> Intent --> Entity --> Retrieval --> Answer --> Audit
    Retrieval -.观察结果后重新规划.-> Retrieval
    Intent -.非中医.-> Answer
    Entity <--> Vector
    Dialogue <--> Model
    Intent <--> Model
    Entity <--> Model
    Answer <--> Model
    Retrieval --> Catalog --> Guard --> Explain --> Repository --> Evidence
    Repository <--> Neo4j
    Evidence --> Audit
    Audit --> API
```

## 2. Agent 现状

| Agent | 当前实现 | 是否独立子图 |
| --- | --- | --- |
| `dialogue` | 模型调用角色封装，负责上下文改写并支持历史降级 | 否 |
| `intent` | 确定性实体预匹配 + 模型领域意图分类 | 否 |
| `entity` | 模型候选抽取 + 确定性 exact/fuzzy/FAISS 链接 | 否 |
| `retrieval` | 真实受控闭环 Agent，有独立契约并执行规划、观察和预算内重规划 | 否 |
| `answer` | 模型回答角色封装，失败时使用证据化回答 | 否 |
| `evidence_audit` | 真实确定性 Agent，逐项证据覆盖并回退 | 否 |

当前所有 Agent 都运行在固定 LangGraph 主拓扑中。`retrieval` 已具备 `plan → act → observe → replan` 闭环，`retrieval` 和 `evidence_audit` 都是实际可测试 Agent；但还没有为每个 Agent 编译独立子图，也没有动态 Agent 调度。

## 3. 检索规划顺序

```mermaid
flowchart LR
    MultiHop["白名单两跳"] --> Attribute["属性事实"]
    Attribute --> Aggregate["受控聚合"]
    Aggregate --> MultiCondition["多条件列表"]
    MultiCondition --> Relation["唯一方向关系模板"]
    Relation --> Text2Cypher["Schema 约束 Text2Cypher"]
```

`RetrievalAgent` 只输出上述计划之一，不持有 Cypher Guard 或仓储，不能执行查询。查询必须继续经过 Guard、`EXPLAIN`、只读仓储和确定性证据归一化。

## 4. 安全与证据不变量

- Neo4j 始终在只读会话中执行。
- 不接受任意 Cypher、模型自治执行、动态路径或变量长度路径。
- 用户输入只能通过查询参数传入。
- grounded 回答必须包含本轮真实图谱证据。
- 未知实体只澄清，不生成医疗事实。
- 无依据时明确说明；无来源时显示“未提供出处”。
- 回答审核使用确定性覆盖检查，不暴露模型隐藏推理。

## 5. 重新生成图片

```powershell
uv run python scripts/render_architecture_diagram.py
```

输出文件：

```text
docs/graph_qa/assets/project-architecture.png
```
