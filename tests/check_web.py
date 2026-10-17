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
check("каталог показывает не больше 20 записей", rows == 20, f"строк: {rows}")

code, listing2 = get("/files?page=2")
check("вторая страница каталога есть", code == 200 and "страница 2 из 2".encode() in listing2)
check("пагинация ведёт на /files, а не в никуда (/books)",
      b"/files?page=1" in listing2 and b"/books?page=" not in listing2)

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
    code, tail = get(f"/log/{web_job['session']}/tail?offset=0")
    payload = json.loads(tail) if code == 200 else {}
    check("хвост журнала отдаётся куском json",
          code == 200 and payload.get("text", "").startswith("==") and payload.get("status") == "done",
          f"код {code}, статус {payload.get('status')}")
    code, tail2 = get(f"/log/{web_job['session']}/tail?offset={payload.get('offset', 0)}")
    check("новых строк нет — приходит пусто",
          code == 200 and json.loads(tail2).get("text") == "")
    code, archive = get(f"/result/{pathlib.Path(web_job['result']).name}")
    check("архив скачивается со страницы", code == 200 and archive[:2] == b"PK")

# ── 4. остановка задания и вход в Colab ────────────────────────────────────
code, page = get("/")
check("на странице статус входа в Colab виден",
      code == 200 and any(marker in page for marker in
                          ("вход есть".encode(), "входа нет".encode(), "неизвестно".encode(),
                           "ждём код".encode())))

code, _, _ = post("/colab/usage")
check("кнопка «проверить / остаток» отвечает", code in (200, 303), f"код {code}")

code, _, _ = post("/login/start")
follow = get("/")[1] if code == 303 else b""
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

bad = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)}, время {time.time() - started:.0f} с)")
sys.exit(1 if bad else 0)
