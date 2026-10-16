#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Веб-интерфейс: состояние сборок, загрузка книги, вход в Colab, файлы и результаты.

Работает в том же процессе, что и Telegram-бот, и через тот же объект Jobs — поэтому состояние
заданий одно и «залипших» статусов из-за двух писателей не бывает. Зависимостей нет: только
стандартная библиотека, образ от этого не толстеет.

Возможности страницы повторяют команды бота: /status /jobs /log /stop /login /usage,
плюс каталог загруженных книг и готовых архивов (клик по файлу — скачивание).
На странице по 20 записей, как в /jobs.

Пароля пока нет (по договорённости) — порт нельзя выставлять в интернет, только локальная сеть.
"""

from __future__ import annotations

import email
import html
import json
import pathlib
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PER_PAGE = 20

STYLE = """
 body { font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 24px;
        background: #14161a; color: #e8e8e8; }
 main { max-width: 980px; margin: 0 auto; }
 h1 { font-size: 20px; margin: 0 0 4px; }
 h2 { font-size: 16px; margin: 0 0 10px; }
 .muted { color: #9aa0a6; }
 .card { background: #1c1f24; border: 1px solid #2a2f36; border-radius: 10px; padding: 16px;
         margin: 16px 0; }
 form.inline { display: inline; }
 input[type=file], input[type=text] { color: #e8e8e8; background: #14161a; border: 1px solid #2a2f36;
         border-radius: 8px; padding: 6px 8px; }
 input[type=file] { border: 0; }
 button { background: #2d6cdf; color: #fff; border: 0; border-radius: 8px; padding: 7px 12px;
          font-size: 14px; cursor: pointer; }
 button.ghost { background: #2a2f36; }
 button[disabled] { opacity: .5; cursor: default; }
 table { width: 100%; border-collapse: collapse; }
 td, th { padding: 8px 6px; border-top: 1px solid #2a2f36; vertical-align: top; text-align: left; }
 td.name { font-weight: 600; }
 a, a:visited { color: #79b8ff; }
 .msg { background: #22303f; border: 1px solid #2d6cdf; border-radius: 8px; padding: 10px 12px;
        margin: 12px 0; }
 .note { color: #f0a35e; }
 .ok { color: #6ee787; }
 .bad { color: #ff7b72; }
 .pager { margin-top: 10px; }
 .pager a, .pager span { margin-right: 10px; }
 code { background: #14161a; padding: 1px 4px; border-radius: 4px; }
"""

SCRIPT = """
// Обновляем страницу, чтобы видеть ход сборки, но не мешаем вводить код авторизации.
setTimeout(function () {
  if (!document.querySelector('input[name=code]')) { location.reload(); }
}, 5000);
"""


def human_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


def human_size(size: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"


def parse_multipart(body: bytes, content_type: str) -> dict:
    """Разбор multipart/form-data без внешних библиотек (модуль cgi в Python 3.13 удалён)."""
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = email.message_from_bytes(header + body)
    fields = {}
    for part in message.walk():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        fields[name] = {"filename": part.get_filename() or "",
                        "data": part.get_payload(decode=True) or b""}
    return fields


def form_fields(content_type: str, body: bytes) -> dict:
    """Поля формы: браузер шлёт и multipart, и application/x-www-form-urlencoded — читаем оба."""
    if "multipart/form-data" in content_type:
        return parse_multipart(body, content_type)
    if "x-www-form-urlencoded" in content_type or not content_type:
        parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        return {key: {"filename": "", "data": value[0].encode()} for key, value in parsed.items()}
    return {}


def safe_name(name: str) -> str:
    return re.sub(r"[^\w\-. ()А-Яа-яЁё]+", "_", urllib.parse.unquote(name)).strip(" .")


class WebApp:
    """Логика страницы: проверяется в тестах без сокетов."""

    def __init__(self, jobs, max_hours: float = 7.0, launch=None, stop=None, login=None,
                 notify=None):
        self.jobs = jobs
        self.max_hours = max_hours
        self.launch_job = launch or (lambda job: None)
        self.stop_job = stop or (lambda job: None)
        # notify — то же самое сообщение уходит в Telegram: что нажали на странице, видно и в чате.
        self.notify = notify or (lambda text: None)
        # login — объект с методами state(), begin(), submit(code), reset(), usage()
        self.login = login
        self.last_message = ""

    # ────────────────────────────── запросы ──────────────────────────────
    def route(self, method: str, path: str, query: dict | None = None,
              content_type: str = "", body: bytes = b"") -> tuple:
        """Возвращает (код, тип содержимого, тело, дополнительные заголовки)."""
        parsed = urllib.parse.urlparse(path)
        query = query or urllib.parse.parse_qs(parsed.query)
        page = _int(query.get("page", ["1"])[0], 1)
        message = query.get("msg", [""])[0]
        if message:
            self.last_message = message
        where = parsed.path.rstrip("/") or "/"

        if method == "GET" and where == "/":
            return self.html(self.shell(self.jobs_page(page)))
        if method == "GET" and where == "/files":
            return self.html(self.shell(self.files_page(page, "books")))
        if method == "GET" and where == "/results":
            return self.html(self.shell(self.files_page(page, "results")))
        if method == "GET" and where == "/api/jobs":
            payload = [{"id": job.id, "session": job.meta["session"], "name": job.meta["name"],
                        "status": job.status, "result": job.meta.get("result", ""),
                        "drive": job.drive_links(), "note": job.meta.get("note", "")}
                       for job in self.jobs.load()]
            return (200, "application/json; charset=utf-8",
                    json.dumps(payload, ensure_ascii=False, indent=2).encode(), {})
        if method == "GET" and where == "/health":
            return 200, "text/plain; charset=utf-8", b"ok\n", {}
        if method == "GET" and parsed.path.endswith("/tail"):
            part = parsed.path.split("/")                 # /log/<сессия>/tail
            job = self.jobs.by_id(part[-2]) if len(part) >= 3 else None
            if job is None:
                return (404, "application/json; charset=utf-8",
                        json.dumps({"text": "", "offset": 0, "status": "нет задания"}).encode(), {})
            payload = job.log_payload(_int(query.get("offset", ["0"])[0], 0))
            return 200, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode(), {}
        if method == "GET" and parsed.path.startswith("/log/"):
            key = parsed.path[len("/log/"):].strip("/")
            if key.endswith("/tail"):
                key = key[:-len("/tail")]
            job = self.jobs.by_id(key)
            if job is None:
                return 200, "text/plain; charset=utf-8", "нет такого задания".encode(), {}
            if "raw" in query or query.get("raw"):
                return 200, "text/plain; charset=utf-8", job.tail(200_000).encode(), {}
            return self.html(self.shell(self.log_page(job)))
        if method == "GET" and parsed.path.startswith("/result/"):
            return self.send_file(self.jobs.results, path_las(parsed.path), "application/zip")
        if method == "GET" and parsed.path.startswith("/file/"):
            return self.send_file(self.jobs.books, path_las(parsed.path), "application/epub+zip")

        if method == "POST" and where == "/upload":
            return self.upload(content_type, body)
        if method == "POST" and parsed.path.startswith("/run/"):
            return self.run_existing(path_las(parsed.path))
        if method == "POST" and parsed.path.startswith("/repeat/"):
            job = self.jobs.by_id(path_las(parsed.path))
            if job is None:
                return self.redirect("/", "Такого задания нет.")
            book = pathlib.Path(job.meta.get("book") or "")
            if not book.is_file():
                return self.redirect("/", f"Файла «{book.name}» больше нет в папке — пришлите книгу заново.")
            return self.run_existing(book.name)
        if method == "POST" and where == "/stop":                    # остановить всё
            jobs = [job for job in self.jobs.load() if job.status in ("running", "queued")]
            for job in jobs:
                self.stop_job(job)
                job.meta.update({"status": "stopped", "finished": time.time(),
                                 "note": "остановлено со страницы"})
                job.save()
            if jobs:
                self.notify(f"⏹ Со страницы остановлено заданий: {len(jobs)}. "
                            f"Машины Colab освобождены.")
                return self.redirect("/", f"Остановлено заданий: {len(jobs)}. "
                                          f"Машины Colab освобождены.")
            return self.redirect("/", "Останавливать нечего: ничего не собирается.")
        if method == "POST" and parsed.path.startswith("/delete/"):
            job = self.jobs.by_id(path_las(parsed.path))
            if job is None:
                return self.redirect("/", "Такого задания уже нет.")
            message = self.jobs.delete(job)
            self.notify(message)
            return self.redirect("/", message)
        if method == "POST" and parsed.path.startswith("/stop/"):
            job = self.jobs.by_id(path_las(parsed.path))
            if job is None:
                return self.redirect("/", "Такого задания нет.")
            self.stop_job(job)
            return self.redirect("/", f"Задание «{job.meta['name']}» остановлено.")
        if method == "POST" and where == "/login/start":
            message = self.login_call("begin")
            self.notify("Вход запрошен со страницы.\n" + message)   # ссылка придёт и в Telegram
            return self.redirect("/", message)
        if method == "POST" and where == "/login/code":
            fields = form_fields(content_type, body)
            code = (fields.get("code", {}).get("data") or b"").decode("utf-8", "replace").strip()
            if not code:
                return self.redirect("/", "Код пустой — скопируйте его со страницы Google и вставьте сюда.")
            message = self.login_call("submit", code)
            self.notify("Код со страницы: " + message)
            return self.redirect("/", message)
        if method == "POST" and where == "/login/reset":
            return self.redirect("/", self.login_call("reset"))
        if method == "POST" and where == "/colab/usage":
            return self.redirect("/", self.login_call("usage"))
        return 404, "text/plain; charset=utf-8", "нет такой страницы".encode(), {}

    def login_call(self, action: str, *args) -> str:
        if self.login is None:
            return "Вход в Colab недоступен в этой сборке."
        try:
            return str(getattr(self.login, action)(*args))
        except Exception as exc:
            return f"Не вышло: {exc}"

    # ────────────────────────────── страницы ──────────────────────────────
    def shell(self, body: str) -> str:
        banner = ""
        if self.last_message:
            banner = f'<div class="msg">{html.escape(self.last_message)}</div>'
            self.last_message = ""
        return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>Сборка аудиокниг</title><style>{STYLE}</style>'
                f'<script>{SCRIPT}</script></head><body><main>'
                f'<h1>Сборка аудиокниг</h1>'
                f'<div class="muted">Книга собирается через Google Colab; готовое уезжает на '
                f'Google Drive и лежит в томе. Страница обновляется сама каждые 5 секунд.</div>'
                f'{banner}{body}'
                f'<div class="muted" style="margin-top:16px">Обновлено {time.strftime("%H:%M:%S")}'
                f' · <a href="/">сборки</a> · <a href="/files">книги</a> · '
                f'<a href="/results">архивы</a> · <a href="/api/jobs">json</a></div>'
                f'</main></body></html>')

    def html(self, page: str) -> tuple:
        return 200, "text/html; charset=utf-8", page.encode(), {}

    def jobs_page(self, page: int) -> str:
        jobs = self.jobs.load()
        running = self.jobs.live_running()
        busy = bool(running)

        hint = ""
        if busy:
            job = running[0]
            minutes = (time.time() - float(job.meta.get("started") or time.time())) / 60
            hint = (f'<div class="note">Сейчас идёт сборка «{html.escape(job.meta["name"])}» '
                    f'({minutes:.0f} мин). Новую книгу можно загрузить после неё или '
                    f'остановить текущую кнопкой ниже.</div>')

        upload = f"""<div class="card"><h2>Загрузить книгу</h2>
<form method="post" action="/upload" enctype="multipart/form-data">
  <input type="file" name="book" accept=".epub" {"disabled" if busy else ""}>
  <button type="submit" {"disabled" if busy else ""}>Загрузить и запустить</button>
</form>
<div class="muted" style="margin-top:8px">Только EPUB. Книги длиннее {self.max_hours:g} ч режутся
на части автоматически. Одна книга за раз.</div>{hint}</div>"""

        rows = [self.job_row(job) for job in page_slice(jobs, page)]
        if not rows:
            rows = ['<tr><td colspan="3" class="muted">Пока ничего не собиралось.</td></tr>']
        stop_all = ""
        if busy:
            stop_all = ('<form class="inline" method="post" action="/stop" '
                        "onsubmit=\"return confirm('Остановить все сборки и освободить машины?')\""
                        '><button class="ghost" type="submit">остановить всё</button></form>')
        table = (f'<div class="card"><h2>Сборки {stop_all}</h2><table>'
                 '<tr><th>Книга</th><th>Состояние</th><th>Действия</th></tr>'
                 + "\n".join(rows) + "</table>"
                 + pager(page, len(jobs), "/") + "</div>")

        return upload + self.colab_card() + table + self.files_card("/files", "Книги в папке", "books") \
            + self.files_card("/results", "Готовые архивы", "results")

    def job_row(self, job) -> str:
        meta = job.meta
        icons = {"done": "✅", "failed": "⚠️", "running": "▶️", "queued": "⏳", "stopped": "⏹"}
        started = meta.get("started") or meta.get("created")
        finished = meta.get("finished") or time.time()
        elapsed = human_delta(finished - float(started or finished))
        tail = ""
        if job.status == "running":
            lines = [line.strip() for line in job.tail(400).splitlines() if line.strip()]
            if lines:
                tail = f'<div class="muted">{html.escape(lines[-1][:140])}</div>'
        note = ""
        if meta.get("note"):
            note = f'<div class="note">{html.escape(str(meta["note"])[:160])}</div>'
        actions = [f'<a href="/log/{job.meta["session"]}" target="_blank">журнал</a>']
        if meta.get("result"):
            actions.append(f'<a href="/result/{urllib.parse.quote(pathlib.Path(meta["result"]).name)}">'
                           f'скачать</a>')
        links = job.drive_links()
        if links:
            folder = next((url for url in links if "/folders/" in url), "")
            if folder:
                actions.append(f'<a href="{html.escape(folder)}" target="_blank">папка Drive</a>')
        if job.status == "running":
            actions.append(f'<form class="inline" method="post" action="/stop/{job.meta["session"]}">'
                           f'<button class="ghost" type="submit">остановить</button></form>')
        else:
            actions.append(f'<form class="inline" method="post" action="/repeat/{job.meta["session"]}">'
                           f'<button class="ghost" type="submit" title="Запустить сборку этой книги заново">'
                           f'повторить</button></form>')
            actions.append(f'<form class="inline" method="post" action="/delete/{job.meta["session"]}" '
                           f"onsubmit=\"return confirm('Убрать это задание из списка?')\""
                           f'><button class="ghost" type="submit" title="Убрать задание из списка '
                           f'(книга и архив останутся)">удалить</button></form>')
        return (f'<tr><td class="name">{html.escape(meta["name"])}</td>'
                f'<td>{icons.get(job.status, "•")} {job.status} · {elapsed}{tail}{note}</td>'
                f'<td>{" · ".join(actions)}</td></tr>')

    def colab_card(self) -> str:
        state = {"signed_in": None, "url": "", "busy": False}
        if self.login is not None:
            try:
                state = getattr(self.login, "state")()
            except Exception as exc:
                state = {"signed_in": None, "url": "", "busy": False, "error": str(exc)}
        if state.get("signed_in") is True:
            badge = '<span class="ok">вход есть</span>'
        elif state.get("busy"):
            badge = '<span class="note">ждём код из браузера — вход ещё не завершён</span>'
        elif state.get("signed_in") is False:
            badge = '<span class="bad">входа нет</span>'
        else:
            badge = '<span class="muted">состояние неизвестно — нажмите «проверить»</span>'

        buttons = ['<form class="inline" method="post" action="/colab/usage">'
                   '<button class="ghost" type="submit">проверить / остаток</button></form>',
                   '<form class="inline" method="post" action="/login/start">'
                   '<button type="submit">войти</button></form>',
                   '<form class="inline" method="post" action="/login/reset">'
                   '<button class="ghost" type="submit">сбросить вход</button></form>']

        code_form = ""
        if state.get("url"):
            code_form = (f'<div style="margin-top:10px">1. Откройте ссылку и разрешите доступ '
                         f'(можно с телефона):<br><a href="{html.escape(state["url"])}" '
                         f'target="_blank">{html.escape(state["url"][:110])}…</a><br>'
                         f'2. Google покажет код — вставьте его сюда:</div>'
                         f'<form method="post" action="/login/code" style="margin-top:8px">'
                         f'<input type="text" name="code" size="40" placeholder="код авторизации"> '
                         f'<button type="submit">войти</button></form>')
        extra = state.get("note") or state.get("error") or ""
        extra_html = (f'<div class="muted" style="margin-top:8px">{html.escape(str(extra))}</div>'
                      if extra else "")
        return (f'<div class="card"><h2>Google Colab</h2>'
                f'<div>Аккаунт: {badge}</div>'
                f'<div class="muted">Вход в Colab тот же, что и в Telegram: подтвердите доступ в '
                f'браузере и пришлите код. «Сбросить вход» откладывает текущий token.json, '
                f'после чего можно войти другим аккаунтом.</div>'
                f'{code_form}'
                f'<div style="margin-top:10px">{" ".join(buttons)}</div>'
                f'{extra_html}'
                f'</div>')

    def log_page(self, job) -> str:
        """Живой журнал: страница сама дотягивает новые строки и обновляет статус."""
        payload = job.log_payload(0)
        status_line = (f'<span id="status">{html.escape(job.status)} · {payload["elapsed"]}</span>')
        return (f'<div class="card"><h2>Журнал: {html.escape(job.meta["name"])}</h2>'
                f'<div class="muted">Состояние: {status_line} · обновляется само, '
                f'перезагружать страницу не нужно. '
                f'<a href="/log/{job.meta["session"]}?raw=1">текстом</a> · '
                f'<a href="/">к сборкам</a></div>'
                f'<pre id="log" style="white-space:pre-wrap;max-height:70vh;overflow:auto;'
                f'background:#0f1114;border:1px solid #2a2f36;border-radius:8px;padding:12px;'
                f'margin-top:10px">{html.escape(payload["text"])}</pre></div>'
                f'<script>'
                f'const pre = document.getElementById("log");'
                f'const st = document.getElementById("status");'
                f'let offset = {payload["offset"]};'
                f'async function pull() {{'
                f'  try {{'
                f'    const r = await fetch("/log/{job.meta["session"]}/tail?offset=" + offset,'
                f' {{cache: "no-store"}});'
                f'    if (r.ok) {{'
                f'      const j = await r.json();'
                f'      if (j.text) {{'
                f'        const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40;'
                f'        pre.textContent += j.text; offset = j.offset;'
                f'        if (stick) {{ pre.scrollTop = pre.scrollHeight; }}'
                f'      }}'
                f'      st.textContent = j.status + " · " + j.elapsed;'
                f'    }}'
                f'  }} catch (e) {{}}'
                f'  setTimeout(pull, 1500);'
                f'}}'
                f'setTimeout(pull, 1000);'
                f'</script>')

    def files_card(self, base: str, title: str, kind: str) -> str:
        folder = self.jobs.books if kind == "books" else self.jobs.results
        files = list_files(folder)
        rows = []
        for path in files[:PER_PAGE]:
            route = "/file/" if kind == "books" else "/result/"
            changed = time.strftime("%d.%m %H:%M", time.localtime(path.stat().st_mtime))
            action = ""
            if kind == "books":
                action = (f'<form class="inline" method="post" action="/run/'
                          f'{urllib.parse.quote(path.name)}">'
                          f'<button class="ghost" type="submit">запустить</button></form>')
            rows.append(f'<tr><td><a href="{route}{urllib.parse.quote(path.name)}">'
                        f'{html.escape(path.name)}</a></td>'
                        f'<td class="muted">{human_size(path.stat().st_size)}</td>'
                        f'<td class="muted">{changed}</td><td>{action}</td></tr>')
        if not rows:
            rows = ['<tr><td colspan="4" class="muted">Папка пуста.</td></tr>']
        more = ""
        if len(files) > PER_PAGE:
            more = f'<div class="muted" style="margin-top:8px">показаны первые {PER_PAGE} из ' \
                   f'{len(files)} — остальные в <a href="{base}">полном списке</a></div>'
        return (f'<div class="card"><h2>{title}</h2><table>'
                f'<tr><th>Файл</th><th>Размер</th><th>Изменён</th><th></th></tr>'
                + "\n".join(rows) + f'</table>{more}</div>')

    def files_page(self, page: int, kind: str) -> str:
        folder = self.jobs.books if kind == "books" else self.jobs.results
        title = "Книги в папке books" if kind == "books" else "Готовые архивы в папке results"
        route = "/file/" if kind == "books" else "/result/"
        files = list_files(folder)
        rows = []
        for path in page_slice(files, page):
            changed = time.strftime("%d.%m %H:%M", time.localtime(path.stat().st_mtime))
            action = ""
            if kind == "books":
                action = (f'<form class="inline" method="post" action="/run/'
                          f'{urllib.parse.quote(path.name)}">'
                          f'<button class="ghost" type="submit">запустить</button></form>')
            rows.append(f'<tr><td><a href="{route}{urllib.parse.quote(path.name)}">'
                        f'{html.escape(path.name)}</a></td>'
                        f'<td class="muted">{human_size(path.stat().st_size)}</td>'
                        f'<td class="muted">{changed}</td><td>{action}</td></tr>')
        if not rows:
            rows = ['<tr><td colspan="4" class="muted">Папка пуста.</td></tr>']
        return (f'<div class="card"><h2>{title}</h2>'
                f'<div class="muted">Всего файлов: {len(files)}. Клик по имени — скачивание.</div>'
                f'<table><tr><th>Файл</th><th>Размер</th><th>Изменён</th></tr>'
                + "\n".join(rows) + "</table>" + pager(page, len(files), f"/{kind}") + "</div>")

    # ────────────────────────────── файлы и действия ──────────────────────────────
    def send_file(self, folder: pathlib.Path, name: str, kind: str) -> tuple:
        target = (folder / safe_name(name)).resolve()
        root = folder.resolve()
        if root != target.parent or not target.is_file():
            return 404, "text/plain; charset=utf-8", "файл не найден".encode(), {}
        return (200, kind, target.read_bytes(),
                {"Content-Disposition": f'attachment; filename="{target.name}"'})

    def run_existing(self, name: str) -> tuple:
        """Запустить сборку книги, которая уже лежит в папке (повтор генерации)."""
        book = self.jobs.books / safe_name(name)
        if not book.is_file():
            return self.redirect("/files", f"Файла «{name}» нет в папке.")
        if self.jobs.live_running():
            return self.redirect("/", "Сейчас идёт сборка — дождитесь её конца или нажмите "
                                       "«остановить всё».")
        job = self.jobs.add(book.name, "web")
        job.meta["book"] = str(book)
        job.save()
        self.launch_job(job)
        self.notify(f"🔁 Повторяю сборку «{book.name}» (запущено со страницы).")
        return self.redirect("/", f"Повторяю сборку «{book.name}» — о ходе напишу.")

    def upload(self, content_type: str, body: bytes) -> tuple:
        fields = parse_multipart(body, content_type) if "multipart/form-data" in content_type else {}
        item = fields.get("book") or {}
        name = item.get("filename") or ""
        data = item.get("data") or b""
        if not name or not data:
            return self.redirect("/", "Файл не пришёл — выберите EPUB и повторите.")
        if not name.lower().endswith(".epub"):
            return self.redirect("/", "Нужен EPUB: конвейер режет и озвучивает именно его.")
        if self.jobs.live_running():
            return self.redirect("/", "Сейчас идёт сборка — дождитесь её конца или остановите.")
        from jobs import safe_stem
        target = self.jobs.books / f"{safe_stem(name)}.epub"
        target.write_bytes(data)
        job = self.jobs.add(name, "web")
        job.meta["book"] = str(target)
        job.save()
        self.launch_job(job)
        self.notify(f"📖 Принял «{name}» ({len(data) / 1e6:.1f} МБ) — загружено со страницы, "
                    f"сборка пошла.")
        return self.redirect("/", f"Принял «{name}» ({len(data) / 1e6:.1f} МБ) — сборка пошла.")

    def redirect(self, location: str, message: str) -> tuple:
        self.last_message = message
        page = (f'<!doctype html><meta charset="utf-8"><p>{html.escape(message)}</p>'
                f'<p><a href="{location}">вернуться</a></p>')
        return (303, "text/html; charset=utf-8", page.encode(),
                {"Location": f"{location}?msg={urllib.parse.quote(message)}"})


def path_las(path: str) -> str:
    return urllib.parse.unquote(path.rsplit("/", 1)[-1])


def list_files(folder: pathlib.Path) -> list:
    """Файлы папки, свежие сверху (папки не показываем)."""
    if not folder.is_dir():
        return []
    return sorted((path for path in folder.iterdir() if path.is_file()),
                  key=lambda path: path.stat().st_mtime, reverse=True)


def page_slice(items: list, page: int) -> list:
    page = max(1, page)
    return items[(page - 1) * PER_PAGE: page * PER_PAGE]


def pager(page: int, total: int, base: str) -> str:
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    if pages == 1:
        return f'<div class="pager muted">всего записей: {total}</div>'
    parts = []
    if page > 1:
        parts.append(f'<a href="{base}?page={page - 1}">← новее</a>')
    parts.append(f"<span>страница {page} из {pages}</span>")
    if page < pages:
        parts.append(f'<a href="{base}?page={page + 1}">старее →</a>')
    return f'<div class="pager">{"".join(parts)} · всего записей: {total} (по {PER_PAGE} на страницу)</div>'


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app: WebApp = None                                    # подставляется при создании сервера

    def log_message(self, *args):
        pass                                             # журнал доступа не нужен

    def _respond(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            code, kind, payload, headers = self.app.route(
                method, self.path, {}, self.headers.get("Content-Type", ""), body)
        except Exception as exc:                          # одна ошибка не должна ронять страницу
            code, kind, payload, headers = 500, "text/plain; charset=utf-8", str(exc).encode(), {}
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):
        self._respond("GET")

    def do_POST(self):
        self._respond("POST")


def start_server(app: WebApp, host: str = "0.0.0.0", port: int = 8080):
    """Поднимает страницу в отдельном потоке и возвращает (сервер, поток)."""
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="web", daemon=True)
    thread.start()
    return server, thread
