# 本地演示版运行与验收

> 状态：本地单 worker 演示版
>
> 适用范围：一台开发机、本机 Redis、本机 Neo4j、单个 API 和 Streamlit 实例
>
> 不适用：公开上线、多用户认证、多 worker 或在线医疗诊断
>
> 最后更新：2026-11-01

## 1. 演示范围

本地演示版提供：

- Streamlit 对话页面。
- FastAPI 非流式与 SSE 流式接口。
- 最近 5 轮会话、会话清空、限流和全局并发控制。
- 中医意图识别、实体召回、Text2Cypher、只读 Neo4j 查询。
- 公开处理阶段、流式回答、属性/关系统计/多跳路径证据和回答校验状态。
- 医疗知识参考提示，不提供诊断或处方。

前后端均已实现，并已补充属性、聚合、多条件列表和受控多跳的展示与验收记录。

## 2. 前置条件

- Windows PowerShell 7。
- 已安装 `uv`。
- Redis 正在监听 `127.0.0.1:6379`。
- Neo4j 正在监听 `127.0.0.1:7687`，且图谱数据已导入。
- 根目录存在 `.env`，包含 `MODEL_*`、`NEO4J_*` 和向量模型配置。
- 本地 BGE-large 模型路径可用；首次构建 FAISS 可能约需 10 分钟。

`.env` 中的真实密钥、密码和本地模型不会被提交。

## 3. 一键启动

```powershell
pwsh -File scripts\start_local_demo.ps1 -OpenBrowser
```

脚本会：

1. 检查 `.env`、Redis 和 Neo4j。
2. 使用 `uv.lock` 同步依赖。
3. 在内存中生成一次性本地 API Key。
4. 隐藏启动 FastAPI 和 Streamlit。
5. 等待两个服务健康检查通过。
6. 保存进程 PID 和日志路径，不保存 API Key。

默认地址：

```text
Streamlit: http://127.0.0.1:8501
API:       http://127.0.0.1:8000
```

## 4. 停止

```powershell
pwsh -File scripts\stop_local_demo.ps1
```

停止脚本只结束 PID 文件中记录、且路径指向本项目 `.venv` 的 Python 进程。

## 5. 手工启动

需要分别调试前后端时：

```powershell
uv sync --locked
uv run uvicorn graph_qa.api:app --host 127.0.0.1 --port 8000
uv run streamlit run graph_qa/streamlit_app.py
```

手工启动前需要自行配置 API Key 和 Streamlit Key，配置示例见
[.env.example](../../.env.example)。

## 6. 演示验收流程

1. 打开 Streamlit 页面，确认显示标题和医疗免责声明。
2. 输入“麻黄有什么功效？”。
3. 确认左侧显示规范化、实体匹配、查询校验和回答等阶段。
4. 确认右侧逐步显示回答。
5. 展开“图谱证据”，确认存在真实节点、关系和来源。
6. 输入“它有什么禁忌？”验证最近 5 轮上下文改写。
7. 点击“清空当前会话”，确认会话标识被清除。
8. 输入非中医问题，确认不执行图查询并标记未使用图谱证据。
9. 输入危险数据库操作，确认请求在模型调用前被拒绝。

## 7. 日志与故障排查

运行日志位于：

```text
output/local-demo/api.out.log
output/local-demo/api.err.log
output/local-demo/streamlit.out.log
output/local-demo/streamlit.err.log
```

常见检查：

```powershell
netstat -ano | findstr ":6379 :7687 :8000 :8501"
Invoke-RestMethod http://127.0.0.1:8000/health
```

- Redis/Neo4j 未监听时，启动脚本会直接停止。
- 端口被占用时，可通过 `-ApiPort` 和 `-StreamlitPort` 修改演示端口。
- API 首次启动需要加载 BGE-large；默认最多等待 900 秒。
- 如果依赖损坏，可重新执行 `uv sync --locked`。

## 8. 已知限制

- 只面向本机演示，不是公网生产部署。
- 使用静态本地 API Key，不代表最终用户登录体系。
- 单 worker 运行，不把多 worker 作为验收目标。
- 当前覆盖白名单属性、一跳正向/反向关系、单跳和多条件计数、多条件结果列表和四种 Schema 白名单两跳；任意深度或动态规划多跳尚未实现。
- Redis 5 轮会话已通过 20 组、62 轮固定集；更长上下文和跨端用户身份仍不在当前范围。
- 本次浏览器验证中，追问“它有什么禁忌？”完整等待约 70 秒；正确答案最终返回，
  但模型阶段仍有明显延迟波动。
