#!/usr/bin/env python3
"""Заглушка обёртки run-book.py: печатает стадии, кладёт архив и ссылки на Drive."""
import argparse, os, pathlib, time, zipfile

parser = argparse.ArgumentParser()
parser.add_argument("book")
parser.add_argument("--session"); parser.add_argument("--gpu"); parser.add_argument("--drive-dir")
parser.add_argument("--max-hours"); parser.add_argument("--out-dir"); parser.add_argument("--no-caffeinate", action="store_true")
args, _extra = parser.parse_known_args()

book = pathlib.Path(args.book)
out = pathlib.Path(args.out_dir or "/tmp")
out.mkdir(parents=True, exist_ok=True)
work = float(os.environ.get("FAKE_SECONDS", "6"))

# Книга со словом «limit» в имени изображает отказ Colab: так проверяется понятное сообщение
# «все машины заняты, подождите» в Telegram.
if "limit" in book.name:
    print("  машина не выдана: ColabRequestError: Failed to issue request POST "
          "https://colab.research.google.com/tun/m/assign?nbh=abc: Service Unavailable", flush=True)
    print("TooManyAssignmentsError: too many assignments for this user", flush=True)
    raise SystemExit(1)

print(f"== поднимаю сессию {args.session} ({args.gpu}) ==", flush=True)
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
print("папка: https://drive.google.com/drive/folders/1TestFolderId", flush=True)
print(f"• {book.stem}.m4b: https://drive.google.com/file/d/1TestM4bId/view", flush=True)
print(f"• {book.stem}-read-along.epub: https://drive.google.com/file/d/1TestEpubId/view", flush=True)
print(f"готово: {archive}", flush=True)
