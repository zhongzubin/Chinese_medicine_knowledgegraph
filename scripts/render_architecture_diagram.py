# -*- coding: utf-8 -*-
"""渲染中医知识图谱问答系统的三泳道架构流程图。"""

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

WIDTH = 2400
HEIGHT = 1320

BACKGROUND = "#FFFFFF"
TEXT = "#30363D"
MUTED = "#6F7882"
ARROW = "#7D8996"

PROCESS_FILL = "#F8FBFF"
PROCESS_BORDER = "#9CBAD2"
DECISION_FILL = "#FFF9EC"
DECISION_BORDER = "#DDAA55"
DATABASE_FILL = "#DDF4DF"
DATABASE_BORDER = "#69AF78"
EXTERNAL_FILL = "#F7F8FA"
EXTERNAL_BORDER = "#AEB8C2"
NOTE_FILL = "#FFFDF8"
NOTE_BORDER = "#D8C9AA"

LEFT_PANEL_FILL = "#FFF8F1"
LEFT_PANEL_BORDER = "#F2D4B9"
LEFT_ACCENT = "#C77B2F"
MIDDLE_PANEL_FILL = "#F5FBF5"
MIDDLE_PANEL_BORDER = "#CBE5CE"
MIDDLE_ACCENT = "#4B9658"
RIGHT_PANEL_FILL = "#F8F5FF"
RIGHT_PANEL_BORDER = "#D8CEF3"
RIGHT_ACCENT = "#7D56BE"

Box = Tuple[int, int, int, int]
Point = Tuple[int, int]


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(str(path), size=size)


TITLE_FONT = font(31, bold=True)
SUBTITLE_FONT = font(16)
CARD_TITLE_FONT = font(18, bold=True)
CARD_BODY_FONT = font(15)
SMALL_FONT = font(14)
BADGE_FONT = font(14, bold=True)
DIAMOND_FONT = font(17, bold=True)
DATABASE_TITLE_FONT = font(20, bold=True)


def draw_centered(
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


def draw_card(
    draw: ImageDraw.ImageDraw,
    box: Box,
    title: str,
    lines: Sequence[str] = (),
    *,
    fill: str = PROCESS_FILL,
    border: str = PROCESS_BORDER,
    title_color: str = TEXT,
    radius: int = 11,
) -> None:
    left, top, right, bottom = box
    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=fill,
        outline=border,
        width=2,
    )
    if not lines:
        draw_centered(
            draw,
            (left + 8, top + 4, right - 8, bottom - 4),
            title,
            text_font=CARD_TITLE_FONT,
            fill=title_color,
        )
        return

    title_height = 34
    draw_centered(
        draw,
        (left + 8, top + 3, right - 8, top + title_height),
        title,
        text_font=CARD_TITLE_FONT,
        fill=title_color,
    )
    body_top = top + title_height + 1
    line_height = 20
    total_height = line_height * len(lines)
    y = body_top + ((bottom - body_top - total_height) / 2)
    for line in lines:
        draw_centered(
            draw,
            (left + 8, int(y), right - 8, int(y + line_height)),
            line,
            text_font=CARD_BODY_FONT,
            fill=MUTED,
        )
        y += line_height


def draw_panel(
    draw: ImageDraw.ImageDraw,
    box: Box,
    title: str,
    subtitle: str,
    *,
    fill: str,
    border: str,
    accent: str,
) -> None:
    draw.rounded_rectangle(
        box,
        radius=17,
        fill=fill,
        outline=border,
        width=2,
    )
    center_x = (box[0] + box[2]) / 2
    draw.text(
        (center_x, 45),
        title,
        fill=TEXT,
        font=TITLE_FONT,
        anchor="ma",
    )
    draw.text(
        (center_x, 84),
        subtitle,
        fill=MUTED,
        font=SUBTITLE_FONT,
        anchor="ma",
    )
    draw.rounded_rectangle(
        (box[0] + 20, box[1] + 16, box[0] + 25, box[1] + 21),
        radius=2,
        fill=accent,
        outline=accent,
    )


