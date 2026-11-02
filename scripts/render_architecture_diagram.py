# -*- coding: utf-8 -*-
"""渲染当前中医知识图谱问答系统架构图。"""

from pathlib import Path
from typing import Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = (
    PROJECT_ROOT
    / "docs"
    / "graph_qa"
    / "assets"
    / "project-architecture.png"
)
FONT_REGULAR = Path("C:/Windows/Fonts/msyh.ttc")
FONT_BOLD = Path("C:/Windows/Fonts/msyhbd.ttc")

WIDTH = 2200
HEIGHT = 1500
BACKGROUND = "#F7F8FA"
TEXT = "#1F2937"
MUTED = "#5B6573"
ARROW = "#718096"
CLIENT_FILL = "#E8F1FB"
CLIENT_BORDER = "#2B6CB0"
API_FILL = "#E7F5EF"
API_BORDER = "#2F855A"
AGENT_FILL = "#FFF5DC"
AGENT_BORDER = "#B7791F"
RETRIEVAL_FILL = "#F3E8FA"
RETRIEVAL_BORDER = "#6B46C1"
EVIDENCE_FILL = "#FDE8E8"
EVIDENCE_BORDER = "#C53030"
EXTERNAL_FILL = "#EDF1F5"
EXTERNAL_BORDER = "#4A5568"

Box = Tuple[int, int, int, int]


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(str(path), size=size)


