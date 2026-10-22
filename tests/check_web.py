#!/usr/bin/env python3
"""Проверка веб-страницы: каталог книг, загрузка, статус, скачивание архива, остановка, вход.

Запускается из smoke-теста: WEB_BASE — адрес страницы, WORK — каталог с данными (том).
"""
import json, os, pathlib, socket, sys, time, urllib.error, urllib.parse, urllib.request

BASE = os.environ["WEB_BASE"].rstrip("/")     # адрес через опубликованный порт, не изнутри контейнера
WORK = pathlib.Path(os.environ["WORK"])
results = []
started = time.time()


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + extra) if extra and not ok else ''}")


def get(path: str, timeout: int = 30):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        return 0, str(exc).encode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                      # перенаправление не проходим: нужен код 303 и его заголовки


def post(path: str, data: bytes = b"", content_type: str = "", timeout: int = 200):
    request = urllib.request.Request(BASE + path, data=data)
    if content_type:
        request.add_header("Content-Type", content_type)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers
    except Exception as exc:
        return 0, str(exc).encode(), {}


def multipart_epub(name: str, payload: bytes) -> tuple:
    boundary = "----smoke1234"
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="book"; filename="{name}"\r\n'
            f"Content-Type: application/epub+zip\r\n\r\n").encode() + payload + \
           f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


# ── 1. страница и api ──────────────────────────────────────────────────────
code, page = get("/")
check("страница открывается", code == 200 and b"<h1>" in page, f"код {code}")
check("на странице есть блок входа в Colab", code == 200 and b"Google Colab" in page)

code, payload = get("/api/jobs")
jobs = []
try:
    jobs = json.loads(payload)
except ValueError:
    pass
check("json со списком заданий отдаётся", code == 200 and isinstance(jobs, list), f"код {code}")
check("в заданиях есть уже собранная книга", any("smoke.epub" == item["name"] for item in jobs))

# ── 2. каталог книг и постраничный вывод ───────────────────────────────────
code, listing = get("/files")
check("каталог книг открывается", code == 200 and b"probe-" in listing, f"код {code}")
check("в каталоге видно размер файла", code == 200 and "Б".encode() in listing)
rows = listing.count(b'/file/')                 # все файлы на странице, не только тестовые
check("каталог показывает по 10 записей на страницу", rows == 10, f"строк: {rows}")

code, listing2 = get("/files?bpage=2")
check("вторая страница каталога есть", code == 200 and "страница 2 из".encode() in listing2)
check("пагинация ведёт на /files, а не в никуда (/books)",
      b"/files?bpage=1" in listing2 and b"/books?" not in listing2)

code, file_body = get("/file/probe-00.epub")
check("клик по книге скачивает её", code == 200 and file_body.startswith(b"probe"))

# ── 3. загрузка книги через страницу ───────────────────────────────────────
body, content_type = multipart_epub("web-book.epub", b"PK fake epub from web")
code, _, headers = post("/upload", body, content_type)
location = headers.get("Location", "")
banner = get(location)[1].decode("utf-8", "replace") if code == 303 else ""
check("книга принята через страницу", code == 303 and "web-book" in banner,
      f"код {code}, сообщение: {banner[:80]}")

for _ in range(30):
    time.sleep(2)
    code, payload = get("/api/jobs")
    jobs = json.loads(payload) if code == 200 else []
    if any(item["name"] == "web-book.epub" and item["status"] in ("done", "failed", "stopped")
           for item in jobs):
        break
web_job = next((item for item in jobs if item["name"] == "web-book.epub"), None)
check("загруженная книга доехала до конца", bool(web_job) and web_job["status"] == "done",
      f"статус: {web_job['status'] if web_job else 'нет задания'}")

# ── 3c. «сколько осталось» на странице: та же кнопка, что в Telegram ─────────
if web_job:
    code, page = get(f"/eta/{web_job['session']}")
    check("страница «сколько осталось» открывается",
          code == 200 and "Сколько осталось".encode() in page, f"код {code}")
    check("на ней видно проценты и сегменты",
          code == 200 and ("%".encode() in page and "сегментов".encode() in page))
    code, payload = get(f"/api/eta/{web_job['session']}")
    line = json.loads(payload).get("line", "") if code == 200 else ""
    check("живое обновление прогресса отдаёт строку",
          code == 200 and "%" in line, f"код {code}, строка: {line[:60]}")

# ── 3b. тело запроса больше потолка не читается вовсе (413) ─────────────
host, port = urllib.parse.urlsplit(BASE).hostname, urllib.parse.urlsplit(BASE).port or 80
sock = socket.create_connection((host, port), timeout=15)
sock.sendall(b"POST /upload HTTP/1.1\r\nHost: test\r\n"
             b"Content-Type: multipart/form-data; boundary=x\r\n"
             b"Content-Length: 300000000\r\nConnection: close\r\n\r\n")
reply = b""
while True:
    chunk = sock.recv(4096)
    if not chunk:
        break
    reply += chunk
sock.close()
status_code = int(reply.split(b"\r\n")[0].split()[1]) if reply else 0
check("запрос больше 200 МБ отклоняется сразу (413), тело не читается",
      status_code == 413 and len(reply) < 4096, f"статус {status_code}, байт {len(reply)}")

