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
import shutil
import json
import os
import pathlib
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import settings as bot_settings

PER_PAGE = 10                      # строк на страницу во всех списках
MAX_UPLOAD = 200 * 1024 * 1024        # потолок книги со страницы: том не должен умереть от одного файла

# Части книги на странице: тот же набор значков, что в боте, чтобы не искать смысл в словах.
CHAIN_ICONS = {"queued": "⏳", "running": "▶️", "done": "✅", "failed": "⚠️",
               "held": "⏸", "stopped": "⏹"}
CHAIN_STATUS = {"queued": "в очереди", "running": "идёт", "done": "готово", "failed": "сбой",
                "held": "ждёт вашего решения", "stopped": "остановлено"}

STYLE = """
 body { font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 24px;
        background: #14161a; color: #e8e8e8; }
 main { max-width: 980px; margin: 0 auto; }
 input.search { box-sizing: border-box; background: #0f1114; color: #e8e8e8; border: 1px solid #3a3f47;
                border-radius: 6px; padding: 7px 10px; font: inherit; }
 input.search:focus { outline: none; border-color: #6b8afd; }
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
// Обновляем страницу, чтобы видеть ход сборки, но не мешаем вводить код авторизации, не
// перезагружаем живой журнал (у него своя подгрузка) и не сбиваем поиск, пока его набирают.
let typedAt = 0;
setInterval(function () {
  const busy = document.querySelector('input[name=code]') || document.getElementById('log');
  const active = document.activeElement;
  const typing = (active && active.classList && active.classList.contains('search'))
                 || Date.now() - typedAt < 10000;
  if (!busy && !typing) { location.reload(); }
}, 5000);
// Поиск без Enter: через четверть секунды после последней буквы запрашиваем только таблицу
// списка и подменяем её — поле ввода остаётся тем же, курсор и фокус не прыгают. Запрос кладём
// в адрес страницы: автообновление и ссылки на страницы списка его сохраняют.
document.addEventListener('input', function (event) {
  const box = event.target;
  if (!box.classList || !box.classList.contains('search')) { return; }
  typedAt = Date.now();
  clearTimeout(box.pending);
  box.pending = setTimeout(function () {
    const url = new URL(location.href);
    if (box.value.trim()) { url.searchParams.set(box.name, box.value.trim()); }
    else { url.searchParams.delete(box.name); }
    url.searchParams.delete(box.dataset.page);
    url.searchParams.delete('msg');
    history.replaceState(null, '', url);
    const query = new URLSearchParams(url.search);
    query.set('base', location.pathname);
    fetch('/fragment/' + box.dataset.list + '?' + query.toString())
      .then(function (answer) { return answer.text(); })
      .then(function (text) {
        const fresh = new DOMParser().parseFromString(text, 'text/html').querySelector('.list-body');
        const card = document.getElementById('list-' + box.dataset.list);
        if (fresh && card) { card.querySelector('.list-body').replaceWith(fresh); }
      });
  }, 250);
});
"""


def human_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


