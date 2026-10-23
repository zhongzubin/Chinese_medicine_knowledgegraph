# -*- coding: utf-8 -*-
"""爬取中医百科「中医方剂」下全部方剂，并导出为 Excel 表格。

数据来源: https://zhongyibaike.com/wiki/中医方剂
输出文件: crawler/方剂大全.xlsx

用法:
    python crawler/fangji_spider.py                    # 全量爬取（默认断点续爬）
    python crawler/fangji_spider.py --limit 8          # 只抓前 8 首，用于试跑
    python crawler/fangji_spider.py --delay-min 2 --delay-max 3.5
    python crawler/fangji_spider.py --refresh          # 忽略本地缓存，重新抓取

爬取规范:
    1. 单线程串行抓取，请求间隔默认随机 1~2 秒，不对站点造成压力；
    2. 请求前检查 robots.txt（该站仅禁止 /admin 路径）；
    3. 失败按指数退避重试，仍失败则跳过并记录，不暴力重试；
    4. 每抓完一首方剂立即写入缓存，中断后再次运行可续爬，不重复请求已抓过的页面。

代码结构（建议按此顺序阅读）:
    1. 请求与合规    build_session / fetch / load_robots
    2. 文本提取      normalize_text / iter_blocks / collect_lines / composition_text
    3. 页面解析      parse_catalog（目录页）/ parse_detail（详情页状态机）
    4. 断点续爬      load_cache / append_cache
    5. 导出表格      export_excel / write_failed
    6. 主流程        parse_args / main
"""

import argparse
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import pandas as pd
import requests
from bs4 import BeautifulSoup
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

BASE_DIR = Path(__file__).resolve().parent
BASE_URL = "https://zhongyibaike.com"
# 目录页：一页列出全部方剂，只需请求一次
INDEX_URL = f"{BASE_URL}/wiki/中医方剂"
ROBOTS_URL = f"{BASE_URL}/robots.txt"

# 使用自定义 UA 表明身份，方便站点管理员识别；该站对普通 UA 也可访问
USER_AGENT = (
    "ChineseMedicineKnowledgeGraphBot/1.0 "
    "(+https://github.com/zhongzubin/Chinese_medicine_knowledgegraph)"
)

# 输出表格、断点续爬缓存、失败记录都放在 crawler 目录下
DEFAULT_OUTPUT = BASE_DIR / "data/方剂大全.xlsx"
DEFAULT_CACHE = BASE_DIR / ".cache" / "fangji_daquan.jsonl"
DEFAULT_FAILED_LOG = BASE_DIR / "data/方剂大全_失败记录.txt"

# 表格列: (列名, 列宽)
# 名称/大类/子类来自目录页，其余列来自详情页；列宽只是 Excel 显示宽度
COLUMNS = [
    ("序号", 6),
    ("方剂名称", 16),
    ("大类", 10),
    ("子类", 14),
    ("出处", 24),
    ("分类", 20),
    ("组成", 42),
    ("解释", 42),
    ("功用", 30),
    ("主治", 46),
    ("用法", 42),
    ("注意", 30),
    ("别名", 20),
    ("其他信息", 40),
    ("图片链接", 32),
    ("详情页链接", 28),
    ("爬取时间", 20),
]

OTHER_COL = "其他信息"

# 详情页小标题 / 正文中的【…】标记 -> 表格列
# 按顺序匹配，先命中的关键词生效：
# 「功能与主治」先命中「功能」归入功用，「用法与用量」先命中「用法」
FIELD_KEYWORDS = (
    ("出处", "出处"),
    ("分类", "分类"),
    ("组成", "组成"),
    ("处方", "组成"),
    ("解释", "解释"),
    ("方解", "解释"),
    ("功用", "功用"),
    ("功效", "功用"),
    ("功能", "功用"),
    ("辨证", "主治"),
    ("主治", "主治"),
    ("用法", "用法"),
    ("制法", "用法"),
    ("煎服", "用法"),
    ("禁忌", "注意"),
    ("注意", "注意"),
    ("宜忌", "注意"),
    ("异名", "别名"),
    ("别名", "别名"),
)