TITLE_FONT = font(48, bold=True)
SUBTITLE_FONT = font(23)
LANE_FONT = font(28, bold=True)
BOX_TITLE_FONT = font(24, bold=True)
BOX_BODY_FONT = font(19)
SMALL_FONT = font(17)
BADGE_FONT = font(17, bold=True)


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    box: Box,
    text: str,
    *,
    text_font: ImageFont.FreeTypeFont,
    fill: str = TEXT,
) -> None:
    left, top, right, bottom = box
    bounds = draw.textbbox((0, 0), text, font=text_font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.text(
        (
            left + (right - left - width) / 2,
            top + (bottom - top - height) / 2 - bounds[1],
        ),
        text,
        fill=fill,
        font=text_font,
    )


def draw_box(
    draw: ImageDraw.ImageDraw,
    xy: Box,
    title: str,
    lines: Sequence[str] = (),
    *,
    fill: str,
    border: str,
    title_color: str = TEXT,
) -> None:
    left, top, right, bottom = xy
    draw.rounded_rectangle(
        xy,
        radius=18,
        fill=fill,
        outline=border,
        width=3,
    )
    title_height = 42 if lines else bottom - top
    draw_centered_text(
        draw,
        (left + 14, top + 10, right - 14, top + title_height),
        title,
        text_font=BOX_TITLE_FONT,
        fill=title_color,
    )
    if not lines:
        return
    y = top + 55
    for line in lines:
        draw.text(
            (left + 22, y),
            line,
            fill=TEXT,
            font=BOX_BODY_FONT,
        )
        y += 27


def draw_lane(
    draw: ImageDraw.ImageDraw,
    xy: Box,
    title: str,
    *,
    fill: str,
    border: str,
) -> None:
    draw.rounded_rectangle(
        xy,
        radius=24,
        fill=fill,
        outline=border,
        width=2,
    )
    draw.text(
        (xy[0] + 24, xy[1] + 14),
        title,
        fill=border,
        font=LANE_FONT,
    )


def draw_arrow(
    draw: ImageDraw.ImageDraw,
    points: Sequence[Tuple[int, int]],
    *,
    color: str = ARROW,
    width: int = 4,
    arrow_size: int = 13,
) -> None:
    draw.line(points, fill=color, width=width, joint="curve")
    (x1, y1), (x2, y2) = points[-2], points[-1]
    if x1 == x2:
        direction = 1 if y2 > y1 else -1
        polygon = [
            (x2, y2),
            (x2 - arrow_size, y2 - direction * arrow_size),
            (x2 + arrow_size, y2 - direction * arrow_size),
        ]
    elif y1 == y2:
        direction = 1 if x2 > x1 else -1
        polygon = [
            (x2, y2),
            (x2 - direction * arrow_size, y2 - arrow_size),
            (x2 - direction * arrow_size, y2 + arrow_size),
        ]
    else:
        polygon = [
            (x2, y2),
            (x2 - arrow_size, y2 - arrow_size),
            (x2 + arrow_size, y2 + arrow_size),
        ]
    draw.polygon(polygon, fill=color)


def draw_badge(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    *,
    fill: str,
    border: str,
) -> None:
    bounds = draw.textbbox((0, 0), text, font=BADGE_FONT)
    width = bounds[2] - bounds[0] + 28
    draw.rounded_rectangle(
        (x, y, x + width, y + 36),
        radius=18,
        fill=fill,
        outline=border,
        width=2,
    )
    draw.text(
        (x + 14, y + 7),
        text,
        fill=border,
        font=BADGE_FONT,
    )


def render() -> Path:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    draw.text((70, 35), "中医知识图谱问答系统架构", fill=TEXT, font=TITLE_FONT)
    draw.text(
        (72, 100),
        "当前实现 · 固定 LangGraph 拓扑 · 只读图查询 · 证据约束回答",
        fill=MUTED,
        font=SUBTITLE_FONT,
    )
    draw_badge(
        draw,
        1780,
        48,
        "Agent Loop MVP",
        fill="#E8F1FB",
        border=CLIENT_BORDER,
    )

    # User entry
    draw_lane(
        draw,
        (55, 145, 2145, 305),
        "用户入口",
        fill="#F3F7FC",
        border=CLIENT_BORDER,
    )
    draw_box(
        draw,
        (90, 195, 620, 280),
        "Streamlit 双栏界面",
        ("进度 / 回答 / 证据 / 回答校验",),
        fill=CLIENT_FILL,
        border=CLIENT_BORDER,
    )
    draw_box(
        draw,
        (670, 195, 1150, 280),
        "CLI",
        ("JSON / 文本输出",),
        fill=CLIENT_FILL,
        border=CLIENT_BORDER,
    )
    draw_box(
        draw,
        (1200, 195, 2110, 280),
        "API 客户端",
        ("普通 POST / SSE 流式消费",),
        fill=CLIENT_FILL,
        border=CLIENT_BORDER,
    )

    # Gateway
    draw_lane(
        draw,
        (55, 330, 2145, 545),
        "FastAPI 接入、安全与会话",
        fill="#F2FAF6",
        border=API_BORDER,
    )
    draw_box(
        draw,
        (90, 385, 650, 505),
        "FastAPI",
        (
            "POST /qa · POST /qa/stream",
            "DELETE /sessions/{session_id} · health",
        ),
        fill=API_FILL,
        border=API_BORDER,
    )
    draw_box(
        draw,
        (700, 385, 1310, 505),
        "安全与并发控制",
        (
            "API Key → client_id",
            "限流 · 全局并发租约 · 错误码映射",
        ),
        fill=API_FILL,
        border=API_BORDER,
    )
    draw_box(
        draw,
        (1360, 385, 2110, 505),
        "Redis",
        (
            "最近 5 轮会话 · TTL · 会话锁",
            "不保存 Cypher / 提示词 / 错误堆栈",
        ),
        fill=EXTERNAL_FILL,
        border=EXTERNAL_BORDER,
    )
    draw_arrow(draw, [(355, 280), (355, 335), (370, 335), (370, 385)])
    draw_arrow(draw, [(910, 280), (910, 335), (900, 335), (900, 385)])
    draw_arrow(draw, [(1655, 280), (1655, 335), (1655, 385)])
    draw_arrow(draw, [(1360, 445), (1310, 445)], color=API_BORDER)
    draw_arrow(draw, [(1310, 470), (1360, 470)], color=API_BORDER)

    # Workflow
    draw_lane(
        draw,
        (55, 575, 2145, 870),
        "固定 LangGraph 工作流（17 个节点，无动态调度）",
        fill="#FFFAED",
        border=AGENT_BORDER,
    )
    agent_boxes = (
        (80, "dialogue", "规范化 · 5 轮改写"),
        (410, "intent", "实体预匹配 · 领域意图"),
        (740, "entity", "候选抽取 · exact / fuzzy / FAISS"),
        (1070, "retrieval", "受控循环 · 规划 → 观察 → 再规划"),
        (1400, "answer", "图谱回答 · 非中医直接回答"),
        (1730, "evidence_audit", "逐项支持校验 · 证据化回退"),
    )
    for x, name, description in agent_boxes:
        draw_box(
            draw,
            (x, 640, x + 280, 790),
            name,
            (description,),
            fill=AGENT_FILL,
            border=AGENT_BORDER,
        )
    for index in range(len(agent_boxes) - 1):
        left = agent_boxes[index][0] + 280
        right = agent_boxes[index + 1][0]
        draw_arrow(draw, [(left + 8, 715), (right - 8, 715)])
    draw_arrow(draw, [(360, 545), (220, 575), (220, 640)])

    draw_box(
        draw,
        (1140, 800, 1500, 850),
        "直接回答（grounded=false）",
        (),
        fill=CLIENT_FILL,
        border=CLIENT_BORDER,
    )
    draw_box(
        draw,
        (1560, 800, 2090, 850),
        "澄清 / 无依据 / 安全失败",
        (),
        fill=EVIDENCE_FILL,
        border=EVIDENCE_BORDER,
    )
    draw_arrow(draw, [(410, 790), (410, 815), (1140, 815)])
    draw_arrow(draw, [(1210, 790), (1210, 800)])
    draw_arrow(draw, [(1870, 790), (1870, 800)])

    # Retrieval / evidence
    draw_lane(
        draw,
        (55, 900, 2145, 1240),
        "受控检索与证据链",
        fill="#FBF5FD",
        border=RETRIEVAL_BORDER,
    )
    draw_box(
        draw,
        (90, 970, 560, 1140),
        "检索规划目录",
        (
            "白名单两跳 → 属性 → 聚合",
            "多条件列表 → 关系模板",
            "→ Schema 约束 Text2Cypher",
        ),
        fill=RETRIEVAL_FILL,
        border=RETRIEVAL_BORDER,
    )
    draw_box(
        draw,
        (610, 990, 970, 1120),
        "Cypher Guard",
        ("单语句 · 只读 · Schema 白名单", "参数 · LIMIT · 禁止动态路径"),
        fill=RETRIEVAL_FILL,
        border=RETRIEVAL_BORDER,
    )
    draw_box(
        draw,
        (1020, 990, 1340, 1120),
        "EXPLAIN",
        ("语法与查询计划校验",),
        fill=RETRIEVAL_FILL,
        border=RETRIEVAL_BORDER,
    )
    draw_box(
        draw,
        (1390, 970, 1690, 1140),
        "Neo4jRepository",
        ("只读事务 · 超时", "结果上限 · 模板化查询"),
        fill=RETRIEVAL_FILL,
        border=RETRIEVAL_BORDER,
    )
    draw_box(
        draw,
        (1740, 970, 2100, 1140),
        "统一证据结构",
        (
            "Evidence · AttributeFact",
            "AggregateFact · PathEvidence",
            "来源 / 未提供出处",
        ),
        fill=EVIDENCE_FILL,
        border=EVIDENCE_BORDER,
    )
    draw_arrow(draw, [(650, 970), (650, 940), (510, 940), (510, 970)])
    draw_arrow(draw, [(560, 1055), (610, 1055)])
    draw_arrow(draw, [(970, 1055), (1020, 1055)])
    draw_arrow(draw, [(1340, 1055), (1390, 1055)])
    draw_arrow(draw, [(1690, 1055), (1740, 1055)])
    draw_arrow(draw, [(1920, 970), (1920, 870)])

    # External dependencies
    draw_lane(
        draw,
        (55, 1270, 2145, 1450),
        "外部依赖与本地资源",
        fill="#F4F6F8",
        border=EXTERNAL_BORDER,
    )
    draw_box(
        draw,
        (90, 1325, 610, 1415),
        "Neo4j",
        ("6 类实体 · 6 类关系 · 26,324 条关系",),
        fill=EXTERNAL_FILL,
        border=EXTERNAL_BORDER,
    )
    draw_box(
        draw,
        (660, 1325, 1120, 1415),
        "模型接口",
        ("意图 · 实体 · Cypher · 回答",),
        fill=EXTERNAL_FILL,
        border=EXTERNAL_BORDER,
    )
    draw_box(
        draw,
        (1170, 1325, 1700, 1415),
        "BGE-large + FAISS",
        ("11,043 实体 · Top-3 · 阈值召回",),
        fill=EXTERNAL_FILL,
        border=EXTERNAL_BORDER,
    )
    draw_box(
        draw,
        (1750, 1325, 2110, 1415),
        "安全边界",
        ("无写入 · 无自治执行 · 无动态多跳",),
        fill=EVIDENCE_FILL,
        border=EVIDENCE_BORDER,
    )
    draw_arrow(draw, [(1540, 1140), (1540, 1200), (350, 1200), (350, 1325)])
    draw_arrow(draw, [(1380, 870), (1380, 910), (890, 910), (890, 1325)])
    draw_arrow(draw, [(530, 870), (530, 910), (1435, 910), (1435, 1325)])
    draw_arrow(draw, [(1655, 505), (1655, 575)])

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT_PATH, format="PNG", optimize=True)
    return OUTPUT_PATH


def main() -> int:
    path = render()
    print(f"架构图已生成：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
