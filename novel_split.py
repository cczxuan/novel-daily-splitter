#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小说按字数切分工具（方案 A：结构信号 + DP）

把一本小说（TXT / EPUB）切成每份约 target 字（默认 5500，硬上限 6500）的阅读单元，
每天读一份。切点只落在完整段落末尾，不会切断句子或段落。

用法:
    python novel_split.py 小说.txt
    python novel_split.py 小说.epub -o 输出目录 --target 5500 --max 6500

输出:
    <输出目录>/
    ├── txt/第01天_第01章.txt ...
    ├── <书名>_split.epub
    └── 清单.md

依赖: 仅 Python 标准库（EPUB 读写也不需要额外安装东西）。
     可选：装 charset-normalizer 会让罕见编码认得更好。
"""

from __future__ import annotations

import argparse
import datetime
import html
import posixpath
import re
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------- 配置

DEFAULT_TARGET = 5500          # 每份目标字数
DEFAULT_MAX = 6500             # 硬上限：任何一份都不超过这个字数
DEFAULT_MIN = 4000             # 软下限：低于此值要罚分，避免为了凑章末切出很短的份

W_LEN = 1.0                    # 长度偏差权重
W_MIN = 2.0                    # 过短罚分权重
W_SCENE = 0.04                 # 场景信号奖励权重（0.04 约等于值得为它偏离目标 1100 字）

TOKENS_PER_CHAR = 0.7          # 中文 token 粗估系数，仅用于报告

WHITESPACE = re.compile(r"\s+")

# 段落算"完整结束"的收尾字符。
# 注意：分号、冒号不算句末 —— "如果采纳方案A，会有400人死亡；" 这种后面还有话。
SENT_TAIL_CHARS = set("。．.！!？?…~～」』】》）)｝〕”’\"'")

# 句末（用于超长段落内部退让切分，这里可以放宽到分号）
SENT_END = re.compile(r'[。！？…；;]+[」』】》）)｝〕”’"\']*|[.．!?]{1,3}[」』"\')]*')

# 时间跳转词：小说里天然的"适合停下来"的信号
TIME_JUMP = re.compile(
    r"^(?:第二天|次日|翌日|第三日|三日后|数日后|几日后|几天后|半个月后|一个月后|几个月后|"
    r"半年后|一年后|多年以后|多年后|当晚|那天晚上|当天夜里|当天晚上|与此同时|同一时间|"
    r"次日清晨|第二天一早|第二天早上|天亮时|入夜|夜幕降临|此时)"
)

# 图注 / 脚注碎片：这类行不能充当切点，否则会"断在半张图、半条注上"
FRAGMENT_START = re.compile(
    r"^(?:图表?\s*\d|表\s*\d|图\s*\d|续表|续\s*表|（续）|\(续\)|资料来源|数据来源|"
    r"来源[：:]|注[：:]|[①-⑳]|[\(（]\d+[\)）])"
)

CH_NUM = r"[〇零一二三四五六七八九十百千万两0-9０-９]{1,8}"
CHAPTER_PATTERNS = [
    re.compile(rf"^第\s*{CH_NUM}\s*[章回节節][\s:：、.．]"),
    re.compile(rf"^第\s*{CH_NUM}\s*[章回节節]$"),
    re.compile(rf"^第\s*{CH_NUM}\s*[卷部篇]"),
    re.compile(r"^[Cc]hapter\s*\d+"),
]
CHAPTER_KEYWORDS = ("序章", "序言", "序幕", "楔子", "引子", "前言", "后记", "尾声",
                    "终章", "尾章", "大结局", "番外")
TOC_KEYWORDS = ("目录", "目次", "contents", "content")


@dataclass
class Config:
    target: int = DEFAULT_TARGET
    max_chars: int = DEFAULT_MAX
    min_chars: int = DEFAULT_MIN


class BookProblem(Exception):
    """这本书本身没法切分。异常内容是一份给人看的说明。"""


# 判定"这本书能用文字切分"的门槛
MIN_USABLE_CHARS = 2000        # 文字少于这个数就没法切
CHARS_PER_IMAGE_FLOOR = 60     # 平均每张图至少要有这么多字，否则判为扫描版


SCANNED_MESSAGE = """
{line}
  这本书切不了：它是扫描版（图片版）EPUB
{line}

  文件    《{title}》
  统计    文字只有 {chars:,} 字，却有 {images} 张图片

为什么会这样
  这本书的正文不是文字，而是一页一页扫描（或拍照）出来的图片。
  本工具是按「每天读多少字」来切的，没有文字就没有东西可切。
  页面上残留的那几十上百字，通常只是封面、版权页、页眉页脚。

怎么办
  1. 先用 OCR 把图片认成文字
     · 有界面的：ABBYY FineReader、Adobe Acrobat（"识别文本"）、
       Umi-OCR（免费开源，专门做中文）、微信/QQ 的图片转文字
     · 命令行的：ocrmypdf（处理 PDF）、PaddleOCR（处理图片）
  2. 拿 OCR 出来的 TXT，或者重新做的文字版 EPUB，再跑一次本工具
  3. 如果你只是想看图、不需要按字数切着读，那直接看原 EPUB 就行，
     不用经过这个工具

  想强行试一下也行：加 --force 参数（结果大概率是空的，不建议）。