if web_job:
    code, log = get(f"/log/{web_job['session']}")
    check("журнал задания открывается", code == 200 and "готово".encode() in log)
    check("журнал живой: страница сама подтягивает строки",
          b"/tail?offset=" in log and b"setTimeout(pull" in log)
    # Прокрутка: страница должна показывать ПОСЛЕДНИЕ события. Раньше прокрутку сбивало
    # общее авто-обновление страницы каждые 5 секунд — оно поднимало журнал в начало.
    check("журнал держится за последние события, а не за начало",
          b"toBottom()" in log and b"let follow = true" in log)
    check("живой журнал не перезагружается целиком каждые 5 секунд",
          b"getElementById('log')" in log)
    code, tail = get(f"/log/{web_job['session']}/tail?offset=0")
    payload = json.loads(tail) if code == 200 else {}
    # Первой строкой в журнале теперь идёт строка режима (её пишет бот перед запуском), поэтому
    # проверяем не «начинается с баннера», а что хвост отдаётся и баннер обёртки в нём есть.
    check("хвост журнала отдаётся куском json",
          code == 200 and payload.get("status") == "done"
          and "==" in payload.get("text", ""),
          f"код {code}, статус {payload.get('status')}, начало: {payload.get('text', '')[:40]!r}")
    code, tail2 = get(f"/log/{web_job['session']}/tail?offset={payload.get('offset', 0)}")
    check("новых строк нет — приходит пусто",
          code == 200 and json.loads(tail2).get("text") == "")
    code, archive = get(f"/result/{pathlib.Path(web_job['result']).name}")
    check("архив скачивается со страницы", code == 200 and archive[:2] == b"PK")

# ── 4. остановка задания и вход в Colab ────────────────────────────────────
code, page = get("/colab")
check("на вкладке Google Colab статус входа виден",
      code == 200 and any(marker in page for marker in
                          ("вход есть".encode(), "входа нет".encode(), "неизвестно".encode(),
                           "ждём код".encode())))

code, _, _ = post("/colab/usage")
check("кнопка «проверить / остаток» отвечает", code in (200, 303), f"код {code}")

code, _, _ = post("/login/start")
follow = get("/colab")[1] if code == 303 else b""
check("кнопка «войти» отвечает без падения",
      code == 303 and ("код авторизации".encode() in follow or "уже есть".encode() in follow or "не выдал".encode() in follow))

code, _, _ = post("/login/reset")
check("кнопка «сбросить вход» отвечает", code in (200, 303), f"код {code}")

# Удаление задания со страницы: запись уходит из списка, книга остаётся.
if web_job:
    code, _, _ = post(f"/delete/{web_job['session']}")
    after = json.loads(get("/api/jobs")[1])
    check("задание удаляется со страницы",
          code == 303 and all(item["session"] != web_job["session"] for item in after))
    check("книга при этом осталась в папке",
          any(path.name == "web-book.epub" for path in (WORK / "data" / "books").iterdir()))

# Действия со страницы должны дублироваться в Telegram (в тесте — в журнал заглушки).
sent_path = WORK / "tgstate" / "sent.jsonl"      # журнал заглушки Telegram лежит рядом с данными
sent_text = sent_path.read_text(encoding="utf-8") if sent_path.exists() else ""
check("вход со страницы продублирован в Telegram", "Вход запрошен со страницы" in sent_text)
check("повтор со страницы продублирован в Telegram", "запущено со страницы" in sent_text)
check("загрузка со страницы продублирована в Telegram", "загружено со страницы" in sent_text)

# ── 3д. настройки: режим обработки, порог нарезки, копирование на Drive ────
def post_form(path: str, fields: dict):
    """Форма обычным способом (как браузер): application/x-www-form-urlencoded."""
    return post(path, urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded")


code, page = get("/settings")
check("на вкладке «Настройки» есть разделы настроек",
      code == 200 and "Как обрабатывать книги".encode() in page and "Настройки".encode() in page)

code, _, _ = post_form("/settings", {"mode": "parts", "parts_hours": "2", "drive": "on"})
after = get("/settings")[1]
check("режим переключается на «с разбивкой на части»",
      code == 303 and "режим «с разбивкой на части»".encode() in after, f"код {code}")
check("и страница объясняет, что делает режим частей",
      "Режим частей: книгу длиннее 120 мин".encode() in after
      and 'name="parts_minutes"'.encode() in after
      and "Путь частей ещё собирается".encode() not in after)

code, _, _ = post_form("/settings", {"mode": "book", "parts_hours": "99", "drive": "on"})
after = get("/settings")[1]
check("неправильный порог не применяется — режим остаётся прежним",
      code == 303 and "режим «с разбивкой на части»".encode() in after
      and "не изменены".encode() in after, f"код {code}")

code, _, _ = post_form("/settings", {"mode": "book", "parts_hours": "1.5"})     # флажок снят
after = get("/settings")[1]
check("снятый флажок выключает копирование на Google Drive",
      "Google Drive: нет".encode() in after
      and "режим «одна книга целиком, без нарезки»".encode() in after)
check("настройки легли в том (переживут обновление образа)",
      (WORK / "data" / "settings.json").is_file()
      and json.loads((WORK / "data" / "settings.json").read_text(encoding="utf-8"))["mode"] == "book")
post_form("/settings", {"mode": "book", "parts_hours": "1.5", "drive": "on"})   # вернуть как было

# Журнал заглушки читаем заново: он снят раньше, а настройки сохранялись уже после этого.
fresh_sent = sent_path.read_text(encoding="utf-8") if sent_path.exists() else ""
check("что нажали на странице, ушло в Telegram", "Настройки со страницы" in fresh_sent)

bad = [name for name, ok in results if not ok]

print(f"  (проверок: {len(results)}, время {time.time() - started:.0f} с)")
sys.exit(1 if bad else 0)
