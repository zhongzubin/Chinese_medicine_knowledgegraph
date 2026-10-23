# -*- coding: utf-8 -*-
"""爬取中医百科「中药大全」下全部中药材，并导出为 Excel 表格。

数据来源: https://zhongyibaike.com/wiki/中药大全
输出文件: crawler/中药大全.xlsx

用法:
    python crawler/zhongyao_spider.py                  # 全量爬取（默认断点续爬）
    python crawler/zhongyao_spider.py --limit 5        # 只抓前 5 味，用于试跑
    python crawler/zhongyao_spider.py --delay-min 2 --delay-max 3.5
    python crawler/zhongyao_spider.py --refresh        # 忽略本地缓存，重新抓取

爬取规范:
    1. 单线程串行抓取，请求间隔默认随机 1~2 秒，不对站点造成压力；
    2. 请求前检查 robots.txt（该站仅禁止 /admin 路径）；
    3. 失败按指数退避重试，仍失败则跳过并记录，不暴力重试；
    4. 每抓完一味立即写入缓存，中断后再次运行可续爬，不重复请求已抓过的页面。

代码结构（建议按此顺序阅读）:
    1. 请求与合规    build_session / fetch / load_robots
    2. 文本提取      normalize_text / iter_blocks / collect_lines
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
# 目录页：一页列出全部药材，只需请求一次
INDEX_URL = f"{BASE_URL}/wiki/中药大全"
ROBOTS_URL = f"{BASE_URL}/robots.txt"

# 使用自定义 UA 表明身份，方便站点管理员识别；该站对普通 UA 也可访问
USER_AGENT = (
    "ChineseMedicineKnowledgeGraphBot/1.0 "
    "(+https://github.com/zhongzubin/Chinese_medicine_knowledgegraph)"
)

# 输出表格、断点续爬缓存、失败记录都放在 crawler 目录下
DEFAULT_OUTPUT = BASE_DIR / "data" / "中药大全.xlsx"
DEFAULT_CACHE = BASE_DIR / ".cache" / "zhongyao_daquan.jsonl"
DEFAULT_FAILED_LOG = BASE_DIR / "data" / "中药大全_失败记录.txt"

# 表格列: (列名, 列宽)
# 名称/大类/子类来自目录页，其余列来自详情页；列宽只是 Excel 显示宽度
COLUMNS = [
    ("序号", 6),
    ("药材名称", 12),
    ("大类", 14),
    ("子类", 16),
    ("英文名", 24),
    ("别名", 30),
    ("来源", 42),
    ("性味", 18),
    ("炮制", 32),
    ("性状", 42),
    ("功效", 32),
    ("经脉", 14),
    ("主治", 42),
    ("用法用量", 18),
    ("注意禁忌", 24),
    ("药方", 50),
    ("其他信息", 36),
    ("图片链接", 36),
    ("详情页链接", 28),
    ("爬取时间", 20),
]

OTHER_COL = "其他信息"

# 详情页小标题 -> 表格列
# 按顺序匹配，先命中的关键词生效；
# 站点有时把「中毒症状」「解救方法」单独成节，一并归入注意禁忌
FIELD_KEYWORDS = (
    ("来源", "来源"),
    ("性味", "性味"),
    ("炮制", "炮制"),
    ("性状", "性状"),
    ("功效", "功效"),
    ("经脉", "经脉"),
    ("归经", "经脉"),
    ("主治", "主治"),
    ("用法", "用法用量"),
    ("用量", "用法用量"),
    ("禁忌", "注意禁忌"),
    ("注意", "注意禁忌"),
    ("中毒", "注意禁忌"),
    ("解救", "注意禁忌"),
    ("药方", "药方"),
)

# 部分页面的方剂直接以「1.红参熟地汤」「一、银耳羹」「葱枣汤」这类标题出现，
# 有的带序号前缀，有的以汤/粥/散等剂型结尾，用这两条规则识别
RECIPE_PREFIX_RE = re.compile(r"^\s*(?:[0-9０-９]+|[一二三四五六七八九十百]+)\s*[、.．]")
RECIPE_SUFFIX_RE = re.compile(r"(?:汤|粥|酒|膏|散|茶|饮|丸|羹|煎|方)$")

# 解析时只把这些标签当作“内容块”，其余行内标签只取文字
BLOCK_TAGS = {"h2", "h3", "h4", "p", "ul", "ol", "table", "blockquote", "dl", "pre"}
# 页面用 div/section 层层包裹正文，遇到容器标签就继续向下找内容块
CONTAINER_TAGS = {"div", "section", "article", "header", "main"}
# 行内标签统一 unwrap 成纯文本（药材页无需保留 <a> 结构）
INLINE_TAGS = ("a", "strong", "b", "em", "i", "span", "font", "small", "sub", "sup")

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]{2,}")
EXCEL_CELL_LIMIT = 32000


# ============ 基础工具 ============

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
                # 列表：每个 <li> 单独成行（如别名、药方清单）
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


# ============ 页面解析 ============

def parse_catalog(html):
    """解析目录页，返回 [{name, url, category, sub_category}, ...]（已去重）。"""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find("section", id="content") or soup
    medicines = []
    seen = set()
    category = ""

    # 目录结构：h2 是大类（如「解表药」），其后紧跟 ul/ol；
    # 每个 li 是一个子类（如「辛温解表药」），li 里的 strong 是子类名，a 是药材链接
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
                # 同一药材可能在多个子类下重复出现，用 URL 去重
                if not name or url in seen:
                    continue
                seen.add(url)
                medicines.append({
                    "name": name,
                    "url": url,
                    "category": category,
                    "sub_category": sub_category,
                })
    return medicines


def classify_section(heading):
    """把详情页大标题映射到表格列，无法识别时返回 None。"""
    # 「名称」块单独处理（里面是英文名和别名）
    if heading == "名称":
        return None
    # 大标题固定为「X的种植和炮制」「X的效果」「X的药方」三类
    if "药方" in heading:
        return "药方"
    if "炮制" in heading:
        return "炮制"
    if "功效" in heading or "效果" in heading:
        return "功效"
    return None


def classify_field(heading):
    """把详情页小标题映射到表格列。"""
    # 关键词表是有序的，先到先得（详见 FIELD_KEYWORDS 的注释）
    for keyword, column in FIELD_KEYWORDS:
        if keyword in heading:
            return column
    return None


def looks_like_recipe(heading):
    """判断一个未归类标题是否更像方剂名。"""
    # 例：「1.红参熟地汤」命中序号前缀；「葱枣汤」「银耳羹」命中毒型后缀
    text = heading.replace(" ", "")
    return bool(RECIPE_PREFIX_RE.match(text) or RECIPE_SUFFIX_RE.search(text))


def split_name_info(lines):
    """从「名称」区块里拆出英文名与别名。"""
    # 先挑出纯英文行作为英文名（该站英文名通常单独一行），其余当别名候选
    english_name = ""
    rest = []
    for line in lines:
        if not english_name and LATIN_RE.search(line) and not CJK_RE.search(line):
            english_name = line
        else:
            rest.append(line)

    # 别名优先取含顿号的那一行；否则把多行短词拼起来，排除带句号的长描述
    aliases = ""
    for line in rest:
        if "、" in line:
            aliases = line
            break
    if not aliases:
        short_items = [item for item in rest if len(item) <= 15 and not item.endswith("。")]
        pool = short_items if len(short_items) > 1 else rest
        aliases = "、".join(pool)

    # 去重，保留顺序
    if aliases:
        unique = []
        for item in re.split(r"[、,，;；]", aliases):
            item = item.strip()
            if item and item not in unique:
                unique.append(item)
        aliases = "、".join(unique)
    return english_name, aliases


def parse_detail(html):
    """解析单味药材详情页，返回名称信息、分栏正文与图片链接。"""
    soup = BeautifulSoup(html, "html.parser")

    # 详情页的 h1 就是药材名
    title_tag = soup.find("h1")
    name = normalize_text(title_tag.get_text()) if title_tag else ""

    # 只解析正文区域，避开侧边栏、推荐位等干扰内容
    content = soup.find("section", id="content")
    if content is None:
        raise ValueError("页面缺少 section#content，可能是无效页面")

    # 先收集图片地址，再清理噪音节点
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
    # 行内标签（a/strong/span 等）拆成纯文本，只留文字
    for tag in content.find_all(INLINE_TAGS):
        tag.unwrap()

    sections = {}       # 列名 -> 段落列表
    name_lines = []     # 「名称」区块的原始行（用来提取英文名和别名）
    # 三个状态变量共同决定一段文字写进哪一列：
    #   top_column     —— 最近一个 h2 对应的列，如「X的种植和炮制」→ 炮制
    #   current_column —— 最近一个 h3 对应的列，优先级最高
    #   prefix         —— 未识别标题的原文，拼在正文前，避免信息丢失
    top_column = None
    current_column = None
    prefix = ""
    # 详情页首个区块是「名称」，个别页面缺少这个小标题
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
                    # 先看大标题（药方/炮制/功效），再看小标题关键词
                    mapped = classify_section(heading) or classify_field(heading)
                    if mapped:
                        top_column = mapped
                        prefix = ""
                    elif top_column == "功效" and looks_like_recipe(heading):
                        # 效果之后直接以方剂名作二级标题的页面，归入「药方」
                        top_column = "药方"
                        prefix = heading
                    elif top_column in ("炮制", "药方"):
                        # 炮制步骤、后续方剂沿用上一栏，标题作为前缀
                        # 例：附子页面的「①盐附子」、连续多个方剂标题
                        prefix = heading
                    else:
                        # 完全认不出来的标题，归入其他信息，标题保留作前缀
                        top_column = OTHER_COL
                        prefix = heading
                current_column = top_column
            else:
                mapped = classify_field(heading)
                # 小标题优先；认不出来就沿用当前 h2 的列
                if mapped:
                    current_column = mapped
                    prefix = ""
                else:
                    current_column = top_column or OTHER_COL
                    prefix = heading
            continue

        # ---------- 情况二：普通正文块 ----------
        lines = collect_lines(block)
        if not lines:
            continue
        if in_name_block:
            name_lines.extend(lines)
            continue

        column = current_column or OTHER_COL
        # 同一块内的多行合并成一段（如炮制步骤的多个 <p>）
        text = "\n".join(lines)
        # prefix 只在未识别标题后加一次，例如「①盐附子：...」
        if prefix:
            text = f"{prefix}：{text}"
        sections.setdefault(column, []).append(text)

    # 从「名称」块中拆出英文名和别名
    english_name, aliases = split_name_info(name_lines)
    return {
        "name": name,
        "english_name": english_name,
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
    """每抓完一味立即追加写入，保证中断后可以续爬。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
        # 立即刷盘，即使进程被强制结束也不丢已抓到的数据
        file.flush()