"""

EMPTY_MESSAGE = """
{line}
  这本书切不了：里面没有可提取的文字
{line}

  文件    《{title}》
  统计    文字 {chars:,} 字，图片 {images} 张

可能的原因
  · 正文是扫描图片（这种情况请先做 OCR，见 README 的"已知限制"）
  · 文件损坏或下载不完整
  · 加密或有阅读权限限制

  用 --force 可以强行试一下。
"""

TINY_MESSAGE = """
{line}
  这本书内容太少，切分没有意义
{line}

  文件    《{title}》
  统计    文字只有 {chars:,} 字（本工具至少需要 {need:,} 字）

  这么短的正文，只会切出 1 份，也就是等于没切。
  如果你确实想切（比如测试工具），加 --force 参数。
"""


def diagnose_epub(title: str, text_chars: int, image_count: int) -> str | None:
    """扫描版 / 空书 / 内容太少的判断。返回 None 表示正常。"""
    line = "═" * 56
    scanned = image_count >= 5 and text_chars < max(
        MIN_USABLE_CHARS, image_count * CHARS_PER_IMAGE_FLOOR
    )
    if text_chars == 0:
        return EMPTY_MESSAGE.format(line=line, title=title, chars=text_chars, images=image_count)
    if scanned:
        return SCANNED_MESSAGE.format(line=line, title=title, chars=text_chars, images=image_count)
    if text_chars < MIN_USABLE_CHARS:
        return TINY_MESSAGE.format(line=line, title=title, chars=text_chars, need=MIN_USABLE_CHARS)
    return None


@dataclass
class Para:
    """一段：在 book.text 里的 [start, end) 区间。"""

    start: int
    end: int
    blank_before: bool = False


@dataclass
class Chapter:
    para_idx: int
    title: str


@dataclass
class Book:
    title: str
    text: str
    paras: list[Para]
    chapters: list[Chapter] = field(default_factory=list)
    encoding: str = "utf-8"
    source_kind: str = "txt"
    warnings: list[str] = field(default_factory=list)
    image_files: dict[str, bytes] = field(default_factory=dict)        # 相对路径 -> 图片数据
    images_before: dict[int, list[str]] = field(default_factory=dict)  # 段落下标 -> 该段之前的图片


@dataclass
class Unit:
    """切分的最小单位：一个或多个完整段落。"""

    start: int
    end: int
    chars: int
    start_para: int
    end_para: int
    continued: bool = False   # True 表示它只是某个超长段落的一部分


# ---------------------------------------------------------------- 基础工具


def decode_bytes(data: bytes) -> tuple[str, str]:
    """尽量认对编码。中文小说 txt 常见 GBK / GB18030 系。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes  # 可选依赖

        best = from_bytes(data).best()
        if best is not None:
            return str(best), best.encoding
    except Exception:
        pass
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def count_chars(text: str) -> int:
    """正文字数：不计空白。"""
    return len(WHITESPACE.sub("", text))


