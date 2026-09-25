#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""体检 + 验证：看看一本书长什么样、切出来合不合格。只读，不写任何文件。

    python 诊断.py sample\某本书.epub
    python 诊断.py sample\某本书.epub --target 5500 --max 6500

先体检（编码、段落、章节、图片、结构），再逐条验证硬约束：
  1. 任何一份都不超过 --max 字数
  2. 正文零丢失、零重复
  3. 每一份的最后一段都是完整句子（不是图注碎片、不是半个句子）
  4. 每一份都从完整段落起
  5. 章末对齐情况
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import novel_split as ns  # noqa: E402


def lines_of(text: str) -> list[str]:
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def inspect(book: ns.Book, src: Path) -> None:
    print("─" * 60)
    print("体检")
    print("─" * 60)
    print(f"文件      {src.name}")
    print(f"书名      {book.title}")
    print(f"格式      {book.source_kind} / {book.encoding}")
    print(f"段落      {len(book.paras):,}")
    print(f"章节      {len(book.chapters):,}")
    print(f"正文      {ns.count_chars(book.text):,} 字（不计空白）")
    if book.image_files:
        mb = sum(len(v) for v in book.image_files.values()) / 1024 / 1024
        print(f"插图      {len(book.image_files):,} 张（{mb:.1f} MB，只进 EPUB）")

    lens = sorted(ns.count_chars(book.text[p.start:p.end]) for p in book.paras)
    if lens:
        over = [x for x in lens if x > ns.DEFAULT_MAX]
        print(f"段落长度  最少 {lens[0]}，中位 {int(statistics.median(lens))}，"
              f"平均 {int(statistics.mean(lens))}，最长 {lens[-1]}")
        print(f"超长段落  {len(over)} 段" + (f"（最长 {max(over):,} 字，会被迫在段内切）" if over else ""))

    if book.warnings:
        print("\n提醒：")
        for w in book.warnings:
            print(f"  - {w}")

    if book.chapters:
        print("\n章节标题（最多列 15 个）：")
        for c in book.chapters[:15]:
            print(f"  段{c.para_idx:>6}  {c.title}")
        if len(book.chapters) > 15:
            print(f"  …… 还有 {len(book.chapters) - 15} 个")


def verify(book: ns.Book, chunks: list[ns.Chunk], cfg: ns.Config) -> list[str]:
    print()
    print("─" * 60)
    print("验证硬约束")
    print("─" * 60)
    failures: list[str] = []
    sizes = [c.chars for c in chunks]

    over = [c for c in chunks if c.chars > cfg.max_chars]
    print(f"[1] 每份字数     {min(sizes):,} ~ {max(sizes):,}，平均 {statistics.mean(sizes):,.0f}"
          f"（上限 {cfg.max_chars:,}）")
    if over:
        failures.append(f"{len(over)} 份超过上限：" + ", ".join(f"第{c.day}天({c.chars}字)" for c in over))

    want = lines_of(book.text)
    got: list[str] = []
    for c in chunks:
        got.extend(lines_of(c.text))
    if got == want:
        print(f"[2] 正文完整性   通过（{len(want):,} 段，顺序一致，无丢失无重复）")
    else:
        diff = next((i for i, (a, b) in enumerate(zip(want, got)) if a != b), min(len(want), len(got)))
        print(f"[2] 正文完整性   失败，首个差异在第 {diff} 段")
        failures.append("正文对不上")

    bad_tail = [(c.day, lines_of(c.text)[-1]) for c in chunks[:-1]
                if not ns.can_end_unit(lines_of(c.text)[-1])]
    bad_head = [(c.day, lines_of(c.text)[0]) for c in chunks[1:]
                if ns.is_fragment(lines_of(c.text)[0])]

    print(f"[3] 每份结尾      {len(chunks) - 1 - len(bad_tail)}/{len(chunks) - 1} 份是完整句子"
          + (f"，{len(bad_tail)} 份可疑" if bad_tail else "，全部干净"))
    print(f"[4] 每份开头      {len(chunks) - 1 - len(bad_head)}/{len(chunks) - 1} 份从完整段落起"
          + (f"，{len(bad_head)} 份从图注碎片起" if bad_head else "，全部干净"))
    if bad_tail:
        failures.append(f"{len(bad_tail)} 份结尾可疑")

    chapter_titles = {c.title for c in book.chapters}
    aligned = sum(1 for c in chunks[1:] if lines_of(c.text)[0] in chapter_titles)
    print(f"[5] 章末对齐      {aligned}/{len(chunks) - 1} 份正好从某一节的开头开始")

    list_item = re.compile(r"^\d{1,3}[.、)）]")
    suspicious = []
    for c in chunks[:-1]:
        last = lines_of(c.text)[-1]
        if len(last) <= 25 or last.endswith(("：", "，", "、")) or list_item.match(last):
            suspicious.append((c.day, c.chars, last))

    if bad_tail or bad_head or suspicious:
        print("\n需要人眼过一遍的地方：")
        for day, last in bad_tail:
            print(f"  第{day:>2}天 结尾不完整 → {last[-60:]}")
        for day, first in bad_head:
            print(f"  第{day:>2}天 开头是碎片 → {first[:60]}")
        for day, chars, last in suspicious:
            print(f"  第{day:>2}天 结尾偏短（{chars:,}字）→ {last[:60]}")
    return failures


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="体检一本书并验证切分结果（只读）")
    ap.add_argument("input", type=Path)
    ap.add_argument("--target", type=int, default=ns.DEFAULT_TARGET)
    ap.add_argument("--max", dest="max_chars", type=int, default=ns.DEFAULT_MAX)
    ap.add_argument("--min", dest="min_chars", type=int, default=ns.DEFAULT_MIN)
    ap.add_argument("--keep-toc", action="store_true")
    args = ap.parse_args(argv[1:])

    cfg = ns.Config(args.target, args.max_chars, args.min_chars)
    try:
        # 注意：这里刻意不走 split_book()，因为那个函数会往磁盘写产物。
        # 诊断必须只读，所以直接调内部步骤，最后什么都不落盘。
        book = ns.load_book(args.input, keep_toc=args.keep_toc)
        units = ns.build_units(book, cfg)
        chunks = ns.build_chunks(book, units, ns.plan_chunks(units, ns.scene_scores(book, units), cfg))
    except ns.BookProblem as exc:
        print(str(exc), file=sys.stderr)
        return 3

    inspect(book, args.input)
    print(f"\n切成 {len(chunks)} 份")
    failures = verify(book, chunks, cfg)

    print()
    if failures:
        print("FAIL: " + "；".join(failures))
        return 1
    print("PASS: 硬约束全部满足")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