# 正文里常见的独立小标题，如「【禁忌】」「【各家论述】」
MARKER_RE = re.compile(r"^【([^】]{1,20})】(.*)$")

# 解析时只把这些标签当作“内容块”，其余行内标签只取文字
BLOCK_TAGS = {"h2", "h3", "h4", "p", "ul", "ol", "table", "blockquote", "dl", "pre"}
# 页面用 div/section 层层包裹正文，遇到容器标签就继续向下找内容块
CONTAINER_TAGS = {"div", "section", "article", "header", "main"}
# 行内标签会被 unwrap 成纯文本；唯独 <a> 保留，供 composition_text 切分药名
INLINE_TAGS = ("a", "strong", "b", "em", "i", "span", "font", "small", "sub", "sup")

LATIN_RE = re.compile(r"[A-Za-z]{2,}")
EXCEL_CELL_LIMIT = 32000


# ============ 请求 ============

def build_session():
    """构造带重试策略的会话，失败时指数退避，避免高频重试。"""
    session = requests.Session()
    # total=3：最多自动重试 3 次
    # backoff_factor=3：依次等待约 3s、6s、12s，越失败等越久
    # status_forcelist：只对限流/服务端错误重试，404 等客户端错误直接抛出
    retry = Retry(
        total=3,
        backoff_factor=3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    # 声明身份和可接受的内容类型，不伪装成浏览器
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return session


def fetch(session, url, timeout):
    """请求单个页面并返回 HTML 文本。"""
    response = session.get(url, timeout=timeout)
    # 4xx/5xx 直接抛异常，交给主循环记录失败
    response.raise_for_status()
    # 站点未声明编码时，用 apparent_encoding 推测，避免中文乱码
    if not response.encoding:
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def load_robots(session, timeout=20):
    """读取并解析 robots.txt。"""
    parser = RobotFileParser()
    try:
        response = session.get(ROBOTS_URL, timeout=timeout)
        response.raise_for_status()
        parser.parse(response.text.splitlines())
    except requests.RequestException as exc:
        # 拿不到 robots.txt 时不阻断任务，只在终端提醒
        print(f"[警告] robots.txt 获取失败（{exc}），按允许抓取处理")
    return parser


# ============ 文本工具 ============

def normalize_text(text):
    """压缩空白、去掉控制字符，中文之间的空格一并去掉。"""
    # 全角空格、零宽字符先换成普通空格
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    # 去掉不可见控制字符，避免写进 Excel 后显示异常
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    # 连续空白合并成一个空格
    text = re.sub(r"\s+", " ", text).strip()
    # 中文与中文之间的空格是排版造成的，直接去掉；英文/数字之间的空格保留
    text = re.sub(r"(?<=[\u4e00-\u9fff、，。；：（）「」“”‘’])\s+(?=[\u4e00-\u9fff、，。；：（）「」“”‘’])", "", text)
    return text


def clip(text, limit=EXCEL_CELL_LIMIT):
    """Excel 单元格最多 32767 字符，超长内容截断。"""
    # 预留一点余量（32000），避免正好卡在 Excel 上限报错
    if len(text) <= limit:
        return text
    return text[:limit] + "……（内容过长已截断）"


def iter_blocks(node):
    """按文档顺序产出顶层内容块，跳过脚本、广告等噪音。"""
    for child in node.children:
        name = getattr(child, "name", None)
        # 纯文本节点、脚本/样式、广告 ins 全部跳过
        if name is None or name in ("script", "style", "ins", "noscript"):
            continue
        if name in BLOCK_TAGS:
            yield child
        elif name in CONTAINER_TAGS:
            # div 里面可能还套 div，递归拆到底，保证标题和正文按原顺序出现
            yield from iter_blocks(child)


def collect_lines(node):
    """提取块内文本：列表项各占一行，行内标签直接拼接。"""
    lines = []
    buffer = []  # 暂存同一行内的文字碎片（文本节点 + 行内标签）

    def flush():
        """把缓冲区拼成一行，写入 lines 后清空。"""
        if buffer:
            text = normalize_text("".join(buffer))
            if text:
                lines.append(text)
            buffer.clear()

    def walk(current):
        for child in current.children:
            name = getattr(child, "name", None)
            if name is None:
                buffer.append(str(child))
            elif name == "br":
                # <br> 表示换行，先收尾当前行
                flush()
            elif name in ("ul", "ol"):
                # 列表：每个 <li> 单独成行（如组成、附方清单）
                flush()
                for li in child.find_all("li", recursive=False):
                    lines.extend(collect_lines(li))
            elif name in ("p", "div", "table", "tr"):
                # 嵌套的块级元素：先收尾当前行，再递归提取内部文本
                flush()
                lines.extend(collect_lines(child))
                flush()
            else:
                # 行内标签（a/strong/span 等）不换行，直接向下取文字
                walk(child)

    walk(node)
    flush()
    return lines


def composition_text(block):
    """把「药名 + 用量」按顿号拼接，便于阅读和后续结构化。

    组成段落里每味药通常是 <a>药名</a> 紧跟用量，例如
    <a>葛根</a>12克<a>麻黄</a>9克，用 <a> 作为切分点，
    就能得到「葛根12克」「麻黄9克」这样的完整条目。
    """
    segments = []
    current = []

    def flush():
        """收尾当前条目，去掉尾部的顿号、逗号等残留标点。"""
        if current:
            text = normalize_text("".join(current)).strip("、,，;； ")
            if text:
                segments.append(text)
            current.clear()

    for child in block.children:
        name = getattr(child, "name", None)
        if name == "a":
            # 遇到下一个药名，先把上一味药的「药名+用量」收尾
            flush()
            current.append(normalize_text(child.get_text()))
        elif name == "br":
            flush()
        elif name is None:
            # 药名后面的用量文字，接着上一段写
            current.append(str(child))
        elif name in ("ul", "ol"):
            flush()
            for li in child.find_all("li", recursive=False):
                current.extend(collect_lines(li))
                flush()
        else:
            # 其他嵌套标签（如 strong）直接取文字，保持不换行
            current.append(" " + child.get_text(" "))

    flush()
    return "、".join(segments)


# ============ 页面解析 ============

def parse_catalog(html):
    """解析方剂目录页，返回 [{name, url, category, sub_category}, ...]（已去重）。"""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find("section", id="content") or soup
    formulas = []
    seen = set()
    category = ""

    # 目录结构：h2 是大类（如「解表剂」），其后紧跟 ul/ol；
    # 每个 li 是一个子类（如「辛温解表」），li 里的 strong 是子类名，a 是方剂链接
    for node in iter_blocks(content):
        if node.name == "h2":
            category = normalize_text("".join(node.stripped_strings))
            continue
        if node.name not in ("ul", "ol"):
            continue

        for li in node.find_all("li", recursive=False):
            strong = li.find("strong")
            # 部分大类没有 strong，直接用大类名当子类
            sub_category = normalize_text(strong.get_text()) if strong else category
            for link in li.find_all("a", href=True):
                href = link["href"].split("#")[0].strip()
                # 只收站内 /wiki/ 链接，过滤外链和锚点
                if not href.startswith("/wiki/"):
                    continue
                url = urljoin(BASE_URL, href)
                name = normalize_text(link.get_text())
                # 同一方剂可能在多个子类下重复出现，用 URL 去重
                if not name or url in seen:
                    continue
                seen.add(url)
                formulas.append({
                    "name": name,
                    "url": url,
                    "category": category,
                    "sub_category": sub_category,
                })
    return formulas


def classify_section(heading):
    """把详情页大标题映射到表格列，无法识别时返回 None。"""
    # 详情页大标题固定为「X的药方」「X的功效」两种，
    # 药方块里包含出处/分类/组成/解释，功效块里包含功用/主治/用法/注意
    if "药方" in heading:
        return "组成"
    if "功效" in heading or "效果" in heading:
        return "功用"
    return None


def classify_field(heading):
    """把详情页小标题或【…】标记映射到表格列。"""
    # 关键词表是有序的，先到先得（详见 FIELD_KEYWORDS 的注释）
    for keyword, column in FIELD_KEYWORDS:
        if keyword in heading:
            return column
    return None


def split_marker(line):
    """拆出「【禁忌】」这类行内标记，返回 (标记, 剩余正文)。

    站点经常把多个小节写在一个段落里，例如：
    「【宜忌】…」「【各家论述】…」，拆开后才能归到不同列。
    """
    matched = MARKER_RE.match(line)
    if not matched:
        return None, line
    return matched.group(1), matched.group(2).strip()


def dedupe_aliases(text):
    """按顿号等分隔符拆分别名并去重，保留原有顺序。"""
    ordered = []
    positions = {}
    for item in re.split(r"[、,，;；]", text or ""):
        item = item.strip().strip("。")
        if not item:
            continue
        # 去掉括号内的出处后再比较，避免「火剂汤」与「火剂汤(某书)」重复；
        # 同一别名保留信息更完整的那个（通常带出处）
        key = re.sub(r"[（(][^）)]*[）)]", "", item).strip()
        if not key:
            continue
        if key not in positions:
            positions[key] = len(ordered)
            ordered.append(item)
        elif len(item) > len(ordered[positions[key]]):
            ordered[positions[key]] = item
    return "、".join(ordered)


def parse_name_aliases(lines):
    """从「名称」区块里整理别名。"""
    # 名称块里可能夹着英文名/拼音，先过滤掉纯拉丁字母的行
    candidates = [line for line in lines if line and not (LATIN_RE.search(line) and not re.search(r"[\u4e00-\u9fff]", line))]
    # 优先取含顿号的那一行（一条里写了多个别名）；
    # 否则把多行短词拼起来，排除带句号的长描述
    aliases = ""
    for line in candidates:
        if "、" in line:
            aliases = line
            break
    if not aliases:
        short_items = [item for item in candidates if len(item) <= 15 and not item.endswith("。")]
        pool = short_items if len(short_items) > 1 else candidates
        aliases = "、".join(pool)
    return dedupe_aliases(aliases)


def parse_detail(html):
    """解析单首方剂详情页，返回名称、别名、分栏正文与图片链接。"""
    soup = BeautifulSoup(html, "html.parser")

    # 详情页的 h1 就是方剂名
    title_tag = soup.find("h1")
    name = normalize_text(title_tag.get_text()) if title_tag else ""

    # 只解析正文区域，避开侧边栏、推荐位等干扰内容
    content = soup.find("section", id="content")
    if content is None:
        raise ValueError("页面缺少 section#content，可能是无效页面")

    # 先收集图片地址，再清理噪音节点（图片通常只在正文开头出现）
    images = []
    for img in content.find_all("img"):
        src = (img.get("src") or "").strip()
        if src:
            url = urljoin(BASE_URL, src)
            if url not in images:
                images.append(url)

    # 删除脚本、样式、广告、图片容器等不需要的节点
    for tag in content.find_all(["script", "style", "ins", "noscript", "figure"]):
        tag.decompose()
    # 把 strong/span 等行内标签拆成纯文本；
    # 但 <a> 必须保留，因为「组成」列要靠 <a> 切分药名
    for tag in content.find_all([name for name in INLINE_TAGS if name != "a"]):
        tag.unwrap()

    sections = {}       # 列名 -> 段落列表
    name_lines = []     # 「名称」区块的原始行（用来提取别名）
    # 三个状态变量共同决定一段文字写进哪一列：
    #   top_column     —— 最近一个 h2 对应的列，如「X的药方」→ 组成
    #   current_column —— 最近一个 h3 或【…】标记对应的列，优先级最高
    #   prefix         —— 未识别标题的原文，拼在正文前，避免信息丢失
    top_column = None
    current_column = None
    prefix = ""
    # 部分页面的首个区块是「名称」（存放别名），个别页面缺少这个小标题
    in_name_block = True

    for block in iter_blocks(content):
        # ---------- 情况一：遇到标题，切换当前列 ----------
        if block.name in ("h2", "h3", "h4"):
            heading = normalize_text(" ".join(block.stripped_strings))
            if block.name == "h2":
                in_name_block = heading == "名称"
                if in_name_block:
                    top_column = OTHER_COL
                    prefix = ""
                else:
                    # 先看大标题（药方/功效），再看小标题关键词
                    mapped = classify_section(heading) or classify_field(heading)
                    top_column = mapped or OTHER_COL
                    # 没认出来的大标题（如「Notes for X」）当正文前缀保留
                    prefix = "" if mapped else heading
                current_column = top_column
            else:
                mapped = classify_field(heading)
                # 小标题优先；认不出来就沿用当前 h2 的列
                current_column = mapped or top_column or OTHER_COL
                prefix = "" if mapped else heading
            continue

        # ---------- 情况二：普通正文块 ----------
        lines = collect_lines(block)
        if not lines:
            continue
        if in_name_block:
            name_lines.extend(lines)
            continue

        # 「组成」特殊处理：保留 <a> 切分药名，得到「药名+用量」条目
        if (current_column or OTHER_COL) == "组成" and block.find("a"):
            text = composition_text(block)
            if text:
                sections.setdefault("组成", []).append(text)
            continue

        # 逐行处理，因为同一段里可能夹着多个【…】小标题
        for line in lines:
            marker, rest = split_marker(line)
            if marker:
                mapped = classify_field(marker)
                if mapped:
                    # 标记能识别（如【禁忌】【异名】），切换列
                    current_column = mapped
                    prefix = ""
                else:
                    # 标记认不出来（如【各家论述】），连同标记名一起放进其他信息
                    current_column = OTHER_COL
                    prefix = marker
                line = rest
                if not line:
                    continue

            column = current_column or OTHER_COL
            # prefix 只用一次：给该小节的第一行加标题，后文不再重复
            text = f"{prefix}：{line}" if prefix else line
            prefix = ""
            sections.setdefault(column, []).append(text)

    # 「名称」块里的别名与正文【异名】里的别名合并去重
    name_aliases = parse_name_aliases(name_lines)
    aliases = dedupe_aliases("、".join([name_aliases] + sections.pop("别名", [])))
    return {
        "name": name,
        "aliases": aliases,
        # 把每列的多段正文用换行拼成一个大字符串，写进一个 Excel 单元格
        "sections": {key: "\n".join(value) for key, value in sections.items()},
        "images": images,
    }


# ============ 缓存 ============

def load_cache(path):
    """按详情页 URL 载入已抓取记录。"""
    cache = {}
    if not path.exists():
        return cache
    # JSONL 格式：一行一条 JSON，边读边建索引，避免一次性载入大文件
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # 上一轮异常退出可能留下半行，跳过即可
                continue
            url = record.get("url")
            if url:
                # 重复 URL 以最后一条为准，保证数据是最新一次抓取的
                cache[url] = record
    return cache


def append_cache(path, record):
    """每抓完一首方剂立即追加写入，保证中断后可以续爬。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
        # 立即刷盘，即使进程被强制结束也不丢已抓到的数据
        file.flush()


# ============ 导出 ============

def export_excel(records, output):
    """把抓取结果写入带格式的 Excel。"""
    # 第 1 步：把 JSON 记录拍平成「一行一首方剂」的字典
    rows = []
    for index, record in enumerate(records, start=1):
        sections = record.get("sections") or {}
        rows.append({
            "序号": index,
            "方剂名称": record.get("name", ""),
            "大类": record.get("category", ""),
            "子类": record.get("sub_category", ""),
            "出处": sections.get("出处", ""),
            "分类": sections.get("分类", ""),
            "组成": sections.get("组成", ""),
            "解释": sections.get("解释", ""),
            "功用": sections.get("功用", ""),
            "主治": sections.get("主治", ""),
            "用法": sections.get("用法", ""),
            "注意": sections.get("注意", ""),
            "别名": record.get("aliases", ""),
            OTHER_COL: sections.get(OTHER_COL, ""),
            "图片链接": "\n".join(record.get("images") or []),
            "详情页链接": record.get("url", ""),
            "爬取时间": record.get("crawled_at", ""),
        })

    columns = [name for name, _ in COLUMNS]
    # 第 2 步：转 DataFrame，空值写空字符串，再统一截断超长单元格
    dataframe = pd.DataFrame(rows, columns=columns).fillna("").astype(str)
    dataframe = dataframe.map(clip)

    output.parent.mkdir(parents=True, exist_ok=True)
    dataframe.to_excel(output, index=False, sheet_name="方剂大全", engine="openpyxl")

    # 第 3 步：重新打开 Excel，加表头样式、边框、冻结窗格和筛选器
    workbook = load_workbook(output)
    sheet = workbook["方剂大全"]

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2F7D5A")
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for index, (title, width) in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
        cell = sheet.cell(row=1, column=index)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    sheet.row_dimensions[1].height = 26
    # 冻结「序号 + 方剂名称」两列，滚动时始终能看到方剂名
    sheet.freeze_panes = "C2"
    # 表头自动筛选，方便按大类/子类查看
    sheet.auto_filter.ref = sheet.dimensions

    # 正文单元格统一自动换行、顶端对齐，长段落阅读更舒服
    for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row, max_col=len(COLUMNS)):
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    workbook.save(output)


def write_failed(failed, path):
    """记录抓取失败的条目，便于后续补抓。"""
    if not failed:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        file.write("方剂名称\t详情页链接\t失败原因\n")
        for name, url, reason in failed:
            file.write(f"{name}\t{url}\t{reason}\n")


# ============ 主流程 ============

def parse_args():
    parser = argparse.ArgumentParser(description="爬取中医百科「中医方剂」并导出 Excel")
    # 输出表格、缓存、限流参数的默认值和说明
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Excel 输出路径")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="断点续爬缓存文件")
    parser.add_argument("--limit", type=int, default=0, help="只抓取前 N 首（0 表示全部）")
    parser.add_argument("--delay-min", type=float, default=1.0, help="请求间隔下限（秒）")
    parser.add_argument("--delay-max", type=float, default=2.0, help="请求间隔上限（秒）")
    parser.add_argument("--timeout", type=float, default=20.0, help="单次请求超时（秒）")
    parser.add_argument("--refresh", action="store_true", help="忽略本地缓存，重新抓取全部详情页")
    args = parser.parse_args()

    # 对请求间隔做下限保护，避免误传过小参数导致高频请求
    if args.delay_min > args.delay_max:
        parser.error("--delay-min 不能大于 --delay-max")
    if args.delay_min < 0.2:
        parser.error("请求间隔过短，请将 --delay-min 设置为不小于 0.2 秒，以免给对方服务器造成压力")
    return args


def main():
    args = parse_args()
    started_at = time.time()

    # 第一步：先确认 robots.txt 允许，再请求目录页
    session = build_session()
    robots = load_robots(session)
    if not robots.can_fetch(USER_AGENT, INDEX_URL):
        print("robots.txt 不允许抓取目录页，已停止。")
        return

    print(f"[1/3] 读取目录页: {INDEX_URL}")
    index_html = fetch(session, INDEX_URL, args.timeout)
    formulas = parse_catalog(index_html)
    # --limit 只截断本次要处理的列表，不影响缓存里已有的数据
    if args.limit > 0:
        formulas = formulas[: args.limit]
    print(f"      目录中解析到 {len(formulas)} 首方剂（已按链接去重）")
    if not formulas:
        print("未解析到方剂条目，请检查页面结构是否变化。")
        return

    # 载入断点续爬缓存；--refresh 则先清空旧缓存
    cache_path = args.cache
    if args.refresh and cache_path.exists():
        cache_path.unlink()
        print("      已按 --refresh 清空旧缓存")
    cache = {} if args.refresh else load_cache(cache_path)
    print(f"      本地已有缓存 {len(cache)} 条，可跳过的页面不会重复请求")

    # 第二步：逐首请求详情页。单线程 + 随机间隔，尽量降低对站点的压力
    print(f"[2/3] 开始抓取详情页（间隔 {args.delay_min}~{args.delay_max} 秒）")
    failed = []
    interrupted = False
    bar = tqdm(formulas, desc="方剂详情", unit="首", ncols=100, dynamic_ncols=True)
    try:
        for index, item in enumerate(bar, start=1):
            bar.set_postfix_str(item["name"])
            # 缓存命中：上次已经抓过，直接跳过（断点续爬的关键）
            if item["url"] in cache:
                continue
            if not robots.can_fetch(USER_AGENT, item["url"]):
                failed.append((item["name"], item["url"], "robots.txt 不允许抓取"))
                continue

            try:
                html = fetch(session, item["url"], args.timeout)
                detail = parse_detail(html)
                record = {
                    "url": item["url"],
                    # 优先用详情页 h1 作名称，取不到时退回目录页的链接文字
                    "name": detail["name"] or item["name"],
                    "category": item["category"],
                    "sub_category": item["sub_category"],
                    "aliases": detail["aliases"],
                    "sections": detail["sections"],
                    "images": detail["images"],
                    "crawled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                # 先写内存再落盘，两者保持一致
                cache[item["url"]] = record
                append_cache(cache_path, record)
            except requests.RequestException as exc:
                # 网络/HTTP 错误：记录后继续，不中断整轮任务
                failed.append((item["name"], item["url"], f"请求失败: {exc}"))
                bar.write(f"[失败] {item['name']} -> {exc}")
            except Exception as exc:  # 单条解析异常不影响整体
                failed.append((item["name"], item["url"], f"解析失败: {exc}"))
                bar.write(f"[失败] {item['name']} -> {exc}")

            # 最后一条不用再等；其余每条抓完随机休眠
            if index < len(formulas):
                time.sleep(random.uniform(args.delay_min, args.delay_max))
    except KeyboardInterrupt:
        # 手动 Ctrl+C：不丢已抓数据，继续往下导出
        interrupted = True
        bar.write("已手动中断，正在导出已抓取的数据；下次运行会自动续爬。")
    finally:
        bar.close()

    # 按目录顺序取记录，保证 Excel 行序与网站目录一致
    records = [cache[item["url"]] for item in formulas if item["url"] in cache]
    if not records:
        print("没有抓取到任何数据，未生成 Excel。")
        return

    # 第三步：导出 Excel，并写失败清单（没失败就不生成文件）
    print(f"[3/3] 导出 Excel: {args.output}")
    export_excel(records, args.output)
    write_failed(failed, DEFAULT_FAILED_LOG)

    elapsed = time.time() - started_at
    print("-" * 60)
    print(f"完成: 共 {len(records)} 首方剂，失败 {len(failed)} 条，耗时 {elapsed / 60:.1f} 分钟")
    print(f"表格: {args.output}")
    if failed:
        print(f"失败记录: {DEFAULT_FAILED_LOG}（可再次运行脚本自动补抓）")
    if interrupted:
        print("提示: 本次为中断后导出，重新运行同一条命令即可继续抓取剩余条目。")


if __name__ == "__main__":
    main()