def title_of(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip()


def ends_sentence(line: str) -> bool:
    s = line.rstrip()
    return bool(s) and s[-1] in SENT_TAIL_CHARS


def is_fragment(line: str) -> bool:
    """图注、脚注、续表标记之类的碎片。"""
    s = line.strip()
    return len(s) <= 90 and bool(FRAGMENT_START.match(s))


def can_end_unit(line: str) -> bool:
    """这一段能不能充当一份的结尾：既要是完整句子，又不能是图注碎片。"""
    return ends_sentence(line) and not is_fragment(line)


# ---------------------------------------------------------------- 载入 TXT


def load_txt(path: Path) -> Book:
    text, enc = decode_bytes(path.read_bytes())
    text = normalize_newlines(text)

    paras: list[Para] = []
    offset = 0
    blank_pending = False
    for line in text.split("\n"):
        if line.strip() == "":
            blank_pending = True
        else:
            paras.append(Para(offset, offset + len(line.rstrip()), blank_pending))
            blank_pending = False
        offset += len(line) + 1

    book = Book(title=path.stem, text=text, paras=paras, encoding=enc, source_kind="txt")
    drop_toc(book)
    book.chapters = detect_chapters(book)
    return book


def drop_toc(book: Book) -> None:
    """网文 txt 开头常有目录页，会污染章节识别，这里尽量丢掉。"""
    if len(book.paras) < 6:
        return
    head = book.paras[: min(400, len(book.paras))]
    lines = [title_of(book.text[p.start : p.end]) for p in head]

    start = -1
    for i, line in enumerate(lines[:20]):
        if line.replace(" ", "").lower() in TOC_KEYWORDS:
            start = i + 1
            break
    if start < 0 and sum(1 for line in lines[:8] if is_chapter_title(line)) >= 5:
        start = 0
    if start < 0:
        return

    run = 0
    while start + run < len(head) and is_chapter_title(lines[start + run]):
        run += 1
    if run < 5:
        return

    drop = set(range(start, start + run))
    drop.add(max(0, start - 1))
    book.paras = [p for i, p in enumerate(book.paras) if i not in drop]
    book.warnings.append(f"跳过了开头的目录页（{run} 行）")


# ---------------------------------------------------------------- 载入 EPUB


class XhtmlToBlocks(HTMLParser):
    """把 XHTML 抽成段落列表 + 标题列表。"""

    BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
             "li", "tr", "section", "article", "blockquote"}
    SKIP = {"head", "style", "script", "title", "meta", "link"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.headings: list[str] = []
        self.images: list[tuple[int, str, str]] = []   # (第几个段落之前, src, alt)
        self._buf: list[str] = []
        self._skip = 0
        self._in_heading = 0

    def handle_starttag(self, tag, attrs):
        # <img src> 是普通图片，<image xlink:href> 是 SVG 整页图
        if tag in ("img", "image"):
            d = dict(attrs)
            src = d.get("src") or d.get("xlink:href") or d.get("href") or ""
            if src:
                self.images.append((len(self.parts), src, d.get("alt", "")))
            return
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self._flush()
            if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                self._in_heading += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self._flush()
            if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                self._in_heading = max(0, self._in_heading - 1)

    def handle_data(self, data):
        if self._skip or not data.strip():
            return
        self._buf.append(data)

    def _flush(self):
        text = "".join(self._buf).strip()
        self._buf.clear()
        if not text:
            return
        self.parts.append(text)
        if self._in_heading:
            self.headings.append(text)

    def close(self):
        super().close()
        self._flush()


class TocLinkParser(HTMLParser):
    """从 EPUB3 的 nav.xhtml 里抽目录链接。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href", "")
            self._buf.clear()

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = "".join(self._buf).strip()
            if self._href and text:
                self.items.append((self._href, text))
            self._href = None


def parse_ncx_labels(data: bytes) -> dict[str, str]:
    """EPUB2 目录：navPoint 的标题 -> 目标文件。"""
    labels: dict[str, str] = {}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return labels
    for node in root.iter():
        if not node.tag.endswith("navPoint"):
            continue
        text = src = ""
        for child in node:
            if child.tag.endswith("navLabel"):
                for t in child.iter():
                    if t.tag.endswith("text") and (t.text or "").strip():
                        text = t.text.strip()
                        break
            elif child.tag.endswith("content"):
                src = child.get("src", "")
        if text and src:
            labels.setdefault(src.split("#")[0], text)
    return labels


def parse_nav_labels(data: bytes) -> dict[str, str]:
    """EPUB3 目录：nav.xhtml 里的链接。"""
    parser = TocLinkParser()
    parser.feed(decode_bytes(data)[0])
    labels: dict[str, str] = {}
    for href, text in parser.items:
        if "://" in href:
            continue
        labels.setdefault(href.split("#")[0], text)
    return labels


def looks_like_toc_doc(link_count: int, lines: list[str]) -> bool:
    """判断某一节是不是目录页。"""
    if link_count >= 30:
        return True
    if len(lines) < 15:
        return False
    plain = sum(1 for ln in lines if len(ln) <= 40 and not can_end_unit(ln))
    return plain / len(lines) >= 0.8


def load_epub(path: Path, keep_toc: bool = False, force: bool = False) -> Book:
    warnings: list[str] = []
    with zipfile.ZipFile(path) as zf:
        try:
            mime = zf.read("mimetype")
        except KeyError:
            mime = b""
        if b"application/epub+zip" not in mime:
            warnings.append("这个文件不太像标准 EPUB（mimetype 不对），已在尽力解析")
        try:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
        except KeyError:
            raise BookProblem(
                "\n这个文件不是 EPUB（缺少 META-INF/container.xml），或者已经损坏。\n"
                "请确认下载完整，或者换成 TXT 再试。\n"
            ) from None
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None:
            raise ValueError("EPUB 结构异常：找不到 rootfile")
        opf_path = rootfile.get("full-path")
        opf_dir = str(Path(opf_path).parent).replace("\\", "/")
        if opf_dir in (".", ""):
            opf_dir = ""

        opf = ET.fromstring(zf.read(opf_path))
        title = "未命名"
        for el in opf.iter():
            if el.tag.endswith("}title") or el.tag == "title":
                title = (el.text or "").strip() or title
                break

        manifest: dict[str, tuple[str, str]] = {}
        for el in opf.iter():
            if el.tag.endswith("}item"):
                manifest[el.get("id")] = (el.get("href", ""), el.get("media-type", ""))
        spine = [el.get("idref") for el in opf.iter() if el.tag.endswith("}itemref")]

        # ---- 目录：优先 EPUB3 的 nav，其次 EPUB2 的 ncx
        labels: dict[str, str] = {}
        nav_href = ""
        for el in opf.iter():
            if el.tag.endswith("}item") and "nav" in (el.get("properties") or ""):
                nav_href = el.get("href", "")
        for iid, (href, media) in manifest.items():
            if media == "application/x-dtbncx+xml" and not nav_href:
                nav_href = href
        if nav_href:
            full = f"{opf_dir}/{nav_href}" if opf_dir else nav_href
            try:
                raw = zf.read(full)
                labels = parse_nav_labels(raw) if full.endswith((".xhtml", ".html", ".htm")) else parse_ncx_labels(raw)
            except KeyError:
                warnings.append(f"EPUB 里找不到目录文件：{full}")

        chunks: list[str] = []            # 拼接用的碎片，保证偏移量精确
        paras: list[Para] = []
        chapters: list[Chapter] = []
        image_files: dict[str, bytes] = {}
        images_before: dict[int, list[str]] = {}
        cursor = 0
        index = 0
        image_count = 0
        skipped_toc = 0
        missing_images = 0

        def add_para(body: str, blank: bool) -> None:
            nonlocal cursor
            if not body.strip():
                return
            paras.append(Para(cursor, cursor + len(body), blank))
            chunks.append(body)
            cursor += len(body)
            chunks.append("\n\n")
            cursor += 2

        for idref in spine:
            href, media = manifest.get(idref, ("", ""))
            if not href or ("html" not in media and not href.endswith((".xhtml", ".html", ".htm"))):
                continue
            full = f"{opf_dir}/{href}" if opf_dir else href
            try:
                raw = zf.read(full)
            except KeyError:
                warnings.append(f"EPUB 中引用了不存在的文件：{full}")
                continue
            # <img> 是普通图片，<image xlink:href> 是 SVG 封面/整页图
            image_count += raw.count(b"<img") + raw.count(b"<image")
            parser = XhtmlToBlocks()
            parser.feed(decode_bytes(raw)[0])
            parser.close()
            if not parser.parts:
                looks_like_picture = (
                    b"<img" in raw or b"<image" in raw or b"<svg" in raw
                )
                if not looks_like_picture:  # 纯图片封面属正常，不吵
                    warnings.append(f"EPUB 中这一节没有正文：{href}")
                continue
            link_count = raw.count(b"<a ")
            if not keep_toc and looks_like_toc_doc(link_count, parser.parts):
                skipped_toc += 1
                continue
            index += 1
            heading = labels.get(href, "") or (parser.headings[0] if parser.headings else "")
            first_para = len(paras)
            for i, part in enumerate(parser.parts):
                add_para(part, blank=(index > 1 or i > 0))

            # 把这一节里的图片按位置挂到段落上
            doc_dir = posixpath.dirname(full)
            for local_idx, src, _alt in parser.images:
                if "://" in src or src.startswith("data:"):
                    continue
                target = posixpath.normpath(posixpath.join(doc_dir, src))
                if target.startswith("../") or target.startswith("/"):
                    continue
                rel = posixpath.relpath(target, opf_dir) if opf_dir else target
                try:
                    data = zf.read(target)
                except KeyError:
                    missing_images += 1
                    continue
                image_files.setdefault(rel, data)
                images_before.setdefault(first_para + local_idx, []).append(rel)

            if heading and len(heading) <= 40:
                label = heading
            elif chapters:
                label = chapters[-1].title   # 没有目录项的附属页，跟着上一节
            else:
                label = "开篇"
            chapters.append(Chapter(first_para, label))

    text = "".join(chunks)
    text_chars = count_chars(text)

    # 扫描版 / 空书 / 内容太少的判断要放在最前面，因为这时已经能确认了
    problem = diagnose_epub(title, text_chars, image_count)
    if problem and not force:
        raise BookProblem(problem)

    if skipped_toc:
        warnings.append(f"跳过了 {skipped_toc} 个目录页（用 --keep-toc 可以保留）")
    if missing_images:
        warnings.append(f"有 {missing_images} 张插图在 EPUB 里找不到，已跳过")
    if image_files:
        total_mb = sum(len(v) for v in image_files.values()) / 1024 / 1024
        warnings.append(
            f"已保留 {len(image_files)} 张插图（{total_mb:.1f} MB），会打包进输出的 EPUB；TXT 仍为纯文字"
        )
    elif image_count:
        warnings.append(f"原书含 {image_count} 张插图，但没有可用的图片文件")
    return Book(
        title=title,
        text=text,
        paras=paras,
        chapters=chapters,
        encoding="utf-8",
        source_kind="epub",
        warnings=warnings,
        image_files=image_files,
        images_before=images_before,
    )


def load_book(path: Path, keep_toc: bool = False, force: bool = False) -> Book:
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return load_epub(path, keep_toc=keep_toc, force=force)
    if suffix in (".txt", ".text", ".md", ""):
        book = load_txt(path)
        if not force and count_chars(book.text) < MIN_USABLE_CHARS:
            raise BookProblem(
                TINY_MESSAGE.format(
                    line="═" * 56, title=book.title,
                    chars=count_chars(book.text), need=MIN_USABLE_CHARS,
                )
            )
        return book
    if suffix == ".pdf":
        raise BookProblem(
            "\n这个工具不支持 PDF。\n\n"
            "  想处理 PDF，先转成 EPUB 或 TXT：\n"
            "  · 文字版 PDF：用 Calibre 转 EPUB\n"
            "  · 扫描版 PDF：先 OCR（ocrmypdf、ABBYY、Adobe Acrobat），再转\n"
        )
    raise ValueError(f"暂不支持的格式：{suffix}（目前支持 .txt 和 .epub）")


# ---------------------------------------------------------------- 章节识别


def is_chapter_title(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 40:
        return False
    for kw in CHAPTER_KEYWORDS:
        if line.startswith(kw) and len(line) <= len(kw) + 12:
            return True
    return any(p.match(line) for p in CHAPTER_PATTERNS)


def detect_chapters(book: Book) -> list[Chapter]:
    chapters: list[Chapter] = []
    for i, p in enumerate(book.paras):
        line = title_of(book.text[p.start : p.end])
        if is_chapter_title(line):
            chapters.append(Chapter(i, line))
    return chapters


# ---------------------------------------------------------------- 段落 → 单元


def build_units(book: Book, cfg: Config) -> list[Unit]:
    """把段落合并成"不可切断的单元"，再对超长单元做句子级退让切分。"""
    paras = book.paras
    n = len(paras)
    raw_units: list[Unit] = []
    i = 0
    while i < n:
        j = i
        while j < n - 1 and not can_end_unit(book.text[paras[j].start : paras[j].end]):
            j += 1  # 这一段不完整（没句末标点，或是图注碎片），与下一段并成一个单元
        start, end = paras[i].start, paras[j].end
        raw_units.append(Unit(start, end, count_chars(book.text[start:end]), i, j))
        i = j + 1

    units: list[Unit] = []
    for u in raw_units:
        if u.chars <= cfg.max_chars:
            units.append(u)
        else:
            units.extend(split_long_unit(book, u, cfg))
    return units


def split_long_unit(book: Book, unit: Unit, cfg: Config) -> list[Unit]:
    """单段超过上限时的兜底：只能在句子边界上退让。"""
    text = book.text[unit.start : unit.end]
    ends = [m.end() for m in SENT_END.finditer(text)]
    if not ends or ends[-1] < len(text.rstrip()):
        ends.append(len(text))

    cuts = [0]
    start = 0
    prev = 0
    for e in ends:
        if e <= start:
            continue
        if count_chars(text[start:e]) > cfg.max_chars:
            if prev > start:
                cuts.append(prev)
                start = prev
            else:
                hard = min(start + cfg.max_chars, len(text))
                cuts.append(hard)
                start = hard
                book.warnings.append(
                    f"有一段连续超过 {cfg.max_chars} 字找不到句末标点，已硬切"
                )
        prev = e
    cuts.append(len(text))
    cuts = sorted({c for c in cuts if 0 <= c <= len(text)})

    pieces: list[Unit] = []
    for a, b in zip(cuts, cuts[1:]):
        body = text[a:b]
        if not body.strip():
            continue
        pieces.append(
            Unit(unit.start + a, unit.start + b, count_chars(body),
                 unit.start_para, unit.end_para, continued=True)
        )
    return pieces


# ---------------------------------------------------------------- 场景信号


def scene_scores(book: Book, units: list[Unit]) -> list[float]:
    """给每个边界 j 打分：越像"适合停下来的地方"分越高（0 ~ 1）。"""
    scores = [0.0] * (len(units) + 1)
    chapter_paras = {c.para_idx: c.title for c in book.chapters}

    for j, u in enumerate(units):
        if j == 0:
            continue
        if u.start_para in chapter_paras and not u.continued:
            scores[j] = 1.0          # 正好落在章首 = 上一章结束
            continue
        para = book.paras[u.start_para]
        line = title_of(book.text[para.start : para.end])
        s = 0.0
        if para.blank_before:
            s += 0.45                # 原文里的空行 = 场景分隔
        if TIME_JUMP.match(line):
            s += 0.45                # "第二天""翌日"这类时间跳转
        scores[j] = min(s, 0.9)
    return scores


# ---------------------------------------------------------------- DP 切分


def plan_chunks(units: list[Unit], scene: list[float], cfg: Config) -> list[int]:
    """返回切点下标，形如 [0, 5, 11, ..., n]，每份 = units[i:j]。"""
    n = len(units)
    if n == 0:
        return [0]

    cum = [0] * (n + 1)
    for i, u in enumerate(units):
        cum[i + 1] = cum[i] + u.chars

    INF = float("inf")
    dp = [INF] * (n + 1)
    back = [-1] * (n + 1)
    dp[0] = 0.0

    for j in range(1, n + 1):
        i = j - 1
        while i >= 0:
            length = cum[j] - cum[i]
            if length > cfg.max_chars:      # 再往前只会更长，可以停
                break
            if dp[i] < INF:
                cost = dp[i] + chunk_cost(length, scene[j], cfg)
                if cost < dp[j]:
                    dp[j] = cost
                    back[j] = i
            i -= 1

    if dp[n] == INF:
        return greedy_fallback(units, cfg)

    cuts = []
    j = n
    while j > 0:
        cuts.append(j)
        j = back[j]
    cuts.append(0)
    return sorted(cuts)


def chunk_cost(length: int, scene_score: float, cfg: Config) -> float:
    t = cfg.target
    cost = W_LEN * ((length - t) / t) ** 2
    if length < cfg.min_chars:
        cost += W_MIN * ((cfg.min_chars - length) / cfg.min_chars) ** 2
    cost -= W_SCENE * scene_score
    return cost


def greedy_fallback(units: list[Unit], cfg: Config) -> list[int]:
    cuts = [0]
    cur = 0
    for i, u in enumerate(units):
        if cur + u.chars > cfg.max_chars and i > 0:
            cuts.append(i)
            cur = 0
        cur += u.chars
    cuts.append(len(units))
    return sorted(set(cuts))


# ---------------------------------------------------------------- 组装结果


@dataclass
class Chunk:
    day: int
    start_unit: int
    end_unit: int      # 不含
    start_para: int
    end_para: int      # 含
    text: str
    chars: int
    chapter_from: str
    chapter_to: str
    label: str


def safe_name(name: str, limit: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .")
    return (name or "未命名")[:limit]


def short_title(title: str, limit: int = 22) -> str:
    title = title.strip()
    return title if len(title) <= limit else title[: limit - 1] + "…"


def build_chunks(book: Book, units: list[Unit], cuts: list[int]) -> list[Chunk]:
    # 每个单元当前处在哪一章
    at_unit: list[str] = ["开篇"] * len(units)
    current = "开篇"
    by_para = {c.para_idx: c.title for c in book.chapters}
    for k, u in enumerate(units):
        for p in range(u.start_para, min(u.end_para, u.start_para + 3) + 1):
            if p in by_para:
                current = by_para[p]
                break
        at_unit[k] = current

    chunks: list[Chunk] = []
    for day, (i, j) in enumerate(zip(cuts, cuts[1:]), start=1):
        text = book.text[units[i].start : units[j - 1].end]
        c_from = at_unit[i]
        c_to = at_unit[j - 1]
        label = short_title(c_from) if c_from == c_to else f"{short_title(c_from)}-{short_title(c_to)}"
        chunks.append(
            Chunk(day, i, j, units[i].start_para, units[j - 1].end_para,
                  text, count_chars(text), c_from, c_to, label)
        )
    return chunks


# ---------------------------------------------------------------- 输出


def write_txt(outdir: Path, book: Book, chunks: list[Chunk], with_header: bool = True) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    total = len(chunks)
    for c in chunks:
        path = outdir / f"第{c.day:02d}天_{safe_name(c.label)}.txt"
        body = c.text
        if with_header:
            body = (
                f"【第 {c.day} 天 / 共 {total} 天】{c.chapter_from} ~ {c.chapter_to}"
                f"　正文 {c.chars:,} 字\n" + "─" * 24 + "\n\n" + body
            )
        path.write_text(body.rstrip() + "\n", encoding="utf-8")
        files.append(path)
    return files


def _xhtml(title: str, body_html: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
        f'<head><meta charset="utf-8"/><title>{html.escape(title)}</title>'
        "<style>p{margin:0 0 .55em;text-indent:2em;line-height:1.7}"
        "h2{font-size:1.05em;margin:1.4em 0 .8em;text-indent:0;font-weight:600}"
        "p.pic{text-indent:0;text-align:center;margin:1.2em 0}"
        "p.pic img{max-width:100%;height:auto}"
        ".day{color:#888;font-size:.85em;text-indent:0;margin-bottom:1.2em}</style>"
        f"</head>\n<body>\n{body_html}\n</body></html>\n"
    )


def _chunk_to_html(book: Book, chunk: Chunk, chapter_titles: set[str], with_images: bool) -> str:
    """按段落逐段生成 XHTML，并在原位置插回图片。"""
    out: list[str] = []
    for k in range(chunk.start_para, chunk.end_para + 1):
        if with_images:
            for rel in book.images_before.get(k, []):
                out.append(f'<p class="pic"><img src="{html.escape(rel)}" alt=""/></p>')
        para = book.paras[k]
        line = book.text[para.start : para.end].strip()
        if not line:
            continue
        if line in chapter_titles:
            out.append(f"<h2>{html.escape(line)}</h2>")
        else:
            out.append(f"<p>{html.escape(line)}</p>")
    if with_images:
        # 挂在最后一段之后的图
        for rel in book.images_before.get(chunk.end_para + 1, []):
            out.append(f'<p class="pic"><img src="{html.escape(rel)}" alt=""/></p>')
    return "\n".join(out)


IMAGE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
}


def write_epub(path: Path, book: Book, chunks: list[Chunk], with_images: bool = True) -> Path:
    chapter_titles = {c.title for c in book.chapters}
    nav_points: list[str] = []
    manifest: list[str] = []
    spine: list[str] = []
    day_files: dict[str, str] = {}

    used_images = {
        rel: data for rel, data in book.image_files.items() if with_images
    }

    for c in chunks:
        iid, href = f"day{c.day:03d}", f"day{c.day:03d}.xhtml"
        day_title = f"第 {c.day} 天 · {c.chapter_from} ~ {c.chapter_to}"
        body = (
            f'<p class="day">{html.escape(day_title)}　正文 {c.chars:,} 字</p>\n'
            + _chunk_to_html(book, c, chapter_titles, with_images)
        )
        day_files[href] = _xhtml(day_title, body)
        manifest.append(f'    <item id="{iid}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'    <itemref idref="{iid}"/>')
        nav_points.append(f'      <li><a href="{href}">{html.escape(day_title)}</a></li>')

    for n, (rel, _data) in enumerate(sorted(used_images.items()), start=1):
        media = IMAGE_TYPES.get(Path(rel).suffix.lower(), "image/jpeg")
        manifest.append(f'    <item id="img{n:04d}" href="{html.escape(rel)}" media-type="{media}"/>')

    nav = _xhtml(
        "目录",
        '<h2>目录</h2>\n<nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops">\n<ol>\n'
        + "\n".join(nav_points)
        + "\n</ol>\n</nav>",
    )

    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">urn:uuid:{uuid.uuid4()}</dc:identifier>\n'
        f"    <dc:title>{html.escape(book.title)}</dc:title>\n"
        "    <dc:language>zh-CN</dc:language>\n"
        f'    <meta property="dcterms:modified">{modified}</meta>\n'
        "  </metadata>\n"
        "  <manifest>\n"
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        + "\n".join(manifest)
        + "\n  </manifest>\n  <spine>\n"
        + "\n".join(spine)
        + "\n  </spine>\n</package>\n"
    )

    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n</container>\n"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container, zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
        for href, content in day_files.items():
            zf.writestr(f"OEBPS/{href}", content, zipfile.ZIP_DEFLATED)
        for rel, data in used_images.items():
            zf.writestr(f"OEBPS/{rel}", data, zipfile.ZIP_DEFLATED)
    return path


def write_index(path: Path, book: Book, chunks: list[Chunk], cfg: Config,
                stats: dict, has_txt: bool) -> Path:
    lines = [
        f"# {book.title} · 切分清单",
        "",
        f"- 来源：{book.source_kind}，编码 {book.encoding}",
        f"- 正文总字数：{stats['total_chars']:,}",
        f"- 切成 {len(chunks)} 份（目标 {cfg.target} 字，上限 {cfg.max_chars} 字，下限 {cfg.min_chars} 字）",
        f"- 每份实际：{stats['min_chunk']:,} ~ {stats['max_chunk']:,} 字，平均 {stats['avg_chunk']:,.0f} 字",
        f"- 章节数：{len(book.chapters)}",
        f"- 本次耗时：{stats['total_time']:.3f} 秒",
        "- LLM token 消耗：0（纯本地，无模型调用）",
        f"- 估算 token 总量：约 {stats['est_tokens']:,}（按 {TOKENS_PER_CHAR} token/字 粗估）",
        "",
    ]
    if book.warnings:
        lines += ["## 提醒", ""] + [f"- {w}" for w in book.warnings] + [""]
    lines += ["## 每日清单", "", "| 天 | 章节 | 字数 | 估算 token | 文件 |", "| ---: | --- | ---: | ---: | --- |"]
    for c in chunks:
        fname = f"第{c.day:02d}天_{safe_name(c.label)}.txt" if has_txt else ""
        lines.append(
            f"| {c.day} | {c.chapter_from} ~ {c.chapter_to} | {c.chars:,} "
            f"| {round(c.chars * TOKENS_PER_CHAR):,} | `{fname}` |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 主流程


@dataclass
class Result:
    book: Book
    chunks: list[Chunk]
    stats: dict
    outdir: Path
    txt_files: list[Path]
    epub_file: Path | None
    index_file: Path


def split_book(
    src: Path,
    outdir: Path | None = None,
    cfg: Config | None = None,
    want_txt: bool = True,
    want_epub: bool = True,
    with_header: bool = True,
    keep_toc: bool = False,
    with_images: bool = True,
    force: bool = False,
    report: bool = True,
) -> Result:
    cfg = cfg or Config()
    if cfg.max_chars < cfg.target:
        raise ValueError(f"上限（{cfg.max_chars}）不能小于目标（{cfg.target}）")

    t_all = time.perf_counter()
    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    book = load_book(src, keep_toc=keep_toc, force=force)
    timings["载入"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    units = build_units(book, cfg)
    scene = scene_scores(book, units)
    timings["结构解析"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    chunks = build_chunks(book, units, plan_chunks(units, scene, cfg))
    timings["DP 切分"] = time.perf_counter() - t0

    outdir = outdir or src.parent / f"{src.stem}_split"
    txt_files: list[Path] = []
    if want_txt:
        t0 = time.perf_counter()
        txt_files = write_txt(outdir / "txt", book, chunks, with_header)
        timings["写出 TXT"] = time.perf_counter() - t0

    epub_file: Path | None = None
    if want_epub:
        t0 = time.perf_counter()
        epub_file = write_epub(outdir / f"{safe_name(book.title)}_split.epub", book, chunks,
                               with_images=with_images)
        timings["写出 EPUB"] = time.perf_counter() - t0

    numbers = [c.chars for c in chunks] or [0]
    stats = {
        "total_chars": sum(numbers),
        "min_chunk": min(numbers),
        "max_chunk": max(numbers),
        "avg_chunk": sum(numbers) / len(numbers),
        "chapters": len(book.chapters),
        "paragraphs": len(book.paras),
        "units": len(units),
        "est_tokens": int(sum(numbers) * TOKENS_PER_CHAR),
        "timings": timings,
        "total_time": 0.0,
    }

    # 先把耗时定下来：清单里要写这个数字，所以把它自己那一步排除在外
    stats["total_time"] = time.perf_counter() - t_all

    t0 = time.perf_counter()
    index_file = write_index(outdir / "清单.md", book, chunks, cfg, stats, has_txt=want_txt)
    timings["写出清单"] = time.perf_counter() - t0

    if report:
        print_report(book, chunks, stats, outdir)
    return Result(book, chunks, stats, outdir, txt_files, epub_file, index_file)


def print_report(book: Book, chunks: list[Chunk], stats: dict, outdir: Path) -> None:
    print(f"\n=== {book.title} ===")
    print(f"来源        {book.source_kind} / {book.encoding}，"
          f"段落 {stats['paragraphs']:,}，章节 {stats['chapters']}，切分单元 {stats['units']:,}")
    for name, sec in stats["timings"].items():
        print(f"{name:<10}{sec:8.3f} s")
    print(f"{'合计':<10}{stats['total_time']:8.3f} s")
    print(f"\n切成 {len(chunks)} 份，正文 {stats['total_chars']:,} 字，"
          f"每份 {stats['min_chunk']:,} ~ {stats['max_chunk']:,} 字（平均 {stats['avg_chunk']:,.0f}）")
    print("LLM/API token 消耗：0（纯本地，未调用任何模型）")
    print(f"估算 token 总量：约 {stats['est_tokens']:,}（按 {TOKENS_PER_CHAR} token/字 粗估，仅供参考）")
    print(f"输出目录：{outdir}")
    if book.warnings:
        print("提醒：")
        for w in book.warnings:
            print(f"  - {w}")


# ---------------------------------------------------------------- CLI


WELCOME = """
╔══════════════════════════════════════════════════════════╗
║            小说按字数切分 · 每天读一份                    ║
╚══════════════════════════════════════════════════════════╝

把小说文件（.txt 或 .epub）拖到本窗口里，或者直接粘贴完整路径，
然后按回车。也可以把书直接拖到「启动.cmd」上。

切出来的结果会放在书所在的位置，文件夹名是「书名_split」。
"""


def ask_for_path() -> Path | None:
    """交互式问用户要文件路径。"""
    print(WELCOME)
    try:
        raw = input("书在哪里？（把文件拖进来即可，直接回车取消）\n> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return None
    raw = raw.strip().strip('"').strip("'").strip()
    if not raw:
        print("没有拿到文件路径，已取消。")
        return None
    path = Path(raw)
    if not path.exists():
        print(f"\n找不到这个文件：{raw}")
        print("提示：路径里如果有空格，整条路径要用英文双引号包起来。")
        return None
    return path


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="把小说按字数切成每天一份（TXT + EPUB）")
    ap.add_argument("inputs", nargs="*", type=Path,
                    help="小说文件（.txt 或 .epub），可以一次给多个；不给就交互式询问")
    ap.add_argument("-o", "--outdir", type=Path, default=None, help="输出目录，默认 <文件名>_split")
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET, help=f"每份目标字数，默认 {DEFAULT_TARGET}")
    ap.add_argument("--max", dest="max_chars", type=int, default=DEFAULT_MAX, help=f"每份上限，默认 {DEFAULT_MAX}")
    ap.add_argument("--min", dest="min_chars", type=int, default=DEFAULT_MIN, help=f"每份软下限，默认 {DEFAULT_MIN}")
    ap.add_argument("--no-txt", action="store_true", help="不输出 txt")
    ap.add_argument("--no-epub", action="store_true", help="不输出 epub")
    ap.add_argument("--no-header", action="store_true", help="txt 里不加天数表头")
    ap.add_argument("--keep-toc", action="store_true", help="保留开头的目录页（默认跳过）")
    ap.add_argument("--no-images", action="store_true", help="epub 里不带原书插图")
    ap.add_argument("--force", action="store_true", help="书有问题（扫描版/太短）时也强行切")
    args = ap.parse_args(argv)

    files = [p for p in args.inputs]
    if not files:
        picked = ask_for_path()
        if picked is None:
            return 2
        files = [picked]

    missing = [p for p in files if not p.exists()]
    if missing:
        for p in missing:
            print(f"找不到文件：{p}", file=sys.stderr)
        return 2

    cfg = Config(target=args.target, max_chars=args.max_chars, min_chars=args.min_chars)
    multi = len(files) > 1
    for src in files:
        outdir = args.outdir
        if outdir is not None and multi:
            outdir = outdir / src.stem
        try:
            split_book(src, outdir=outdir, cfg=cfg,
                       want_txt=not args.no_txt, want_epub=not args.no_epub,
                       with_header=not args.no_header, keep_toc=args.keep_toc,
                       with_images=not args.no_images, force=args.force)
        except BookProblem as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except Exception as exc:
            print(f"出错了：{exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