# ============ 导出 ============

def export_excel(records, output):
    """把抓取结果写入带格式的 Excel。"""
    # 第 1 步：把 JSON 记录拍平成「一行一味药材」的字典
    rows = []
    for index, record in enumerate(records, start=1):
        sections = record.get("sections") or {}
        rows.append({
            "序号": index,
            "药材名称": record.get("name", ""),
            "大类": record.get("category", ""),
            "子类": record.get("sub_category", ""),
            "英文名": record.get("english_name", ""),
            "别名": record.get("aliases", ""),
            "来源": sections.get("来源", ""),
            "性味": sections.get("性味", ""),
            "炮制": sections.get("炮制", ""),
            "性状": sections.get("性状", ""),
            "功效": sections.get("功效", ""),
            "经脉": sections.get("经脉", ""),
            "主治": sections.get("主治", ""),
            "用法用量": sections.get("用法用量", ""),
            "注意禁忌": sections.get("注意禁忌", ""),
            "药方": sections.get("药方", ""),
            OTHER_COL: sections.get(OTHER_COL, ""),
            "图片链接": "\n".join(record.get("images") or []),
            "详情页链接": record.get("url", ""),
            "爬取时间": record.get("crawled_at", ""),
        })

    columns = [name for name, _ in COLUMNS]
    # 第 2 步：转 DataFrame，逐单元格截断，避免超出 Excel 单元格上限
    dataframe = pd.DataFrame(rows, columns=columns)
    dataframe = dataframe.map(lambda value: clip(str(value)) if value is not None else "")

    output.parent.mkdir(parents=True, exist_ok=True)
    dataframe.to_excel(output, index=False, sheet_name="中药大全", engine="openpyxl")

    # 第 3 步：重新打开 Excel，加表头样式、边框、冻结窗格和筛选器
    workbook = load_workbook(output)
    sheet = workbook["中药大全"]

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
    # 冻结「序号 + 药材名称」两列，滚动时始终能看到药材名
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
        file.write("药材名称\t详情页链接\t失败原因\n")
        for name, url, reason in failed:
            file.write(f"{name}\t{url}\t{reason}\n")


