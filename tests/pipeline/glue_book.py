#!/usr/bin/env python3
"""Заглушка сборки книги из частей: проверяет, что демон подал ей то, что нужно.

Настоящий glue_book.py складывает части в одну книгу (склейка, громкость по всей книге, музыка,
m4b и read-along) и проверяется отдельно, на настоящем конвейере. Здесь важно другое: что бот
запустил сборку сам, когда все части готовы, отдал ей каталог с частями и сложил архив в результаты.
"""
import argparse, json, pathlib, zipfile

parser = argparse.ArgumentParser()
parser.add_argument("--mode", default="final")
parser.add_argument("--parts-dir", required=True)
parser.add_argument("--part", default="0")
parser.add_argument("--out", required=True)
parser.add_argument("--book", default="")
parser.add_argument("--polish", default="soft")
parser.add_argument("--work", default="")
parser.add_argument("--expect", default="0")
parser.add_argument("--name", default="")
args, _extra = parser.parse_known_args()

parts = sorted(pathlib.Path(args.parts_dir).glob("part-*"))
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
print(f"сборка книги: режим {args.mode}, частей найдено {len(parts)}, ждём {args.expect}", flush=True)
if not parts:
    print("частей нет — собирать нечего", flush=True)
    raise SystemExit(1)
if args.mode == "part":
    index = int(args.part or 0)
    chosen = [item for item in parts if item.name == f"part-{index}"]
    if not chosen:
        print(f"части {index} нет в {args.parts_dir}", flush=True)
        raise SystemExit(1)
    title = f"Книга для теста — часть {index} из {len(parts)}"
    archive = out / f"{args.name or ('часть-' + str(index))}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{title}.m4b", b"part audio")
        zf.writestr(f"{title}-read-along.epub", b"part epub")
    print(f"архив  : {archive}", flush=True)      # бот узнаёт о готовом архиве по пути в журнале
    print(f"готова часть {index}: {archive.name}", flush=True)
    raise SystemExit(0)

if int(args.expect or 0) and len(parts) != int(args.expect):
    print(f"частей {len(parts)}, а ждали {args.expect} — сборку не начинаю", flush=True)
    raise SystemExit(1)
title = "Книга для теста"
archive = out / f"{args.name or 'книга'}.zip"
with zipfile.ZipFile(archive, "w") as zf:
    zf.writestr(f"{title}.m4b", b"whole book audio")
    zf.writestr(f"{title}-read-along.epub", b"whole book epub")
json.dump({"parts": len(parts), "polish": args.polish, "archive": archive.name},
          open(pathlib.Path(args.parts_dir) / "glue.json", "w", encoding="utf-8"), ensure_ascii=False)
print(f"архив  : {archive}", flush=True)          # абсолютный путь: по нему бот видит готовый архив
print(f"книга собрана: {archive.name}", flush=True)