def progress_line(state: dict) -> str:
    """Строка прогресса для страницы — та же, что бот показывает в Telegram."""
    if not state.get("stage_index"):
        return "стадия ещё не началась"
    done, segments = state.get("done"), state.get("segments")
    bar = "▰" * round(state["percent"] / 10) + "▱" * (10 - round(state["percent"] / 10))
    text = (f"{bar} {state['percent']} % · стадия {state['stage_index']}/{state['stages']}: "
            f"{state['stage_title']}")
    if done and segments:
        text += f" · сегментов {done}/{segments} ({round(100 * done / segments)} %)"
    if state.get("measured_rate"):
        text += f" · {state['measured_rate']} сегм/мин"
    if state.get("remaining_minutes"):
        text += f" · осталось ≈{state['remaining']}, около {state['eta']}"
    if state.get("part") and state["part"].get("total", 1) > 1:
        part = state["part"]
        where = (f"часть {part['index']} из {part['total']}" if part.get("index")
                 else f"готово частей: {part.get('done', 0)} из {part['total']}")
        text += f" · книга: {where}"
    return text


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
                 notify=None, settings=None, chains=None, continue_chain=None, glue=None,
                 ask_split=None, choose_split=None, retry_part=None, profiles=None,
                 profile_action=None, resume=None, profile_label=None, voice_packs=None,
                 refresh_models=None, restart_book=None, choose_voices=None, delete_voices=None):
        self.jobs = jobs
        self.max_hours = max_hours
        self.launch_job = launch or (lambda job: None)
        self.stop_job = stop or (lambda job: None)
        # notify — то же самое сообщение уходит в Telegram: что нажали на странице, видно и в чате.
        self.notify = notify or (lambda text: None)
        # login — объект с методами state(), begin(), submit(code), reset(), usage()
        self.login = login
        # Настройки (режим обработки, порог нарезки, копирование на Drive) лежат в томе и меняются
        # на этой же странице. Объект общий с ботом, поэтому страница и бот видят одни значения.
        self.settings = settings
        # Цепочки частей: страница показывает части книги и кнопку «продолжить генерацию».
        self.chains = chains
        self.continue_chain = continue_chain or (lambda chain_id: "Продолжение недоступно.")
        self.glue = glue or (lambda chain_id: "Сборка книги недоступна.")
        # Длинная книга в режиме частей: «резать или одним файлом» — тот же вопрос, что в Telegram.
        self.ask_split = ask_split or (lambda job, book: False)
        self.choose_split = choose_split or (lambda job_id, cut: "Выбор недоступен в этой сборке.")
        # «Повторить» у части книги перезапускает её в своей цепочке, а не новой книгой.
        self.retry_part = retry_part or (lambda job_id: "Повтор части недоступен в этой сборке.")
        # Профили Google Colab: список и состояние — объект Profiles, действия — те же, что у кнопок
        # в Telegram (войти, выбрать, выйти, удалить, добавить, выйти из всех).
        self.profiles = profiles
        self.profile_action = profile_action or (lambda action, number=0: "Профили недоступны в этой сборке.")
        self.profile_label = profile_label or (lambda number=0: "")
        # Многоголосая книга: пакеты голосов (послушать образцы) и «считать модели» LLM.
        self.voice_packs = voice_packs
        self.refresh_models = refresh_models or (lambda: "Список моделей недоступен в этой сборке.")
        # «Вся книга заново» для книги по частям: новая нарезка и все части с нуля.
        self.restart_book = restart_book or (lambda chain_id, chat_id="": "Недоступно в этой сборке.")
        # Повторная сборка с готовыми голосами: «старые голоса / с нуля»; и «удалить голоса».
        self.choose_voices = choose_voices or (lambda job_id, keep, why="": "Недоступно в этой сборке.")
        self.delete_voices = delete_voices or (lambda key: "Недоступно в этой сборке.")
        # «Продолжить» книгу на паузе: круг по профилям заново.
        self.resume = resume or (lambda job_id: "Продолжение недоступно в этой сборке.")
        self.last_message = ""

    # ────────────────────────────── запросы ──────────────────────────────
    def route(self, method: str, path: str, query: dict | None = None,
              content_type: str = "", body: bytes = b"") -> tuple:
        """Возвращает (код, тип содержимого, тело, дополнительные заголовки)."""
        parsed = urllib.parse.urlparse(path)
        query = query or urllib.parse.parse_qs(parsed.query)
        page = _int(query.get("page", ["1"])[0], 1)
        self.query = query
        message = query.get("msg", [""])[0]
        if message:
            self.last_message = message
        where = parsed.path.rstrip("/") or "/"

        if method == "GET" and where == "/":
            return self.html(self.shell(self.jobs_page(page)))
        if method == "GET" and where == "/files":
            return self.html(self.shell(self.files_card("books", "/files")))
        if method == "GET" and where == "/results":
            return self.html(self.shell(self.files_card("results", "/results")))
        if method == "GET" and parsed.path.startswith("/fragment/"):
            # Только таблица списка — для поиска на лету (страницу целиком не перерисовываем).
            kind = path_las(parsed.path)
            base = (query.get("base") or ["/"])[0]
            base = base if base in ("/", "/files", "/results") else "/"
            if kind == "jobs":
                return self.html(self.jobs_card())
            if kind in ("books", "results"):
                return self.html(self.files_card(kind, base))
            return 404, "text/plain; charset=utf-8", "нет такого списка".encode(), {}
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
        if method == "GET" and parsed.path.startswith("/api/eta/"):
            job = self.jobs.by_id(parsed.path[len("/api/eta/"):].strip("/"))
            line = progress_line(job.progress()) if job else "нет такого задания"
            return (200, "application/json; charset=utf-8",
                    json.dumps({"line": line}, ensure_ascii=False).encode(), {})
        if method == "GET" and parsed.path.startswith("/eta/"):
            job = self.jobs.by_id(parsed.path[len("/eta/"):].strip("/"))
            if job is None:
                return 200, "text/plain; charset=utf-8", "нет такого задания".encode(), {}
            return self.html(self.shell(self.eta_page(job)))
        if method == "GET" and parsed.path.startswith("/log/"):
            job = self.jobs.by_id(parsed.path[len("/log/"):].strip("/"))
            if job is None:
                return 200, "text/plain; charset=utf-8", "нет такого задания".encode(), {}
            if "raw" in query or query.get("raw"):
                return 200, "text/plain; charset=utf-8", job.tail(200_000).encode(), {}
            return self.html(self.shell(self.log_page(job)))
        if method == "GET" and parsed.path.startswith("/voice/"):
            pieces = parsed.path.strip("/").split("/")
            sample = (self.voice_packs.sample(pieces[1], pieces[2].removesuffix(".wav"))
                      if self.voice_packs is not None and len(pieces) == 3 else None)
            if sample is None:
                return 404, "text/plain; charset=utf-8", "образца нет".encode(), {}
            return 200, "audio/wav", sample, {}
        if method == "POST" and where == "/llm/models":
            message = self.refresh_models()
            self.notify("Модели LLM со страницы: " + message)
            return self.redirect("/", message)
        if method == "GET" and parsed.path.startswith("/result/"):
            return self.send_file(self.jobs.results, path_las(parsed.path), "application/zip")
        if method == "GET" and parsed.path.startswith("/file/"):
            return self.send_file(self.jobs.books, path_las(parsed.path), "application/epub+zip")

        if method == "POST" and where == "/upload":
            return self.upload(content_type, body)
        if method == "POST" and parsed.path.startswith(("/book/delete/", "/result/delete/")):
            kind = "books" if parsed.path.startswith("/book/") else "results"
            message = self.delete_file(kind, path_las(parsed.path))
            self.notify("Со страницы: " + message)
            back = (query.get("back") or ["/"])[0]
            return self.redirect(back if back in ("/", "/files", "/results") else "/", message)
        if method == "POST" and parsed.path.startswith("/run/"):
            return self.run_existing(path_las(parsed.path))
        if method == "POST" and parsed.path.startswith("/repeat/"):
            job = self.jobs.by_id(path_las(parsed.path))
            if job is None:
                return self.redirect("/", "Такого задания нет.")
            if job.meta.get("chain") and job.meta.get("part_index"):
                message = self.retry_part(job.id)
                self.notify("Повтор части со страницы. " + message)
                return self.redirect("/", message)
            if job.meta.get("chain"):                      # книга по частям — вся заново
                message = self.restart_book(job.meta["chain"])
                self.notify("Со страницы: " + message)
                return self.redirect("/", message)
            book = pathlib.Path(job.meta.get("book") or "")
            if not book.is_file():
                return self.redirect("/", f"Файла «{book.name}» больше нет в папке — пришлите книгу заново.")
            return self.run_existing(book.name)
        if method == "POST" and parsed.path.startswith("/choice/"):
            # /choice/<задание>/cut|whole — ответ на «резать ли длинную книгу»
            pieces = parsed.path.strip("/").split("/")
            if len(pieces) != 3 or pieces[2] not in ("cut", "whole"):
                return self.redirect("/", "Непонятный выбор: резать (cut) или одним файлом (whole).")
            message = self.choose_split(urllib.parse.unquote(pieces[1]), pieces[2] == "cut")
            self.notify("Выбор со страницы. " + message)
            return self.redirect("/", message)
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
        if method == "POST" and parsed.path.startswith("/profile/"):
            # /profile/<действие>/<номер>: login, activate, logout, delete; add и logout_all — без номера
            pieces = parsed.path.strip("/").split("/")
            action = pieces[1] if len(pieces) > 1 else ""
            number = int(pieces[2]) if len(pieces) > 2 and pieces[2].isdigit() else 0
            if action not in ("login", "activate", "logout", "delete", "add", "logout_all"):
                return self.redirect("/", "Неизвестное действие с профилем.")
            try:
                message = str(self.profile_action(action, number))
            except Exception as exc:
                message = f"Не вышло: {exc}"
            self.notify(f"Профили со страницы: {message}")
            return self.redirect("/", message)
        if method == "POST" and parsed.path.startswith("/resume/"):
            message = self.resume(path_las(parsed.path))
            self.notify("Со страницы: " + message)
            return self.redirect("/", message)
        if method == "POST" and where == "/login/reset":
            return self.redirect("/", self.login_call("reset"))
        if method == "POST" and parsed.path.startswith("/chain/final/"):
            chain = urllib.parse.unquote(parsed.path.split("/chain/final/", 1)[1])
            message = self.glue(chain)
            self.last_message = message
            self.notify(f"Сборка книги со страницы: {message}")
            return self.redirect("/", message)
        if method == "POST" and parsed.path.startswith("/voices/choice/"):
            pieces = parsed.path.strip("/").split("/")
            job = self.jobs.by_id(pieces[2]) if len(pieces) == 4 else None
            if job is None or pieces[3] not in ("keep", "new"):
                return self.redirect("/", "Такого задания нет.")
            message = self.choose_voices(job.id, pieces[3] == "keep")
            self.notify("Со страницы: " + message)
            return self.redirect("/", message)
        if method == "POST" and parsed.path.startswith("/voices/delete/"):
            message = self.delete_voices(path_las(parsed.path))
            self.notify("Со страницы: " + message)
            return self.redirect("/", message)
        if method == "POST" and parsed.path.startswith("/chain/restart/"):
            message = self.restart_book(path_las(parsed.path))
            self.notify("Со страницы: " + message)
            return self.redirect("/", message)
        if method == "POST" and parsed.path.startswith("/chain/continue/"):
            chain = path_las(parsed.path)
            message = self.continue_chain(chain)
            self.notify("Продолжение частей со страницы. " + message)
            return self.redirect("/", message)
        if method == "POST" and where == "/settings":
            if self.settings is None:
                return self.redirect("/", "Настройки недоступны в этой сборке.")
            fields = form_fields(content_type, body)
            raw = {name: (field.get("data") or b"").decode("utf-8", "replace").strip()
                   for name, field in fields.items()}
            # Флажок: когда он снят, в форме его просто нет. Поэтому «нет» передаём явно —
            # иначе сохранение тихо вернуло бы копирование на Drive обратно.
            raw["drive"] = "on" if "drive" in fields else "off"
            message = self.settings.update(raw)
            self.notify("Настройки со страницы. " + message)
            return self.redirect("/", message)
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
<div class="muted" style="margin-top:8px">Только EPUB. {self.mode_hint()} Одна книга за раз.</div>{hint}</div>"""

        return upload + self.chains_card() + self.voices_card() + self.settings_card() + self.colab_card() \
            + self.jobs_card() + self.files_card("books", "/") + self.files_card("results", "/")

    def param(self, name: str) -> str:
        return ((getattr(self, "query", None) or {}).get(name) or [""])[0].strip()

    def jobs_card(self) -> str:
        """Сборки: по 10 на страницу и поиск по названию на лету."""
        search = self.param("q")
        jobs = [job for job in self.jobs.load() if matches(job.meta.get("name") or "", search)]
        page = clamp_page(_int(self.param("page") or "1", 1), len(jobs))
        rows = [self.job_row(job) for job in page_slice(jobs, page)]
        if not rows:
            rows = [f'<tr><td colspan="3" class="muted">'
                    f'{"Ничего не нашлось." if search else "Пока ничего не собиралось."}</td></tr>']
        stop_all = ""
        if self.jobs.live_running():
            stop_all = ('<form class="inline" method="post" action="/stop" '
                        "onsubmit=\"return confirm('Остановить все сборки и освободить машины?')\""
                        '><button class="ghost" type="submit">остановить всё</button></form>')
        return (f'<div class="card" id="list-jobs"><h2>Сборки {stop_all}</h2>'
                + search_box("jobs", "q", "page", search, "поиск по названию книги")
                + '<div class="list-body"><table>'
                '<tr><th>Книга</th><th>Состояние</th><th>Действия</th></tr>'
                + "\n".join(rows) + "</table>"
                + pager(page, len(jobs), "/", "page", self.query_rest("page")) + "</div></div>")

    def query_rest(self, *drop) -> dict:
        """Остальные параметры адреса — чтобы листание одного списка не сбрасывало другие."""
        keep = {}
        for name, values in (getattr(self, "query", None) or {}).items():
            if name not in drop and name not in ("msg", "base") and values and values[0]:
                keep[name] = values[0]
        return keep

    def chains_card(self) -> str:
        """Книги, разрезанные на части: что готово, что идёт, что упало — и кнопка «продолжить»."""
        if self.chains is None:
            return ""
        cards = []
        # Последние пять книг, по одной плашке на книгу: упавшая и начатая заново книга не висит
        # второй плашкой, а к прежним книгам можно вернуться.
        for chain in self.chains.recent(5):
            rows = []
            for part in chain.parts:
                job = self.jobs.by_id(part.get("job_id") or "")
                status = part.get("status") or "queued"
                actions = []
                if job is not None:
                    actions.append(f'<a href="/log/{job.meta["session"]}">журнал</a>')
                    if part.get("result"):
                        actions.append('<a href="/result/'
                                       + urllib.parse.quote(pathlib.Path(part["result"]).name)
                                       + '">скачать</a>')
                    if status == "failed":
                        actions.append('<form class="inline" method="post" action="/repeat/'
                                       + job.meta["session"] + '"><button class="ghost" type="submit" '
                                       'title="Повторить именно эту часть">повторить часть</button></form>')
                rows.append(f'<tr><td>{CHAIN_ICONS.get(status, "•")} '
                            f'{html.escape(chain.title_of(part))}</td>'
                            f'<td>{CHAIN_STATUS.get(status, status)}</td>'
                            f'<td>{"".join(actions)}</td></tr>')
            if not rows:
                rows.append('<tr><td class="muted">нарезка идёт…</td><td></td><td></td></tr>')
            note = ""
            if chain.meta.get("note"):
                note = f'<div class="note" style="margin-top:8px">{html.escape(chain.meta["note"])}</div>'
            resume = ('<form class="inline" method="post" action="/chain/restart/' + chain.id + '" '
                      "onsubmit=\"return confirm('Начать всю книгу заново: новая нарезка и все части?')\""
                      '><button class="ghost" type="submit" title="Остановить всё по книге и озвучить её '
                      'целиком заново">вся книга заново</button></form>')
            if chain.status != "done":
                resume += ('<form class="inline" method="post" action="/chain/continue/'
                           + chain.id + '"><button type="submit" title="Продолжить с первой неготовой '
                          'части: готовые не трогаем">продолжить генерацию</button></form>')
                ready = [part for part in chain.parts if part.get("status") == "done"]
                if chain.parts and len(ready) == len(chain.parts) and chain.status != "gluing":
                    # Все части на месте → финал запускается сам; кнопка нужна, если он не дошёл
                    # (например, контейнер перезапускали ровно в этот момент).
                    resume += ('<form class="inline" method="post" action="/chain/final/'
                               + chain.id + '"><button type="submit" title="Склейка, громкость по всей '
                               'книге, музыка и сборка — здесь, в контейнере">собрать книгу</button></form>')
            cards.append(f'<div class="card"><h2>Книга по частям: '
                         f'{html.escape(str(chain.meta.get("name") or ""))}</h2>'
                         f'<div class="muted">{html.escape(chain.summary())} · порог '
                         f'{float(chain.meta.get("parts_hours") or 0):g} ч · частей в томе: '
                         f'{len(chain.parts)}</div>'
                         f'<table style="margin-top:8px">'
                         f'<tr><th>Часть</th><th>Состояние</th><th>Действия</th></tr>'
                         f'{"".join(rows)}</table>{resume}{note}</div>')
        return "".join(cards)

    def voices_fields(self) -> str:
        """Многоголосая озвучка: режим, поставщик LLM, ключи (показываем только хвост) и модель."""
        import llm
        settings = self.settings
        voices = "".join(f'<option value="{k}"{" selected" if k == settings.voices else ""}>{html.escape(v)}</option>'
                         for k, v in bot_settings.VOICES.items())
        providers = "".join(f'<option value="{k}"{" selected" if k == settings.llm_provider else ""}>'
                            f'{html.escape(v)}</option>' for k, v in llm.PROVIDERS.items())
        names = settings.models()
        current = settings.llm_model
        if current not in names:
            names = [current] + names
        models = "".join(f'<option value="{html.escape(n)}"{" selected" if n == current else ""}>{html.escape(n)}'
                         f'</option>' for n in names)
        keys = "".join(
            f'<div style="margin-top:6px"><label>Ключ {html.escape(title)}</label> '
            f'<input type="password" name="{provider}_key" autocomplete="off" style="width:18em" '
            f'placeholder="{html.escape(llm.masked(settings.values.get(provider + "_key", "")) or "не задан")}"> '
            f'<span class="muted">(пусто — оставить как есть)</span></div>'
            for provider, title in llm.PROVIDERS.items())
        return (f'<div style="margin-top:12px"><label>Озвучка</label><br><select name="voices">{voices}</select> '
                f'<span class="muted">многоголосая: первая машина размечает роли через LLM и создаёт голоса '
                f'героев, вторая озвучивает; рассказчик — прежний; без ключа LLM — один голос</span></div>'
                f'<div style="margin-top:6px"><label>Поставщик LLM</label> <select name="llm_provider">{providers}'
                f'</select> <label>модель</label> <select name="llm_model">{models}</select></div>{keys}')

    def voices_card(self) -> str:
        """Голоса последних многоголосых книг: герои и их образцы — послушать прямо здесь."""
        if self.voice_packs is None:
            return ""
        cards = []
        for pack in self.voice_packs.recent(3):
            rows = []
            for voice in pack["voices"]:
                if voice["fixed"]:
                    rows.append(f'<tr><td class="name">{html.escape(voice["name"])}</td><td>{voice["lines"]}</td>'
                                f'<td class="muted">вшитый образец рассказчика</td></tr>')
                    continue
                player = (f'<audio controls preload="none" src="/voice/{pack["key"]}/{voice["id"]}.wav"></audio>'
                          if voice["sample"] else '<span class="muted">образца нет</span>')
                rows.append(f'<tr><td class="name">{html.escape(voice["name"])}'
                            f'<div class="muted">{html.escape(voice["about"])}</div></td>'
                            f'<td>{voice["lines"]}</td><td>{player}</td></tr>')
            llm_line = " · ".join(str(v) for v in (pack["llm"].get("provider"), pack["llm"].get("model")) if v)
            remove = (f'<form class="inline" method="post" action="/voices/delete/{pack["key"]}" '
                      f"onsubmit=\"return confirm('Удалить голоса этой книги? Следующая сборка создаст их "
                      f"заново.')\"><button class=\"ghost\" type=\"submit\">удалить голоса</button></form>")
            cards.append(f'<div class="card"><h2>Голоса: {html.escape(pack["title"])}</h2>'
                         f'<div class="muted">разметка ролей: {html.escape(llm_line)} · {remove}</div>'
                         f'<table style="margin-top:8px"><tr><th>Роль</th><th>Реплик</th><th>Образец</th></tr>'
                         f'{"".join(rows)}</table></div>')
        return "".join(cards)

    def mode_hint(self) -> str:
        """Что будет с книгой при загрузке — словами, а не догадками по журналу."""
        if self.settings is None:
            return f"Книги длиннее {self.max_hours:g} ч режутся на части автоматически."
        if self.settings.mode == bot_settings.MODE_PARTS:
            return (f"Режим «с разбивкой на части»: книги длиннее {self.settings.parts_hours:g} ч "
                    f"собираются по частям, склейка — в конце.")
        return "Режим «одна книга целиком»: книга идёт одним прогоном, без нарезки."

    def settings_card(self) -> str:
        """Админка: как обрабатывать книги. Настройки те же, что видит бот, и живут в томе."""
        if self.settings is None:
            return ""
        options = "".join(
            f'<option value="{html.escape(value)}"{" selected" if value == self.settings.mode else ""}>'
            f'{html.escape(title)}</option>'
            for value, title in bot_settings.MODES.items())
        note = ""
        if self.settings.mode == bot_settings.MODE_PARTS:
            note = (f'<div class="note">Режим частей: книгу длиннее {self.settings.parts_hours:g} ч бот '
                    f'режет по абзацам; каждая часть уходит в облако только на синтез, сырьё '
                    f'возвращается в том, готовая часть приходит архивом. Когда готовы все части — '
                    f'склейка, громкость, музыка и сборка книги здесь, в контейнере. Google Drive в '
                    f'этом режиме не используется.</div>')
        return (f'<div class="card"><h2>Настройки</h2>'
                f'<form method="post" action="/settings">'
                f'<div><label>Как обрабатывать книги</label><br>'
                f'<select name="mode">{options}</select></div>'
                f'<div style="margin-top:8px"><label>Длинные книги резать на части по</label> '
                f'<input type="number" name="parts_hours" step="0.5" min="0.5" max="12" '
                f'value="{self.settings.parts_hours:g}" style="width:5em"> ч</div>'
                f'<div style="margin-top:8px"><label>'
                f'<input type="checkbox" name="drive"{" checked" if self.settings.drive else ""}> '
                f'копировать книгу на Google Drive</label></div>'
                f'<div style="margin-top:8px"><label>Считать сборку зависшей, если нет движения</label> '
                f'<input type="number" name="stall_minutes" step="5" min="0" max="600" '
                f'value="{self.settings.stall_minutes}" style="width:5em"> мин '
                f'<span class="muted">(журнал не растёт — машину гасим и пишем предупреждение; 0 — не следить)</span></div>'
                f'<div style="margin-top:8px"><label>Профилей Google Colab</label> '
                f'<input type="number" name="profiles" step="1" min="1" max="{bot_settings.PROFILES_MAX}" '
                f'value="{self.settings.profiles}" style="width:4em"> '
                f'<span class="muted">(аккаунты, между которыми бот переключается при сбоях)</span></div>'
                f'<div style="margin-top:8px"><label>Хранить завершённое</label> '
                f'<input type="number" name="keep_days" step="1" min="1" max="365" '
                f'value="{self.settings.keep_days}" style="width:5em"> дн. '
                f'<span class="muted">(старые задания и их журналы, книги по частям, архивы и книги '
                f'удаляются сами; сырьё частей собранной книги — через сутки; журналы Colab CLI '
                f'обрезаются)</span></div>'
                f'{self.voices_fields()}'
                f'<div style="margin-top:10px"><button type="submit">Сохранить</button></div>'
                f'</form>'
                f'<form method="post" action="/llm/models" style="margin-top:8px"><button class="ghost" '
                f'type="submit" title="Запросить у поставщика список моделей по сохранённому ключу">'
                f'считать модели</button></form>'
                f'<div class="muted" style="margin-top:8px">Сейчас: '
                f'{html.escape(self.settings.summary())}</div>{note}</div>')


    def job_row(self, job) -> str:
        meta = job.meta
        icons = {"done": "✅", "failed": "⚠️", "running": "▶️", "queued": "⏳", "stopped": "⏹",
                 "splitting": "✂️", "waiting": "🧩", "gluing": "🧩", "choice": "❓", "held": "⏸",
                 "paused": "⏸", "casting": "🎭", "choice_voices": "🎭"}
        started = meta.get("started") or meta.get("created")
        finished = meta.get("finished") or time.time()
        elapsed = human_delta(finished - float(started or finished))
        chain = self.chains.by_id(meta.get("chain") or "") if self.chains is not None else None
        tail = ""
        if job.status in ("splitting", "waiting", "gluing") and chain is not None:
            # Книга с частями: строки журнала тут ни о чём — важно, где цепочка.
            tail = (f'<div class="muted">{html.escape(chain.summary())} · порог '
                    f'{float(chain.meta.get("parts_hours") or 0):g} ч</div>')
        elif job.status == "running":
            lines = [line.strip() for line in job.tail(400).splitlines() if line.strip()]
            progress = job.progress()
            # Прогресс по сегментам важнее последней строки журнала: во время синтеза журнал
            # забит служебными строками, а «сколько осталось» — то, ради чего страницу открыли.
            tail = f'<div class="muted">{html.escape(progress_line(progress))}</div>'
            if lines:
                tail += f'<div class="muted" style="opacity:.6">{html.escape(lines[-1][:140])}</div>'
        note = ""
        if meta.get("note"):
            note = f'<div class="note">{html.escape(str(meta["note"])[:160])}</div>'
        actions = [f'<a href="/log/{job.meta["session"]}" target="_blank">журнал</a>']
        if meta.get("result"):
            actions.append(f'<a href="/result/{urllib.parse.quote(pathlib.Path(meta["result"]).name)}">'
                           f'скачать</a>')
        # Ссылку на папку берём только «настоящую»: пометка drivefs вроде «local-105» ведёт
        # в никуда, поэтому в этом случае даём рабочий поиск по имени книги.
        links = job.drive_links()
        folder = next((url for url in links if "/folders/" in url), "")
        if not folder:
            folder = job.drive_search_link()
        if folder:
            label = "папка Drive" if "/folders/" in folder else "найти на Drive"
            actions.append(f'<a href="{html.escape(folder)}" target="_blank">{label}</a>')
        if job.status == "running":
            actions.append(f'<a href="/eta/{job.meta["session"]}" title="Проценты, сегменты и оценка '
                           f'времени до конца">сколько осталось</a>')
            actions.append(f'<form class="inline" method="post" action="/stop/{job.meta["session"]}">'
                           f'<button class="ghost" type="submit">остановить</button></form>')
        elif job.status == "choice_voices":
            for choice, label, hint in (("keep", "старые голоса", "Взять готовые голоса этой книги"),
                                        ("new", "голоса с нуля", "Заново разметка ролей и голоса героев")):
                actions.append(f'<form class="inline" method="post" action="/voices/choice/{job.meta["session"]}/'
                               f'{choice}"><button type="submit" title="{hint}">{label}</button></form>')
        elif job.status == "paused":
            # Пауза после круга по профилям (или ждёт входа в следующий профиль): «продолжить».
            actions.append(f'<form class="inline" method="post" action="/resume/{job.meta["session"]}">'
                           f'<button type="submit" title="Пройти по профилям Colab заново">'
                           f'продолжить</button></form>')
            actions.append(f'<form class="inline" method="post" action="/delete/{job.meta["session"]}" '
                           f"onsubmit=\"return confirm('Убрать это задание из списка?')\""
                           f'><button class="ghost" type="submit">удалить</button></form>')
        elif job.status == "choice":
            # Длинная книга ждёт решения — те же две кнопки, что в Telegram.
            for choice, label, hint in (("cut", "резать на части", "Части озвучатся по одной, книга "
                                         "соберётся здесь в конце"),
                                        ("whole", "одним файлом", "Одним прогоном машины, без нарезки")):
                actions.append(f'<form class="inline" method="post" action="/choice/{job.meta["session"]}/'
                               f'{choice}"><button type="submit" title="{hint}">{label}</button></form>')
        elif job.status in ("splitting", "waiting", "gluing") and chain is not None:
            # Книга с частями: «повторить» у неё бессмысленно (машину она не занимает), зато нужна
            # кнопка «продолжить» — ею цепочка поднимается после сбоя или перезапуска бота.
            actions.append('<form class="inline" method="post" action="/chain/continue/'
                           + chain.id + '"><button class="ghost" type="submit" title="Продолжить '
                           'с первой неготовой части">продолжить генерацию</button></form>')
            actions.append(f'<form class="inline" method="post" action="/stop/{job.meta["session"]}">'
                           f'<button class="ghost" type="submit" title="Остановить книгу вместе с '
                           f'частями">остановить</button></form>')
        else:
            part_of_book = bool(meta.get("chain") and (meta.get("part_index") or meta.get("glue")))
            actions.append(f'<form class="inline" method="post" action="/repeat/{job.meta["session"]}">'
                           f'<button class="ghost" type="submit" title="'
                           + ("Повторить только эту часть" if meta.get("part_index") else
                              "Запустить сборку этой книги заново")
                           + f'">{"повторить часть" if meta.get("part_index") else "повторить"}</button></form>')
            if part_of_book and chain is not None:
                actions.append('<form class="inline" method="post" action="/chain/restart/' + chain.id + '" '
                               "onsubmit=\"return confirm('Начать всю книгу заново: новая нарезка и все части?')\""
                               '><button class="ghost" type="submit" title="Остановить всё по книге и озвучить '
                               'её целиком заново">вся книга заново</button></form>')
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
            try:                                  # login может быть любым объектом с state()
                state = getattr(self.login, "state")()   # noqa: B009
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
                f'<div>Аккаунт{html.escape(self.active_profile())}: {badge}</div>'
                f'<div class="muted">Вход в Colab тот же, что и в Telegram: подтвердите доступ в '
                f'браузере и пришлите код. «Сбросить вход» откладывает token.json и всё состояние '
                f'CLI (сессии, настройки, журнал) — после него можно войти другим аккаунтом. '
                f'Вход и подтверждение Drive проходите под одним аккаунтом.</div>'
                f'{code_form}'
                f'<div style="margin-top:10px">{" ".join(buttons)}</div>'
                f'{extra_html}'
                f'{self.profiles_block()}'
                f'</div>')

    def active_profile(self) -> str:
        if self.profiles is None or not getattr(self.profiles, "enabled", False):
            return ""
        try:
            return " — " + str(self.profile_label(self.profiles.active()))
        except Exception:
            return ""

    def profiles_block(self) -> str:
        """Профили: у каждого «войти», «выбрать», «выйти», «удалить»; внизу «добавить» и «выйти из всех»."""
        if self.profiles is None or not getattr(self.profiles, "enabled", False):
            return ""
        def button(action: str, number: int, label: str, ghost: bool = True, confirm: str = "") -> str:
            path = f"/profile/{action}" + (f"/{number}" if number else "")
            check = f" onsubmit=\"return confirm('{confirm}')\"" if confirm else ""
            style = ' class="ghost"' if ghost else ""
            return (f'<form class="inline" method="post" action="{path}"{check}>'
                    f'<button{style} type="submit">{label}</button></form>')
        rows = []
        active = self.profiles.active()
        for number in self.profiles.numbers():
            email = self.profiles.account(number)
            if self.profiles.signed_in(number):
                state = '<span class="ok">вход есть</span>' + (f' · {html.escape(email)}' if email else "")
            else:
                state = '<span class="bad">входа нет</span>'
            mark = "▶️ " if number == active else ""
            actions = [button("login", number, "войти", ghost=False)]
            if number != active:
                actions.append(button("activate", number, "выбрать"))
            actions.append(button("logout", number, "выйти"))
            if self.profiles.count() > 1:
                actions.append(button("delete", number, "удалить", confirm=f"Удалить профиль {number}?"))
            rows.append(f'<tr><td class="name">{mark}Профиль {number}</td><td>{state}</td>'
                        f'<td>{" ".join(actions)}</td></tr>')
        return (f'<h2 style="margin-top:16px">Профили</h2>'
                f'<div class="muted">У каждого профиля свой вход Google. Сборка падает дважды подряд — '
                f'бот сам переходит на следующий профиль; прошёл все — ставит книгу на паузу. '
                f'Профиль без входа: бот просит войти в него и продолжает сам.</div>'
                f'<table>{"".join(rows)}</table>'
                f'<div style="margin-top:8px">{button("add", 0, "добавить профиль")} '
                f'{button("logout_all", 0, "выйти из всех профилей", confirm="Выйти из всех профилей?")}</div>')

    def eta_page(self, job) -> str:
        """«Сколько осталось» — та же кнопка, что в Telegram: проценты по сегментам и оценка."""
        state = job.progress()
        done, segments = state.get("done"), state.get("segments")
        minutes = (time.time() - float(job.meta.get("started") or time.time())) / 60
        rows = [(f"стадия {state['stage_index']}/{state['stages']}", state["stage_title"]),
                ("идёт", human_delta(minutes * 60))]
        rows.append(("сегментов озвучено",
                     f"{done} из {segments} ({round(100 * done / segments)} %)" if done and segments else "—"))
        if state.get("measured_rate"):
            rows.append(("скорость по замерам", f"{state['measured_rate']} сегм/мин"))
        if state.get("expected_hours"):
            rows.append(("ожидаемый звук", f"{state['expected_hours']} ч (синтез идёт примерно столько же)"))
        rows.append(("осталось", f"≈{state['remaining']}" if state.get("remaining_minutes") else "—"))
        rows.append(("закончится около", state["eta"] or "—"))
        table = "".join(f"<tr><th>{html.escape(name)}</th><td>{html.escape(str(value))}</td></tr>"
                        for name, value in rows)
        return (f'<div class="card"><h2>Сколько осталось: {html.escape(job.meta["name"])}</h2>'
                f'<div class="muted" id="progress">{html.escape(progress_line(state))}</div>'
                f'<table style="margin-top:10px">{table}</table>'
                f'<div class="muted" style="margin-top:8px">Обновляется само · '
                f'<a href="/log/{job.meta["session"]}">журнал</a> · <a href="/">к сборкам</a></div>'
                f'<script>'
                f'const pr = document.getElementById("progress");'
                f'async function pull() {{'
                f'  try {{'
                f'    const r = await fetch("/api/eta/{job.meta["session"]}", {{cache: "no-store"}});'
                f'    if (r.ok) {{ const j = await r.json(); if (j.line) {{ pr.textContent = j.line; }} }}'
                f'  }} catch (e) {{}}'
                f'  setTimeout(pull, 5000);'
                f'}}'
                f'setTimeout(pull, 2000);'
                f'</script></div>')

    def log_page(self, job) -> str:
        """Живой журнал: страница сама дотягивает новые строки и обновляет статус."""
        payload = job.log_payload(0)
        status_line = (f'<span id="status">{html.escape(job.status)} · {payload["elapsed"]}</span>')
        return (f'<div class="card"><h2>Журнал: {html.escape(job.meta["name"])}</h2>'
                f'<div class="muted">Состояние: {status_line} · '
                f'<a href="/eta/{job.meta["session"]}">сколько осталось</a> · обновляется само, '
                f'перезагружать страницу не нужно. '
                f'<a href="/log/{job.meta["session"]}?raw=1">текстом</a> · '
                f'<a href="/">к сборкам</a></div>'
                f'<div class="muted" id="progress">{html.escape(progress_line(payload["progress"]))}</div>'
                f'<pre id="log" style="white-space:pre-wrap;max-height:70vh;overflow:auto;'
                f'background:#0f1114;border:1px solid #2a2f36;border-radius:8px;padding:12px;'
                f'margin-top:10px">{html.escape(payload["text"])}</pre></div>'
                f'<script>'
                f'const pre = document.getElementById("log");'
                f'const st = document.getElementById("status");'
                f'const pr = document.getElementById("progress");'
                f'let offset = {payload["offset"]};'
                # Последние события показываем сразу (хвост внизу), и держим хвост внизу,
                # пока пользователь сам не отлистал вверх: тогда «за нами» не тянет.
                f'let follow = true;'
                f'function toBottom() {{ pre.scrollTop = pre.scrollHeight; }}'
                f'pre.addEventListener("scroll", function () {{'
                f'  follow = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40;'
                f'}});'
                f'toBottom();'
                f'async function pull() {{'
                f'  try {{'
                f'    const r = await fetch("/log/{job.meta["session"]}/tail?offset=" + offset,'
                f' {{cache: "no-store"}});'
                f'    if (r.ok) {{'
                f'      const j = await r.json();'
                f'      if (j.text) {{'
                f'        pre.textContent += j.text; offset = j.offset;'
                f'        if (follow) {{ toBottom(); }}'
                f'      }}'
                f'      st.textContent = j.status + " · " + j.elapsed;'
                f'      if (j.progress) {{ pr.textContent = j.progress.line; }}'
                f'    }}'
                f'  }} catch (e) {{}}'
                f'  setTimeout(pull, 1500);'
                f'}}'
                f'setTimeout(pull, 1000);'
                f'</script>')

    def files_card(self, kind: str, base: str) -> str:
        """Книги или архивы: по 10 на страницу, поиск на лету, скачать, запустить, удалить."""
        folder = self.jobs.books if kind == "books" else self.jobs.results
        title = "Книги в папке" if kind == "books" else "Готовые архивы"
        route = "/file/" if kind == "books" else "/result/"
        key, page_key = ("bq", "bpage") if kind == "books" else ("rq", "rpage")
        search = self.param(key)
        files = [path for path in list_files(folder) if matches(path.name, search)]
        page = clamp_page(_int(self.param(page_key) or "1", 1), len(files))
        busy = self.books_in_use() if kind == "books" else set()
        rows = []
        for path in page_slice(files, page):
            try:
                stat = path.stat()
            except OSError:                       # файл удалили прямо сейчас
                continue
            changed = time.strftime("%d.%m %H:%M", time.localtime(stat.st_mtime))
            name = urllib.parse.quote(path.name)
            actions = []
            if kind == "books":
                actions.append(f'<form class="inline" method="post" action="/run/{name}">'
                               f'<button class="ghost" type="submit">запустить</button></form>')
            what = "книгу" if kind == "books" else "архив"
            if path.name in busy:
                actions.append('<span class="muted" title="Книга сейчас в работе">в работе</span>')
            else:
                delete = ("/book/delete/" if kind == "books" else "/result/delete/") + name
                actions.append(f'<form class="inline" method="post" action="{delete}?back={base}" '
                               f'onsubmit="return confirm(\'Удалить {what} «{html.escape(path.name)}»? '
                               f'Это нельзя отменить.\')"><button class="ghost" type="submit">'
                               f'удалить</button></form>')
            rows.append(f'<tr><td><a href="{route}{name}">{html.escape(path.name)}</a></td>'
                        f'<td class="muted">{human_size(stat.st_size)}</td>'
                        f'<td class="muted">{changed}</td><td>{" ".join(actions)}</td></tr>')
        if not rows:
            rows = [f'<tr><td colspan="4" class="muted">'
                    f'{"Ничего не нашлось." if search else "Папка пуста."}</td></tr>']
        hint = "поиск по названию книги" if kind == "books" else "поиск по названию архива"
        return (f'<div class="card" id="list-{kind}"><h2>{title}</h2>'
                + search_box(kind, key, page_key, search, hint)
                + '<div class="list-body"><table>'
                f'<tr><th>Файл</th><th>Размер</th><th>Изменён</th><th></th></tr>'
                + "\n".join(rows) + "</table>"
                + pager(page, len(files), base, page_key, self.query_rest(page_key)) + "</div></div>")

    def books_in_use(self) -> set:
        """Книги, которые сейчас нельзя удалять: их собирают, режут, ждут или ждут ответа."""
        names = set()
        for job in self.jobs.load():
            if job.status in ("running", "queued", "choice", "splitting", "waiting", "gluing", "held",
                              "casting", "paused", "choice_voices"):
                names.add(pathlib.Path(job.meta.get("book") or "").name)
        if self.chains is not None:
            for chain in self.chains.load():
                if chain.status not in ("done", "stopped", "replaced"):
                    names.add(pathlib.Path(chain.meta.get("book") or "").name)
        names.discard("")
        return names

    def delete_file(self, kind: str, name: str) -> str:
        """Удалить книгу из папки или архив из результатов. Задания и журналы остаются."""
        folder = self.jobs.books if kind == "books" else self.jobs.results
        target = (folder / safe_name(name)).resolve()
        if target.parent != folder.resolve() or not target.is_file():
            return f"Файла «{safe_name(name)}» уже нет."
        if kind == "books" and target.name in self.books_in_use():
            return f"«{target.name}» сейчас в работе — сначала остановите сборку, потом удаляйте."
        try:
            target.unlink()
        except OSError as exc:
            return f"Удалить «{target.name}» не вышло: {exc}"
        what = "Книга" if kind == "books" else "Архив"
        return f"{what} «{target.name}» удалена." if kind == "books" else f"{what} «{target.name}» удалён."

    # ────────────────────────────── файлы и действия ──────────────────────────────
    def send_file(self, folder: pathlib.Path, name: str, kind: str) -> tuple:
        target = (folder / safe_name(name)).resolve()
        root = folder.resolve()
        if root != target.parent or not target.is_file():
            return 404, "text/plain; charset=utf-8", "файл не найден".encode(), {}
        # Отдаём сам путь: Handler читает файл потоком, в память архив целиком не тянем.
        return (200, kind, target,
                {"Content-Disposition": f'attachment; filename="{target.name}"'})

    def run_existing(self, name: str) -> tuple:
        """Запустить сборку книги, которая уже лежит в папке (повтор генерации)."""
        book = self.jobs.books / safe_name(name)
        if not book.is_file():
            return self.redirect("/files", f"Файла «{name}» нет в папке.")
        if self.jobs.live_running():
            return self.redirect("/", "Сейчас идёт сборка — дождитесь её конца или нажмите "
                                       "«остановить всё».")
        job = self.jobs.add(book.name, "", book)
        job.meta["ask_voices"] = True        # повтор: голоса книги есть — спросить, старые или с нуля
        job.save()
        if self.ask_split(job, book):
            return self.redirect("/", f"«{book.name}» длинная — выберите в строке задания: резать на "
                                      f"части или одним файлом (вопрос продублирован в Telegram).")
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
        if len(data) > MAX_UPLOAD:
            return self.redirect("/", f"Файл больше {MAX_UPLOAD // 1024 // 1024} МБ — "
                                       "положите его в папку books тома вручную.")
        if self.jobs.live_running():
            return self.redirect("/", "Сейчас идёт сборка — дождитесь её конца или остановите.")
        from jobs import safe_stem
        target = self.jobs.books / f"{safe_stem(name)}.epub"
        staging = target.with_name(f"{target.name}.upload-{os.urandom(4).hex()}")
        staging.write_bytes(data)              # сначала целиком во временный файл…
        staging.replace(target)                # …и только потом на место книги
        job = self.jobs.add(name, "", target)
        if self.ask_split(job, target):
            return self.redirect("/", f"Принял «{name}»: книга длинная — выберите в строке задания, "
                                      f"резать на части или одним файлом (вопрос продублирован в Telegram).")
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
    """Файлы папки, свежие сверху. Исчезнувшие по пути файлы просто пропускаем."""
    if not folder.is_dir():
        return []
    files = []
    for path in folder.iterdir():
        try:
            if path.is_file():
                files.append((path.stat().st_mtime, path))
        except OSError:                       # файл удалили прямо сейчас
            continue
    return [path for _, path in sorted(files, key=lambda item: item[0], reverse=True)]


def page_slice(items: list, page: int) -> list:
    page = max(1, page)
    return items[(page - 1) * PER_PAGE: page * PER_PAGE]


def matches(text: str, search: str) -> bool:
    """Поиск без учёта регистра и «ё/е»: «Ёлка» находится и по «елка»."""
    if not search:
        return True
    fold = lambda value: value.casefold().replace("ё", "е")
    return all(word in fold(text) for word in fold(search).split())


def clamp_page(page: int, total: int) -> int:
    """Номер страницы в пределах списка: после поиска или удаления «страница 5 из 2» не нужна."""
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    return min(max(1, page), pages)


def search_box(kind: str, key: str, page_key: str, value: str, hint: str) -> str:
    return (f'<input type="search" class="search" name="{key}" data-list="{kind}" '
            f'data-page="{page_key}" value="{html.escape(value)}" placeholder="{hint}" '
            f'autocomplete="off" style="width:100%;margin:4px 0 8px">')


def pager(page: int, total: int, base: str, key: str = "page", params: dict | None = None) -> str:
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    if pages == 1:
        return f'<div class="pager muted">всего записей: {total}</div>'

    def link(number: int) -> str:
        return base + "?" + urllib.parse.urlencode(dict(params or {}, **{key: number}))

    parts = []
    if page > 1:
        parts.append(f'<a href="{html.escape(link(page - 1))}">← новее</a>')
    parts.append(f"<span>страница {page} из {pages}</span>")
    if page < pages:
        parts.append(f'<a href="{html.escape(link(page + 1))}">старее →</a>')
    return f'<div class="pager">{" ".join(parts)} · всего записей: {total} (по {PER_PAGE} на страницу)</div>'


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
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            # Тело даже не читаем: не даём одному запросу утопить контейнер в памяти.
            self._reply(413, "text/plain; charset=utf-8", "файл больше 200 МБ".encode(), {})
            return
        try:
            body = self.rfile.read(length) if length else b""
            code, kind, payload, headers = self.app.route(
                method, self.path, {}, self.headers.get("Content-Type", ""), body)
        except Exception as exc:                          # одна ошибка не должна ронять страницу
            code, kind, payload, headers = 500, "text/plain; charset=utf-8", str(exc).encode(), {}
        self._reply(code, kind, payload, headers, method)

    def _reply(self, code: int, kind: str, payload, headers: dict, method: str = "GET") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        if isinstance(payload, pathlib.Path):             # файл: отдаём потоком
            size = payload.stat().st_size
            self.send_header("Content-Length", str(size))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if method != "HEAD":
                with payload.open("rb") as handle:
                    shutil.copyfileobj(handle, self.wfile, 1 << 16)
            return
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