# ============ 主流程 ============

def parse_args():
    parser = argparse.ArgumentParser(description="爬取中医百科「中药大全」并导出 Excel")
    # 输出表格、缓存、限流参数的默认值和说明
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Excel 输出路径")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="断点续爬缓存文件")
    parser.add_argument("--limit", type=int, default=0, help="只抓取前 N 味（0 表示全部）")
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
    medicines = parse_catalog(index_html)
    # --limit 只截断本次要处理的列表，不影响缓存里已有的数据
    if args.limit > 0:
        medicines = medicines[: args.limit]
    print(f"      目录中解析到 {len(medicines)} 味药材（已按链接去重）")
    if not medicines:
        print("未解析到药材条目，请检查页面结构是否变化。")
        return

    # 载入断点续爬缓存；--refresh 则先清空旧缓存
    cache_path = args.cache
    if args.refresh and cache_path.exists():
        cache_path.unlink()
        print("      已按 --refresh 清空旧缓存")
    cache = {} if args.refresh else load_cache(cache_path)
    print(f"      本地已有缓存 {len(cache)} 条，可跳过的页面不会重复请求")

    # 第二步：逐味请求详情页。单线程 + 随机间隔，尽量降低对站点的压力
    print(f"[2/3] 开始抓取详情页（间隔 {args.delay_min}~{args.delay_max} 秒）")
    failed = []
    interrupted = False
    bar = tqdm(medicines, desc="中药详情", unit="味", ncols=100, dynamic_ncols=True)
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
                    "english_name": detail["english_name"],
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
            if index < len(medicines):
                time.sleep(random.uniform(args.delay_min, args.delay_max))
    except KeyboardInterrupt:
        # 手动 Ctrl+C：不丢已抓数据，继续往下导出
        interrupted = True
        bar.write("已手动中断，正在导出已抓取的数据；下次运行会自动续爬。")
    finally:
        bar.close()

    # 按目录顺序取记录，保证 Excel 行序与网站目录一致
    records = [cache[item["url"]] for item in medicines if item["url"] in cache]
    if not records:
        print("没有抓取到任何数据，未生成 Excel。")
        return

    # 第三步：导出 Excel，并写失败清单（没失败就不生成文件）
    print(f"[3/3] 导出 Excel: {args.output}")
    export_excel(records, args.output)
    write_failed(failed, DEFAULT_FAILED_LOG)

    elapsed = time.time() - started_at
    print("-" * 60)
    print(f"完成: 共 {len(records)} 味药材，失败 {len(failed)} 条，耗时 {elapsed / 60:.1f} 分钟")
    print(f"表格: {args.output}")
    if failed:
        print(f"失败记录: {DEFAULT_FAILED_LOG}（可再次运行脚本自动补抓）")
    if interrupted:
        print("提示: 本次为中断后导出，重新运行同一条命令即可继续抓取剩余条目。")


if __name__ == "__main__":
    main()