def draw_database(
    draw: ImageDraw.ImageDraw,
    box: Box,
    title: str,
    lines: Sequence[str] = (),
    *,
    fill: str = DATABASE_FILL,
    border: str = DATABASE_BORDER,
) -> None:
    left, top, right, bottom = box
    ellipse_height = 24
    radius = ellipse_height // 2
    draw.rectangle(
        (left, top + radius, right, bottom - radius),
        fill=fill,
    )
    draw.ellipse(
        (left, bottom - ellipse_height, right, bottom),
        fill=fill,
        outline=border,
        width=2,
    )
    draw.line(
        [(left, top + radius), (left, bottom - radius)],
        fill=border,
        width=2,
    )
    draw.line(
        [(right, top + radius), (right, bottom - radius)],
        fill=border,
        width=2,
    )
    draw.ellipse(
        (left, top, right, top + ellipse_height),
        fill=fill,
        outline=border,
        width=2,
    )
    draw_centered(
        draw,
        (left + 14, top + 26, right - 14, top + 58),
        title,
        text_font=DATABASE_TITLE_FONT,
    )
    y = top + 61
    for line in lines:
        draw_centered(
            draw,
            (left + 12, y, right - 12, y + 20),
            line,
            text_font=CARD_BODY_FONT,
            fill=MUTED,
        )
        y += 20


def draw_diamond(
    draw: ImageDraw.ImageDraw,
    center: Point,
    width: int,
    height: int,
    lines: Sequence[str],
    *,
    fill: str = DECISION_FILL,
    border: str = DECISION_BORDER,
) -> None:
    center_x, center_y = center
    half_width = width // 2
    half_height = height // 2
    points = [
        (center_x, center_y - half_height),
        (center_x + half_width, center_y),
        (center_x, center_y + half_height),
        (center_x - half_width, center_y),
    ]
    draw.polygon(points, fill=fill)
    draw.line([*points, points[0]], fill=border, width=2, joint="curve")
    line_height = 21
    total_height = line_height * len(lines)
    y = center_y - total_height / 2
    for line in lines:
        draw_centered(
            draw,
            (
                center_x - half_width + 16,
                int(y),
                center_x + half_width - 16,
                int(y + line_height),
            ),
            line,
            text_font=DIAMOND_FONT,
            fill=TEXT,
        )
        y += line_height


def _dashed_line(
    draw: ImageDraw.ImageDraw,
    points: Sequence[Point],
    *,
    fill: str,
    width: int,
    dash: int = 9,
    gap: int = 6,
) -> None:
    for start, end in zip(points, points[1:]):
        x1, y1 = start
        x2, y2 = end
        delta_x = x2 - x1
        delta_y = y2 - y1
        length = max(abs(delta_x), abs(delta_y))
        if length == 0:
            continue
        for offset in range(0, length, dash + gap):
            end_offset = min(offset + dash, length)
            start_ratio = offset / length
            end_ratio = end_offset / length
            draw.line(
                [
                    (
                        x1 + delta_x * start_ratio,
                        y1 + delta_y * start_ratio,
                    ),
                    (
                        x1 + delta_x * end_ratio,
                        y1 + delta_y * end_ratio,
                    ),
                ],
                fill=fill,
                width=width,
            )


def _arrowhead(
    draw: ImageDraw.ImageDraw,
    tip: Point,
    previous: Point,
    *,
    color: str,
    size: int = 10,
) -> None:
    tip_x, tip_y = tip
    previous_x, previous_y = previous
    if abs(tip_x - previous_x) >= abs(tip_y - previous_y):
        direction_x = 1 if tip_x > previous_x else -1
        direction_y = 0
    else:
        direction_x = 0
        direction_y = 1 if tip_y > previous_y else -1
    if direction_x:
        points = [
            (tip_x, tip_y),
            (tip_x - direction_x * size, tip_y - size * 0.62),
            (tip_x - direction_x * size, tip_y + size * 0.62),
        ]
    else:
        points = [
            (tip_x, tip_y),
            (tip_x - size * 0.62, tip_y - direction_y * size),
            (tip_x + size * 0.62, tip_y - direction_y * size),
        ]
    draw.polygon(points, fill=color)