- 失败轮次会话 ID 隔离问题已修复：新会话先建立空历史，失败回答不写入历史。
- 当前固定集仍有 4 条已知质量边界，详细记录见
  [retrieval-boundary-fix-2026-10-28.md](retrieval-boundary-fix-2026-10-28.md)。
- 产品级属性事实、答案审核和多轮验收计划见
  [product-quality-roadmap.md](product-quality-roadmap.md)。
- 首轮实现与指标见
  [product-quality-validation-2026-10-31.md](product-quality-validation-2026-10-31.md)。
- 内容仅供中医药知识参考，不能替代专业医疗诊断和治疗。

## 9. 本地演示验收记录

验收日期：2026-10-29。

环境：

- Windows + PowerShell 7。
- 本地 Redis `127.0.0.1:6379`。
- 本地 Neo4j `127.0.0.1:7687`。
- 本地 BGE-large + FAISS 缓存。
- `deepseek-flash` 模型接口。
- FastAPI 单 worker + Streamlit。

一键启动脚本验证：

- `uv sync --locked` 成功。
- FastAPI `/health` 返回 `status=ok`。
- Streamlit 首页健康检查通过。
- 进程 PID 和日志写入 `output/local-demo/`。
- 未把临时 API Key 写入文件。

浏览器验证：

- “麻黄有什么功效？”返回平喘、利水、发汗，并展示 3 条 `HAS_EFFECT` 证据。
- 左侧依次展示规范化、实体匹配、查询校验、执行和回答阶段。
- “它有什么禁忌？”能够从上一轮解析出麻黄，并诚实说明图谱没有禁忌依据。
- “四君子汤由哪些药材组成？”返回人参、白术、茯苓、甘草及对应组成证据。
- “清空当前会话”后页面恢复初始状态，会话标识消失。
- 截图：`output/playwright/local-demo-2026-10-29.png`。

一键停止脚本验证：

- API 和 Streamlit 进程均正常结束。
- 8000/8501 端口释放。
- PID 文件被清理。

未覆盖项：

- 非中医和安全拒绝在浏览器中未重复实测，但已有 API/工作流单元测试覆盖。
- 任意深度多跳、动态 Agent 调度和 200 条以上产品集尚未纳入本轮验收。
- 未进行公网、多用户、多实例或容量压测。

### 9.1 专业问答质量 v1 浏览器补充验收

日期：2026-10-31。

- 输入“麻黄性味归经是什么？”，返回性味、归经并展示两条属性事实。
- 展开“图谱属性”后显示字段值及“当前图谱未提供出处”。
- 继续输入“它有什么禁忌？”，正确继承麻黄实体并返回禁忌属性。
- 两轮使用同一个 `session_id`。
- “含有人参的方剂有多少种？”返回图谱计数和“图谱统计”区域。
- 继续输入“它有什么功效？”，正确继承人参实体。
- “同时含有人参和白术的方剂有多少种？”返回交集计数 45。
- 截图：`output/playwright/product-quality-final-2026-10-31.png`。
- 聚合截图：`output/playwright/aggregate-final-2026-10-31.png`。
- 多条件截图：`output/playwright/multi-condition-final-2026-10-31.png`。

### 9.2 多条件列表与受控两跳浏览器验收

日期：2026-10-31。

- 输入“同时含有人参和白术的方剂有哪些？”，页面返回 45 个结果，首个结果为三痹汤。
- 展开“图谱证据”后，每个结果均展示人参、白术对应的 `HAS_INGREDIENT` 关系和来源。
- 输入“四君子汤所含药材有什么功效？”，页面按人参、甘草、白术、茯苓返回功效。
- 展开“多跳证据”后，展示完整的 `Formula -HAS_INGREDIENT-> Herb -HAS_EFFECT-> Effect` 两跳链路及来源。
- 两次请求均复用当前 Streamlit `session_id`，公开处理阶段与回答正常结束。
- 多条件截图：`output/playwright/multi-condition-final-2026-10-31.png`。
- 多跳截图：`output/playwright/multi-hop-final-2026-10-31.png`。

### 9.3 回答校验 Agent 浏览器验收

日期：2026-11-01。

- 输入“麻黄性味归经是什么？”，页面返回性味、归经两项属性。
- 展开“回答校验”，显示审核 Agent 为 `evidence_audit`，覆盖 `2/2` 项证据。
- 页面显示属性 2 项，并明确提示 2 项未提供图谱出处。
- 同一区域同时展示“已逐项核对 2 项图谱证据，回答覆盖全部可验证事实”。
- 浏览器验收发现并修复了原“至少命中一项即通过”的缺陷；当前部分覆盖会回退为证据化回答。
- 截图：`output/playwright/answer-audit-final-2026-11-01.png`。
