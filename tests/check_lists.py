#!/usr/bin/env python3
"""Списки на странице: по 10 строк, поиск на лету (без Enter) и «удалить книгу» / «удалить архив».

Без сети: живой WebApp с настоящей папкой книг и архивов во временном каталоге.
"""
import os
import pathlib
import re
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bot"))

import web as webmod             # noqa: E402
from jobs import Jobs            # noqa: E402

results = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + extra) if extra and not ok else ''}")


work = pathlib.Path(tempfile.mkdtemp(prefix="lists-"))
jobs = Jobs(work / "data", HERE / "pipeline", max_jobs=1, env=dict(os.environ))
notes = []
page = webmod.WebApp(jobs, notify=notes.append)
for number in range(1, 26):
    (jobs.books / f"книга-{number:02d}.epub").write_bytes(b"PK book")
(jobs.books / "Ёлка и ёжик.epub").write_bytes(b"PK book")
for number in range(1, 13):
    (jobs.results / f"архив-{number:02d}.zip").write_bytes(b"PK zip")


def get(path: str) -> str:
    return page.route("GET", path)[2].decode("utf-8", "replace")


def rows(text: str, kind: str) -> list:
    card = text.split(f'id="list-{kind}"', 1)[1].split('<div class="card"', 1)[0]
    return re.findall(r'<tr><td><a href="/(?:file|result)/[^"]+">([^<]+)</a>', card)


main = get("/")
check("на главной книги — по 10 на страницу", len(rows(main, "books")) == 10, str(rows(main, "books")))
check("архивы — тоже по 10", len(rows(main, "results")) == 10)
check("у каждого списка поле поиска, которое ищет без Enter",
      main.count('class="search"') == 3 and "addEventListener('input'" in main)
third = get("/files?bpage=3")
check("третья страница книг — оставшиеся 6", len(rows(third, "books")) == 6, str(rows(third, "books")))
check("листание книг не сбрасывает поиск в архивах",
      'rq=%D0%B0%D1%80%D1%85%D0%B8%D0%B2' in get("/?rq=архив&bpage=1").split('id="list-books"', 1)[1].split("</div></div>")[0]
      or "rq=" in get("/?rq=архив").split('id="list-books"', 1)[1])
found = get("/?bq=книга-07")
check("поиск находит книгу по части названия", rows(found, "books") == ["книга-07.epub"], str(rows(found, "books")))
check("поиск не различает «ё» и «е» и регистр", rows(get("/?bq=ЕЛКА"), "books") == ["Ёлка и ёжик.epub"])
check("ничего не нашлось — так и написано", "Ничего не нашлось." in get("/?bq=нет-такой"))
fragment = get("/fragment/books?bq=книга-1&base=/files")
check("поиск на лету получает только таблицу списка, без шапки страницы",
      '<div class="list-body">' in fragment and "<h1>" not in fragment and len(rows(fragment, "books")) == 10)
paged = get("/fragment/books?bq=книга&base=/files")
check("ссылки страниц из поиска ведут туда же и с тем же запросом",
      'href="/files?bq=%D0%BA%D0%BD%D0%B8%D0%B3%D0%B0&amp;bpage=2"' in paged, paged[paged.find("pager"):][:200])
check("слишком большой номер страницы не даёт пустой список", len(rows(get("/?bpage=99"), "books")) == 6)

code, _, _, headers = page.route("POST", "/book/delete/книга-01.epub?back=/files")
check("кнопка «удалить» убирает книгу из папки", not (jobs.books / "книга-01.epub").exists()
      and code in (302, 303) and "/files" in str(headers), str(headers))
check("и об этом написано в Telegram", any("книга-01.epub" in text and "удалена" in text for text in notes), str(notes))
busy = jobs.add("книга-02.epub", "555", jobs.books / "книга-02.epub")
busy.meta["status"] = "running"
busy.save()
check("у книги в работе вместо «удалить» — «в работе»",
      "в работе" in get("/?bq=книга-02") and "/book/delete/" not in get("/?bq=книга-02").split('id="list-books"', 1)[1].split("</table>")[0])
page.route("POST", "/book/delete/книга-02.epub")
check("книгу в работе удалить нельзя даже прямым запросом", (jobs.books / "книга-02.epub").exists())
page.route("POST", "/result/delete/архив-01.zip")
check("архив тоже удаляется", not (jobs.results / "архив-01.zip").exists())
page.route("POST", "/book/delete/..%2F..%2Fdata%2Fsettings.json")
check("удалить можно только файл из папки, не выше", (work / "data").is_dir())
check("подтверждение перед удалением", "confirm('Удалить книгу" in get("/"))

bad = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)})")
sys.exit(1 if bad else 0)