def draw_arrow(
    draw: ImageDraw.ImageDraw,
    points: Sequence[Point],
    *,
    color: str = ARROW,
    width: int = 2,
    dashed: bool = False,
    both_ends: bool = False,
) -> None:
    if len(points) < 2:
        return
    if dashed:
        _dashed_line(draw, points, fill=color, width=width)
    else:
        draw.line(points, fill=color, width=width, joint="curve")
    _arrowhead(
        draw,
        points[-1],
        points[-2],
        color=color,
    )
    if both_ends:
        _arrowhead(
            draw,
            points[0],
            points[1],
            color=color,
        )


def draw_label(
    draw: ImageDraw.ImageDraw,
    position: Point,
    text: str,
    *,
    fill: str = MUTED,
    anchor: str = "mm",
) -> None:
    bounds = draw.textbbox(position, text, font=SMALL_FONT, anchor=anchor)
    padding_x = 5
    padding_y = 2
    draw.rounded_rectangle(
        (
            bounds[0] - padding_x,
            bounds[1] - padding_y,
            bounds[2] + padding_x,
            bounds[3] + padding_y,
        ),
        radius=5,
        fill=BACKGROUND,
    )
    draw.text(
        position,
        text,
        fill=fill,
        font=SMALL_FONT,
        anchor=anchor,
    )


def draw_badge(
    draw: ImageDraw.ImageDraw,
    position: Point,
    text: str,
    *,
    fill: str,
    border: str,
    color: str,
) -> None:
    x, y = position
    bounds = draw.textbbox((0, 0), text, font=BADGE_FONT)
    width = bounds[2] - bounds[0] + 24
    draw.rounded_rectangle(
        (x, y, x + width, y + 28),
        radius=14,
        fill=fill,
        outline=border,
        width=1,
    )
    draw_centered(
        draw,
        (x + 9, y + 3, x + width - 9, y + 25),
        text,
        text_font=BADGE_FONT,
        fill=color,
    )


