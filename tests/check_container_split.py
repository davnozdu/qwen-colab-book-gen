#!/usr/bin/env python3
"""Настоящая нарезка книги в контейнере — без заглушек конвейера.

Остальной smoke подменяет shrink_book.py заглушкой (tests/pipeline), поэтому однажды в образ не
попал beautifulsoup4, и режим частей падал на первой же книге «нарезка не удалась», а тесты
были зелёные. Здесь: библиотеки, которые конвейер подключает в контейнере, ffmpeg, и сама
нарезка небольшой книги на две части настоящим скриптом из образа.

Запуск: python3 check_container_split.py <папка конвейера> <рабочая папка>
"""
import importlib
import json
import pathlib
import shutil
import subprocess
import sys

pipeline = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/qwen-epub-audiobook")
work = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/split-check")
results = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + extra) if extra and not ok else ''}")


missing = []
for module in ("bs4", "ebooklib", "lxml", "numpy", "soundfile", "razdel"):
    try:
        importlib.import_module(module)
    except ImportError as exc:
        missing.append(f"{module} ({exc})")
check("библиотеки конвейера для нарезки и сборки есть в образе", not missing, ", ".join(missing))
check("ffmpeg и ffprobe есть (склейка и сборка книги)", bool(shutil.which("ffmpeg") and shutil.which("ffprobe")))

if not missing:
    from ebooklib import epub
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    book = epub.EpubBook()
    book.set_identifier("split-check")
    book.set_title("Проверка нарезки")
    book.set_language("ru")
    book.add_author("Тест")
    chapters = []
    sentence = "Он шёл по тихой дороге и думал о том, что будет завтра утром. "
    for number in range(1, 5):
        chapter = epub.EpubHtml(title=f"Глава {number}", file_name=f"c{number}.xhtml", lang="ru")
        paragraphs = "".join(f"<p>{sentence * 4}</p>" for _ in range(12))
        chapter.content = f"<html><body><h1>Глава {number}</h1>{paragraphs}</body></html>"
        book.add_item(chapter)
        chapters.append(chapter)
    book.toc = chapters
    book.spine = ["nav", *chapters]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    source = work / "Проверка нарезки.epub"
    epub.write_epub(str(source), book)

    out = work / "out"
    done = subprocess.run([sys.executable, str(pipeline / "shrink_book.py"), str(source), "--out", str(out),
                           "--max-hours", "0.05", "--no-drive"],
                          cwd=str(pipeline), capture_output=True, text=True)
    plan_path = out / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
    files = plan.get("files") or []
    check("настоящий shrink_book.py режет книгу на части",
          done.returncode == 0 and len(files) >= 2 and all(pathlib.Path(f["epub"]).is_file() for f in files),
          (done.stdout + done.stderr)[-1500:])

failed = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)}, не прошло: {len(failed)})")
sys.exit(1 if failed else 0)
