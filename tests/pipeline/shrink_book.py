#!/usr/bin/env python3
"""Заглушка нарезки книги на части: для сквозного теста режима частей.

Настоящий shrink_book.py читает EPUB (ebooklib) и режет по границам абзацев — в тесте это лишние
минуты и лишние зависимости. Здесь делаем ровно то, что нужно боту: план нарезки и два файла-
части. Книга с «short» в имени остаётся одной частью — так проверяется и случай «резать не надо».
"""
import json
import pathlib
import shutil
import sys

arguments = sys.argv[1:]
book = pathlib.Path(arguments[0])
out = pathlib.Path(arguments[arguments.index("--out") + 1])
out.mkdir(parents=True, exist_ok=True)
title = "Книга для теста"
parts = 1 if "short" in book.name.lower() else 2

print(f"== нарезка (заглушка): {book.name} ==", flush=True)
plan = {"book": str(book), "stem": book.stem, "paragraphs": 10, "segments": 20,
        "estimated_seconds": 1800.0, "estimated_hours": 0.5, "max_hours": 1.5,
        "parts": parts, "out": str(out), "shrink_dir": "Audiobooks-shrink", "files": [], "drive": []}
if parts > 1:
    for number in range(1, parts + 1):
        part = out / f"{book.stem}-part{number}.epub"
        shutil.copyfile(book, part)
        plan["files"].append({"part": f"{title} — часть {number} из {parts}", "epub": str(part),
                              "paragraphs": 5, "segments": 10, "estimated_seconds": 900.0,
                              "estimated_hours": 0.25, "size_bytes": part.stat().st_size,
                              "from_source": True})
        print(f"  часть {number} из {parts}: {part.name}", flush=True)
(out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
print("PLAN_JSON " + json.dumps(plan, ensure_ascii=False), flush=True)
