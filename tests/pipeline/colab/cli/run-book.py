#!/usr/bin/env python3
"""Заглушка обёртки run-book.py: печатает стадии, кладёт архив и ссылки на Drive."""
import argparse, os, pathlib, subprocess, sys, time, zipfile

parser = argparse.ArgumentParser()
parser.add_argument("book")
parser.add_argument("--session"); parser.add_argument("--gpu"); parser.add_argument("--drive-dir")
parser.add_argument("--max-hours"); parser.add_argument("--out-dir"); parser.add_argument("--no-caffeinate", action="store_true")
parser.add_argument("--telegram-chat")            # бот обязан передать чат владельца, иначе уведомлений нет
parser.add_argument("--parts-raw", action="store_true")
parser.add_argument("--part-index", default="")
parser.add_argument("--parts-dir", default="")
args, _extra = parser.parse_known_args()


def tell(message: str) -> None:
    """Как настоящая обёртка: пишет в Telegram, если известны ключ и чат."""
    token, base = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_API_BASE")
    if not (token and base and args.telegram_chat):
        return
    import urllib.parse, urllib.request
    data = urllib.parse.urlencode({"chat_id": args.telegram_chat, "text": message}).encode()
    try:
        urllib.request.urlopen(f"{base.rstrip('/')}/bot{token}/sendMessage", data=data, timeout=20).read()
    except Exception:
        pass

book = pathlib.Path(args.book)
out = pathlib.Path(args.out_dir or "/tmp")
out.mkdir(parents=True, exist_ok=True)
work = float(os.environ.get("FAKE_SECONDS", "6"))

# Режим частей: часть идёт только на синтез, сырьё возвращается в том, а часть собирается в образе.
if args.parts_raw:
    print(f"== поднимаю сессию {args.session} ({args.gpu}) для части {args.part_index} ==", flush=True)
    tell(f"🚀 Поднимаю машину Colab ({args.gpu}); часть {args.part_index}: {book.name}")
    print("  режим частей: Google Drive не участвует — сырьё вернётся в том бота", flush=True)
    for step, name in ((5, "EPUB → манифест проекта"), (9, "Синтез речи")):
        print(f"[{step}/12] {name}", flush=True)
        time.sleep(max(1.0, work / 4))
    if not book.is_file():
        print("ошибка: книги нет"); raise SystemExit(1)
    parts_dir = pathlib.Path(args.parts_dir or out)
    part = parts_dir / f"part-{args.part_index}"
    for folder in ("project", "project/source", "project/audio/segments"):
        (part / folder).mkdir(parents=True, exist_ok=True)
    (part / "project" / "manifest.json").write_text("{}", encoding="utf-8")
    (part / "project" / "source.json").write_text('{"paragraphs": []}', encoding="utf-8")
    (part / "project" / "source" / "manifest.json").write_text("{}", encoding="utf-8")
    (part / "project" / "source" / "book-text.epub").write_text("epub", encoding="utf-8")
    (part / "project" / "audio" / "timeline.json").write_text("{}", encoding="utf-8")
    (part / "project" / "audio" / "benchmark.json").write_text("{}", encoding="utf-8")
    (part / "project" / "audio" / "segments" / "s001.flac").write_bytes(b"flac")
    print(f"сырьё части {args.part_index} распаковано: {part}", flush=True)
    tell(f"📦 Часть {args.part_index} озвучена, сырьё забрано в том ({book.stem}-raw.zip)")
    glue = pathlib.Path(__file__).resolve().parent.parent.parent / "glue_book.py"
    command = [sys.executable, str(glue), "--mode", "part", "--part", str(args.part_index),
               "--parts-dir", str(parts_dir), "--out", str(out)]
    print("== собираю часть в образе ==\n  " + " ".join(command), flush=True)
    code = subprocess.run(command).returncode
    if code != 0:
        tell(f"⚠️ Часть {args.part_index} озвучена, но собрать её не вышло")
        raise SystemExit(1)
    tell(f"✅ Часть {args.part_index} «{book.stem}» готова (файлами в архивах)")
    raise SystemExit(0)

# Книга со словом «limit» в имени изображает отказ Colab: так проверяется понятное сообщение
# «все машины заняты, подождите» в Telegram.
if "limit" in book.name:
    print("  машина не выдана: ColabRequestError: Failed to issue request POST "
          "https://colab.research.google.com/tun/m/assign?nbh=abc: Service Unavailable", flush=True)
    print("TooManyAssignmentsError: too many assignments for this user", flush=True)
    raise SystemExit(1)

print(f"== поднимаю сессию {args.session} ({args.gpu}) ==", flush=True)
tell(f"🚀 Поднимаю машину Colab ({args.gpu}); книга: {book.name}")
print("   ссылка авторизации Drive (проверка): https://accounts.google.com/o/oauth2/auth?client_id=stub", flush=True)
for step, name in ((5, "EPUB → манифест проекта"), (6, "Нарезка по предложениям + book-text.epub"), (9, "Синтез речи")):
    print(f"[{step}/12] {name}", flush=True)
    time.sleep(work / 3)
if not book.is_file():
    print("ошибка: книги нет"); raise SystemExit(1)
archive = out / f"{book.stem}.zip"
with zipfile.ZipFile(archive, "w") as zf:
    zf.writestr("book.m4b", b"fake audio")
print(f"на Google Drive: /content/drive/MyDrive/{args.drive_dir}/{book.stem}/{book.stem}.m4b", flush=True)
print("папка: https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456", flush=True)
print(f"• {book.stem}.m4b: https://drive.google.com/file/d/1ZzYyXxWwVvUuTtSsRrQqPpOoNn9876543/view", flush=True)
print(f"• {book.stem}-read-along.epub: https://drive.google.com/file/d/1MmNnOoPpQqRrSsTtUuVvWwXxYyZz1234/view", flush=True)
print(f"готово: {archive}", flush=True)
tell(f"✅ Книга собрана: {book.stem}")