def render() -> Path:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    draw.text(
        (WIDTH / 2, 12),
        "中医知识图谱问答系统 · 当前实现架构",
        fill=TEXT,
        font=font(21, bold=True),
        anchor="ma",
    )

    left_panel = (35, 120, 650, 1240)
    middle_panel = (685, 120, 1660, 1240)
    right_panel = (1695, 120, 2365, 1240)
    draw_panel(
        draw,
        left_panel,
        "用户接入与会话",
        "入口 · 安全控制 · 5 轮上下文",
        fill=LEFT_PANEL_FILL,
        border=LEFT_PANEL_BORDER,
        accent=LEFT_ACCENT,
    )
    draw_panel(
        draw,
        middle_panel,
        "在线问答与证据闭环",
        "固定 17 节点 LangGraph · 受控检索 Agent",
        fill=MIDDLE_PANEL_FILL,
        border=MIDDLE_PANEL_BORDER,
        accent=MIDDLE_ACCENT,
    )
    draw_panel(
        draw,
        right_panel,
        "离线知识库与图谱支撑",
        "采集 · 抽取 · 入库 · Schema · 向量索引",
        fill=RIGHT_PANEL_FILL,
        border=RIGHT_PANEL_BORDER,
        accent=RIGHT_ACCENT,
    )

    # 左泳道：入口、安全、会话和输出。
    draw_card(
        draw,
        (72, 200, 285, 270),
        "Streamlit 前端",
        ("双栏问答 · SSE", "证据与审核展示"),
    )
    draw_card(
        draw,
        (72, 300, 285, 370),
        "CLI",
        ("本地命令 · JSON 输出",),
    )
    draw_card(
        draw,
        (72, 400, 285, 470),
        "API 客户端",
        ("POST /qa · SSE 流式消费",),
    )
    draw_card(
        draw,
        (350, 220, 612, 316),
        "FastAPI",
        ("统一问答入口 · 健康检查", "会话删除 · SSE 事件流"),
    )
    draw_card(
        draw,
        (350, 365, 612, 460),
        "安全与并发控制",
        ("API Key → client_id", "限流 · 并发租约 · 错误映射"),
    )
    draw_database(
        draw,
        (350, 545, 612, 745),
        "Redis",
        (
            "最近 5 轮会话 · TTL",
            "会话锁 · 限流窗口",
            "不保存提示词或堆栈",
        ),
    )
    draw_card(
        draw,
        (72, 855, 612, 975),
        "客户端展示",
        (
            "答案 · 图谱证据 · 属性事实 / 统计 / 路径",
            "回答审核 · warnings · grounded 状态",
        ),
        fill="#FCFDFE",
    )
    draw_card(
        draw,
        (72, 1035, 612, 1155),
        "安全边界",
        (
            "Neo4j 始终只读；用户输入只进入查询参数",
            "只公开处理阶段与证据，不暴露隐藏思维链",
        ),
        fill=NOTE_FILL,
        border=NOTE_BORDER,
    )

    draw_arrow(draw, [(285, 235), (350, 235)])
    draw_arrow(draw, [(285, 335), (318, 335), (318, 255), (350, 255)])
    draw_arrow(draw, [(285, 435), (325, 435), (325, 292), (350, 292)])
    draw_arrow(draw, [(481, 316), (481, 365)])
    draw_arrow(draw, [(445, 460), (445, 545)], both_ends=True, color="#6A9B75")
    draw_label(draw, (470, 503), "会话 / 配额")

    # 中泳道：在线工作流。
    draw_card(
        draw,
        (720, 195, 890, 270),
        "规范化问题",
        ("NFKC · 空白清理", "格式与只读安全初筛"),
    )
    draw_card(
        draw,
        (930, 195, 1115, 270),
        "历史改写（LLM）",
        ("指代消解 · 上下文补全", "失败时确定性降级"),
    )
    draw_card(
        draw,
        (1155, 195, 1325, 270),
        "实体预匹配",
        ("名称 / 别名 Exact",),
    )
    draw_diamond(
        draw,
        (1468, 233),
        176,
        108,
        ("是否属于", "中医图谱问题？"),
    )
    draw_arrow(draw, [(890, 233), (930, 233)])
    draw_arrow(draw, [(1115, 233), (1155, 233)])
    draw_arrow(draw, [(1325, 233), (1380, 233)])

    draw_card(
        draw,
        (1040, 330, 1325, 425),
        "实体抽取 + 混合链接",
        ("LLM 候选 · Exact · Fuzzy", "BGE-large + FAISS Top-3"),
    )
    draw_card(
        draw,
        (1450, 330, 1615, 410),
        "直接回答",
        ("grounded=false · SSE", "标记未使用图谱"),
        fill=EXTERNAL_FILL,
        border=EXTERNAL_BORDER,
    )
    draw_arrow(
        draw,
        [(1468, 287), (1468, 308), (1325, 308), (1325, 350)],
    )
    draw_label(draw, (1405, 302), "是")
    draw_arrow(
        draw,
        [(1556, 233), (1635, 233), (1635, 330)],
    )
    draw_label(draw, (1615, 272), "否")

    draw_diamond(
        draw,
        (1180, 515),
        176,
        100,
        ("是否链接到", "具体图谱实体？"),
    )
    draw_arrow(draw, [(1180, 425), (1180, 465)])
    draw_card(
        draw,
        (1400, 470, 1615, 555),
        "请求补充具体实体",
        ("药材 / 方剂 / 疾病 / 症状", "不生成无依据医疗事实"),
        fill=NOTE_FILL,
        border=NOTE_BORDER,
    )
    draw_arrow(draw, [(1268, 515), (1400, 515)])
    draw_label(draw, (1342, 505), "否")

    draw_card(
        draw,
        (705, 455, 1005, 595),
        "RetrievalAgent 受控规划",
        (
            "白名单两跳 · 属性 · 聚合",
            "多条件列表 · 关系模板",
            "Schema 约束 Text2Cypher",
            "最多 3 次 plan → observe → replan",
        ),
        title_color="#3F7D4A",
    )
    draw_arrow(draw, [(1092, 515), (1005, 515)])
    draw_label(draw, (1042, 505), "是")

    draw_card(
        draw,
        (705, 655, 930, 740),
        "Cypher Guard + EXPLAIN",
        ("单语句 · 只读 · 参数", "白名单 · LIMIT · 禁止动态路径"),
    )
    draw_card(
        draw,
        (970, 655, 1200, 740),
        "Neo4j 只读执行",
        ("8s 超时 · 最多 50 行", "结果转 JSON 证据"),
    )
    draw_diamond(
        draw,
        (1370, 760),
        220,
        110,
        ("是否找到", "可验证图谱证据？"),
    )
    draw_arrow(draw, [(855, 595), (855, 655)])
    draw_arrow(draw, [(930, 697), (970, 697)])
    draw_arrow(
        draw,
        [(1200, 697), (1232, 697), (1232, 760), (1260, 760)],
    )
    draw_label(draw, (1217, 681), "结果")
    draw_arrow(
        draw,
        [(818, 740), (818, 808), (1490, 808), (1490, 760), (1480, 760)],
        color="#D19442",
        dashed=True,
    )
    draw_label(draw, (1110, 797), "校验失败 → 观察", fill="#9A6B26")

    draw_card(
        draw,
        (1240, 850, 1438, 940),
        "证据化回答（LLM）",
        ("只使用本轮图谱结果", "关系 / 属性 / 统计 / 路径"),
        title_color="#3F7D4A",
    )
    draw_card(
        draw,
        (1480, 850, 1630, 940),
        "确定性证据审核",
        ("逐项覆盖 · 引用", "缺失时证据化回退"),
    )
    draw_card(
        draw,
        (1480, 990, 1630, 1060),
        "SSE 流式返回",
        ("stage · evidence · audit",),
        fill="#FCFDFE",
    )
    draw_arrow(draw, [(1370, 815), (1370, 850)])
    draw_label(draw, (1350, 829), "是")
    draw_arrow(draw, [(1438, 895), (1480, 895)])
    draw_arrow(draw, [(1555, 940), (1555, 990)])

    draw_card(
        draw,
        (1005, 850, 1195, 940),
        "无依据 / 澄清 / 安全失败",
        ("明确说明当前图谱没有依据", "未确认实体时不查询图谱"),
        fill=NOTE_FILL,
        border=NOTE_BORDER,
    )
    draw_arrow(
        draw,
        [(1315, 815), (1315, 828), (1100, 828), (1100, 850)],
    )
    draw_label(draw, (1278, 821), "否")

    draw_arrow(
        draw,
        [(1260, 760), (1222, 760), (1222, 625), (1018, 625), (1018, 595)],
        color="#D19442",
        dashed=True,
    )
    draw_label(draw, (1115, 615), "重规划（≤3 次）", fill="#9A6B26")

    # 中泳道回传结果到左泳道。
    draw_arrow(
        draw,
        [(1480, 1025), (655, 1025), (655, 920), (612, 920)],
    )
    draw_label(draw, (1040, 1013), "答案 + 证据 + 审核")
    draw_arrow(
        draw,
        [(1005, 895), (648, 895), (648, 945), (612, 945)],
    )
    draw_label(draw, (815, 884), "无依据响应")

    # 右泳道：离线知识库。
    draw_card(
        draw,
        (1740, 180, 1990, 250),
        "中医百科数据源",
        ("中药材目录 · 方剂目录",),
    )
    draw_card(
        draw,
        (1740, 290, 1990, 370),
        "合规爬虫",
        ("robots.txt · 随机限流", "指数退避 · 断点续爬"),
    )
    draw_card(
        draw,
        (1740, 400, 1915, 470),
        "Excel 原始数据",
        ("中药 / 方剂",),
    )
    draw_card(
        draw,
        (1960, 400, 2310, 490),
        "规则 + LLM 知识抽取",
        ("属性抽取 · 组成 / 出处 / 功效", "LLM 识别疾病与症状"),
    )
    draw_card(
        draw,
        (1740, 520, 1915, 590),
        "实体 / 关系结果",
        ("JSON / JSONL",),
    )
    draw_card(
        draw,
        (1960, 520, 2310, 610),
        "Neo4j 批量导入",
        ("格式对齐 · 实体端点补全", "MERGE · 唯一约束 · 幂等"),
    )
    draw_database(
        draw,
        (1740, 670, 1990, 850),
        "Neo4j",
        (
            "6 类实体 · 6 类关系",
            "11,043 节点",
            "26,324 条关系",
        ),
    )
    draw_card(
        draw,
        (2040, 670, 2310, 750),
        "元数据导出",
        ("JSON + Markdown", "中文标签 / 属性 / 模式"),
    )
    draw_card(
        draw,
        (2040, 820, 2310, 910),
        "GraphSchema 白名单",
        ("实体标签 · 关系类型", "属性 · 三元组方向"),
        title_color="#7150AE",
    )
    draw_card(
        draw,
        (1740, 900, 1990, 990),
        "BGE-large + FAISS",
        ("名称 + 别名向量化", "Top-3 · 阈值召回 · 缓存"),
    )
    draw_card(
        draw,
        (2040, 960, 2310, 1050),
        "OpenAI 兼容模型",
        ("在线：改写 / 抽取 / 规划 / 回答", "离线：疾病与症状识别"),
        fill=EXTERNAL_FILL,
        border=EXTERNAL_BORDER,
    )
    draw_card(
        draw,
        (1740, 1090, 2310, 1165),
        "当前导入说明",
        (
            "import_data 默认读取 crawler/data/extract_*_data.json；",
            "抽取脚本默认输出 output/*.jsonl，导入前需确认并对齐格式。",
        ),
        fill=NOTE_FILL,
        border=NOTE_BORDER,
        title_color="#8A6935",
    )

    draw_arrow(draw, [(1865, 250), (1865, 290)])
    draw_arrow(draw, [(1865, 370), (1828, 370), (1828, 400)])
    draw_arrow(draw, [(1915, 435), (1960, 445)])
    draw_arrow(
        draw,
        [(2135, 490), (2135, 555), (1915, 555)],
    )
    draw_arrow(draw, [(1915, 555), (1960, 555)])
    draw_arrow(
        draw,
        [(2135, 610), (2135, 645), (1865, 645), (1865, 670)],
    )
    draw_arrow(draw, [(1990, 710), (2040, 710)])
    draw_arrow(draw, [(2175, 750), (2175, 820)])
    draw_arrow(draw, [(1865, 850), (1865, 900)])
    draw_arrow(
        draw,
        [(2290, 960), (2337, 960), (2337, 445), (2310, 445)],
        dashed=True,
        color="#8E7AAE",
    )
    draw_label(draw, (2327, 565), "离线抽取调用", anchor="rm")

    # 跨泳道依赖：数据、Schema 与向量召回。
    draw_arrow(
        draw,
        [(612, 270), (655, 270), (655, 233), (720, 233)],
    )
    draw_label(draw, (657, 288), "用户问题", anchor="lm")

    draw_arrow(
        draw,
        [(612, 650), (665, 650), (665, 165), (1022, 165), (1022, 195)],
        dashed=True,
        color="#C28A48",
    )
    draw_label(draw, (833, 158), "最近 5 轮历史上下文", fill="#946D39")

    draw_arrow(
        draw,
        [(1740, 950), (1668, 950), (1668, 440), (1325, 440), (1325, 425)],
        color="#6E82A0",
    )
    draw_label(draw, (1610, 430), "Top-3 语义召回", anchor="rm")

    draw_arrow(
        draw,
        [(2040, 865), (1675, 865), (1675, 632), (1005, 632), (1005, 595)],
        color="#8A6BC1",
    )
    draw_label(draw, (1688, 805), "Schema 约束", anchor="rm")

    draw_arrow(
        draw,
        [(1200, 695), (1668, 695), (1740, 695)],
        both_ends=True,
        color="#5D9E6B",
    )
    draw_label(draw, (1470, 685), "只读查询 / 结果")

    draw_arrow(
        draw,
        [(2040, 1005), (1668, 1005)],
        dashed=True,
        color="#8E7AAE",
    )
    draw_label(draw, (1688, 982), "多阶段模型调用", anchor="rm")

    draw_badge(
        draw,
        (2115, 138),
        "固定拓扑 MVP",
        fill="#F1ECFC",
        border=RIGHT_PANEL_BORDER,
        color=RIGHT_ACCENT,
    )
    draw_badge(
        draw,
        (514, 138),
        "单 worker 演示",
        fill="#FFF4E7",
        border=LEFT_PANEL_BORDER,
        color=LEFT_ACCENT,
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT_PATH, format="PNG", optimize=True)
    return OUTPUT_PATH


def main() -> int:
    path = render()
    print(f"架构图已生成：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
