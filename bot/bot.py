#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram-бот сборки аудиокниг: принимает книгу, запускает задание в фоне, показывает ход.

Как это работает:

* вы присылаете боту EPUB — он скачивает файл в том /data/books и запускает нашу обёртку
  (colab/cli/run-book.py) в фоновом задании (tmux внутри контейнера);
* обёртка сама поднимает машину Colab, подключает Google Drive, и сама же шлёт уведомления
  о стадиях в этот чат — тем же токеном бота;
* готовый архив кладётся в /data/results, копия уезжает на Google Drive; бот присылает итог
  со ссылками на Drive.

Команды: /help, /status, /jobs, /log <номер>, /stop <номер>, /login (вход в Colab), /usage.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time

import colab_auth
from jobs import Jobs, safe_stem
from tgapi import Bot, TelegramError

DATA = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
PIPELINE = pathlib.Path(os.environ.get("PIPELINE_DIR", "/opt/qwen-epub-audiobook"))
STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", str(DATA / "state.json")))
TOKEN_FILE = pathlib.Path(os.environ.get("TOKEN_FILE", os.path.expanduser("~/.config/colab-cli/token.json")))

GPU = os.environ.get("GPU", "T4")
DRIVE_DIR = os.environ.get("DRIVE_DIR", "Audiobooks-colab")
SHRINK_HOURS = float(os.environ.get("SHRINK_HOURS", "7"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "1"))
POLL = int(os.environ.get("POLL_SECONDS", "10"))

HELP = """Я собираю аудиокниги через Google Colab.

Пришлите EPUB — начну сборку в фоне, а по ходу дела напишу о каждой стадии.
Собираю по одной книге: пока идёт сборка, новые не принимаю — дождитесь конца или остановите
текущую командой /stop.
В конце пришлю архив (он же ляжет в том) и ссылки на Google Drive.

/status — что делается прямо сейчас
/jobs — последние задания
/repeat <номер> — собрать эту книгу заново
/delete <номер> — убрать задание из списка (книга и архив остаются)
/log <номер> — хвост журнала задания
/stop <номер> — остановить задание
/login — войти в Google Colab (если вход истёк)
/usage — остаток и активные машины аккаунта
/help — эта подсказка

Параметры запуска: машина %s, папка Drive «%s», резать книги длиннее %s ч.
Можно и кнопками: /start покажет меню — «Статус», «Задания», «Войти в Colab», «Остаток»,
«Остановить всё».
""" % (GPU, DRIVE_DIR, f"{SHRINK_HOURS:g}")

# Нижние кнопки под сообщениями: самые ходовые действия, чтобы не писать команды руками.
MENU = [[("📊 Статус", "status"), ("📋 Задания", "jobs")],
        [("🔑 Войти в Colab", "login"), ("📈 Остаток", "usage")],
        [("⏹ Остановить всё", "stop_ask")],
        [("❓ Помощь", "help")]]

# Кнопки под отчётом о неудаче: сразу видно, что делать дальше.
FAILED_MENU = [[("🔁 Повторить", "repeat"), ("📜 Журнал", "log")],
               [("🏠 Меню", "menu")]]


def looks_like_code(text: str) -> bool:
    """Код Google длинный (обычно 40+ знаков), без пробелов; «4/0...» тоже подходит."""
    candidate = (text or "").strip()
    return len(candidate) >= 20 and " " not in candidate and "\n" not in candidate


def same_file(first: pathlib.Path, second: pathlib.Path) -> bool:
    """Один и тот же файл? Сравниваем размер, затем содержимое (книги небольшие)."""
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        import hashlib
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()   # noqa: E731
        return digest(first) == digest(second)
    except OSError:
        return False


class ColabLogin:
    """Вход в Google Colab — один на два входа: Telegram и веб-страницу.

    Держит терминал CLI, в который подставляется код из браузера, умеет показать ссылку,
    проверить вход, отложить токен (сброс) и спросить остаток аккаунта.
    """

    def __init__(self, token_file: pathlib.Path):
        self.token_file = pathlib.Path(token_file)
        self.login = colab_auth.Login(self.token_file)
        self.session = None
        self.url = ""
        self.note = ""

    def awaiting_code(self) -> bool:
        """Открыт ли вход, ждущий код. Не важно, начат он в Telegram или на странице."""
        return self.session is not None

    def state(self) -> dict:
        if self.login.has_token():
            signed = True
        elif self.session:
            signed = None                 # вход начат, но код ещё не пришёл: это не «входа нет»
        else:
            signed = False
        return {"signed_in": signed, "busy": bool(self.session),
                "url": self.url if self.session else "",
                "note": self.note or ("ждём код из браузера" if self.session else "")}

    def begin(self) -> str:
        if self.session and self.session.expired():       # протухшую попытку закрываем
            self.session.close()
            self.session, self.url = None, ""
            self.note = "прежняя попытка входа просрочена"
        if not self.session and self.login.terminal_logged_in():
            self.note = "вход есть"
            return "Вход в Colab уже есть — авторизоваться заново не нужно."
        if self.session:
            return "Вход уже начат: пришлите код из браузера."
        try:
            session = self.login.start()
        except Exception as exc:
            return f"Не смог запустить CLI: {exc}"
        url = session.wait_for_url(120)
        if not url:
            session.close()
            return "CLI не выдал ссылку авторизации: " + session.summary()
        session.wait_for_prompt(60)
        self.session, self.url, self.note = session, url, "ждём код из браузера"
        return ("Вход в Google Colab\n\n1. Откройте ссылку (можно с телефона):\n" + url +
                "\n\n2. Войдите в нужный аккаунт и подтвердите доступ.\n"
                "3. Google откроет СТРАНИЦУ С КОДОМ — скопируйте код и пришлите его мне следующим "
                "сообщением.\n\nПока код не пришёл, вход не завершён: подтверждения в браузере "
                "недостаточно, CLI ждёт именно код.")

    def submit(self, code: str) -> str:
        if not self.session:
            return "Сначала начните вход: /login (или кнопка «войти» на странице)."
        if self.session.expired():
            self.session.close()
            self.session, self.url = None, ""
            return "Прежняя попытка входа просрочена (прошло больше 15 минут). Начните заново: /login."
        session, self.session, self.url = self.session, None, ""
        session.submit_code(code)
        answer = session.wait_finish(120)
        session.close()
        # Успех проверяем не по тексту вывода (там по пути печатается приглашение «Enter the
        # authorization code»), а по делу: появился token.json и CLI работает без запроса кода.
        if self.login.has_token() and self.login.terminal_logged_in():
            self.note = "вход выполнен"
            return "✅ Вход выполнен, token.json сохранён в томе конфигов."
        self.note = "не получилось войти"
        return "Не получилось войти. Ответ CLI:\n" + (session.summary() or answer[-300:])

    def reset(self) -> str:
        """Полный сброс входа Colab: откладываем token.json И остальное состояние CLI.

        Один token.json мультиаккаунт не лечит: sessions.json держит машины, history — журнал,
        а главное — новый вход должен совпасть с аккаунтом, под которым вы подтверждаете Drive.
        Поэтому сбрасываем всю папку настроек CLI, чтобы всё применилось заново.
        """
        if self.session:
            self.session.close()
            self.session, self.url = None, ""
        config_dir = self.token_file.parent
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backed = []
        for path in (self.token_file, config_dir / "sessions.json",
                     config_dir / "settings.json", config_dir / "history"):
            if not path.exists():
                continue
            backup = path.with_name(f"{path.name}.backup-{stamp}")
            try:
                path.rename(backup)
                backed.append(path.name)
            except OSError as exc:
                return f"Не смог отложить {path.name}: {exc}"
        if not backed:
            self.note = "входа и не было"
            return "Входа и не было — можно сразу входить заново."
        stopped = self._stop_colab_sessions()
        self.note = "вход и состояние CLI сброшены: " + ", ".join(backed)
        return ("Сброс полный: отложены " + ", ".join(backed) + ".\n"
                + stopped +
                "Теперь нажмите «войти» и войдите заново. ВАЖНО: и вход в Colab, и подтверждение "
                "доступа к Google Drive проходите под ОДНИМ аккаунтом Google — иначе Google снова "
                "отклонит подключение Drive (ошибка 400).")

    def _stop_colab_sessions(self) -> str:
        """Гасим свои сессии Colab, пока вход ещё жив: со сброшенным токеном их уже не остановить."""
        binary = shutil.which("colab")
        if not binary:
            return ""
        try:
            done = subprocess.run([binary, "sessions"], capture_output=True, text=True,
                                  timeout=60, env=dict(os.environ))
        except (subprocess.TimeoutExpired, OSError):
            return ""
        stopped = []
        for name in re.findall(r"^\[([^\]?]*)\]\s+\S+\s+\|", done.stdout or "", re.M):
            if not name:
                continue
            try:
                subprocess.run([binary, "stop", "-s", name], capture_output=True, text=True,
                               timeout=120, env=dict(os.environ))
                stopped.append(name)
            except (subprocess.TimeoutExpired, OSError):
                pass
        return ("остановлены сессии Colab: " + ", ".join(stopped) + ".\n") if stopped else ""

    def usage(self) -> str:
        binary = shutil.which("colab")
        if not binary:
            return "CLI не установлен в этом контейнере."
        try:
            done = subprocess.run([binary, "usage"], capture_output=True, text=True, timeout=180,
                                  env=dict(os.environ), input="")
            output = (done.stdout + done.stderr).strip() or "пусто"
        except Exception as exc:
            return f"Не вышло: {exc}"
        signed = "Enter the authorization code" not in output
        self.note = f"вход: {'есть' if signed else 'нет'}"
        if not signed and self.session:
            return ("Вход ещё НЕ завершён: браузер вы подтвердили, но нужен код с той страницы, "
                    "которую показал Google — пришлите его сюда (или вставьте в поле на странице).\n"
                    + output[-400:])
        return f"Вход в Colab: {'есть' if signed else 'нет'}\n{output[-600:]}"


class App:
    def __init__(self, token: str):
        self.bot = Bot(token)
        self.username = ""
        self.jobs = Jobs(DATA, PIPELINE, max_jobs=MAX_JOBS)
        self.state = self.load_state()
        self.colab = ColabLogin(pathlib.Path(TOKEN_FILE))

    # ────────────────────────────── состояние ──────────────────────────────
    def load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except ValueError:
                pass
        return {"offset": 0, "owner": "", "seen": []}

    def save_state(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def allowed(self, chat_id) -> bool:
        owner = (self.state.get("owner") or "").strip()
        allowed = os.environ.get("TELEGRAM_CHAT_ID", "").replace(" ", "")
        permitted = {x for x in allowed.split(",") if x} | ({owner} if owner else set())
        if not permitted:                       # первый, кто написал, становится владельцем
            self.state["owner"] = str(chat_id)
            self.save_state()
            self.send_menu(chat_id, "Вы назначены владельцем этого бота — дальше книги буду "
                                    "принимать только от вас.\n\n" + HELP)
            return True
        return str(chat_id) in permitted

    # ────────────────────────────── приём обновлений ──────────────────────────────
    def loop(self) -> None:
        while True:
            updates = self.bot.updates(int(self.state.get("offset", 0)), wait=POLL)
            try:
                for update in updates:
                    # Смещение фиксируем до обработки: иначе сбой в обработчике вернёт ту же
                    # книгу в следующем опросе, и задание запустится дважды.
                    self.state["offset"] = max(int(self.state.get("offset", 0)),
                                               int(update["update_id"]) + 1)
                    self.save_state()
                    self.handle(update)
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception as exc:            # одна ошибка не должна ронять бота
                import traceback
                print(f"[bot] сбой при обработке: {exc}", flush=True)
                traceback.print_exc()
                time.sleep(5)

    def tick(self) -> None:
        """Завершившиеся задания: итог в чат; затем запускаем то, что стоит в очереди."""
        for job in self.jobs.refresh().values():
            self.report(job)
        for job in reversed(self.jobs.load()):
            if job.status == "queued" and self.jobs.free_slot():
                self.launch(job)

    def handle(self, update: dict) -> None:
        # Нажатие кнопки: отдельная ветка — у такого обновления нет ни текста, ни документа.
        if update.get("callback_query"):
            self.on_button(update["callback_query"])
            return
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return
        text = (message.get("text") or "").strip()
        document = message.get("document") or {}

        # Сначала пускаем только своих: чужой чат не должен трогать ни книги, ни вход в Colab.
        if not self.allowed(chat_id):
            self.bot.send(chat_id, "Это бот владельца, книги я не принимаю.")
            return

        if text and not text.startswith("/") and self.colab.awaiting_code():
            # Код авторизации принимаем при любом открытом входе (и после /login в чате, и после
            # кнопки «войти» на странице). Но не всякий текст — иначе обычная фраза ломала вход.
            if looks_like_code(text):
                print("[bot] получен код авторизации, отправляю его CLI", flush=True)
                self.bot.send(chat_id, "Код принят, проверяю…")
                self.in_background(chat_id, lambda: self.colab.submit(text), "Вход в Colab")
            else:
                self.bot.send(chat_id, "Это не похоже на код авторизации (он длинный, без пробелов). "
                                       "Вход продолжается: пришлите код со страницы Google, "
                                       "либо /login, чтобы получить ссылку заново.")
            return
        if text and not text.startswith("/") and ("4/" in text or len(text) > 40):
            # Похоже на код, но вход не начат — говорим об этом прямо, а не молчим.
            self.bot.send(chat_id, "Код сейчас некуда вставлять: вход не начат. "
                                   "Начните его — /login в чате или кнопка «войти» на странице, — "
                                   "а потом пришлите код со страницы Google.")
            return
        if text.startswith("/"):
            self.command(chat_id, text)
            return
        if document:
            self.accept_document(chat_id, document)
            return
        if text:
            self.send_menu(chat_id)

    # ────────────────────────────── кнопки ──────────────────────────────
    def send_menu(self, chat_id, text: str = "") -> None:
        """Помощь вместе с кнопками: команды помнить не обязательно."""
        self.bot.send(chat_id, (text or HELP).strip(), buttons=MENU)

    def on_button(self, query: dict) -> None:
        """Нажали кнопку под сообщением: делаем то же, что делает команда.

        В кнопках живут только «свои» действия — чужие данные игнорируем, а чужой чат
        не обслуживаем, как и в обычных сообщениях.
        """
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        data = (query.get("data") or "").strip()
        if chat_id is None:
            return
        if not self.allowed(chat_id):
            self.bot.answer(query.get("id", ""), "Это бот владельца.")
            return
        self.bot.answer(query.get("id", ""), "смотрю…")
        if data in ("help", "menu", ""):
            self.send_menu(chat_id)
        elif data == "status":
            self.status(chat_id)
        elif data == "jobs":
            self.list_jobs(chat_id)
        elif data == "login":
            self.start_login(chat_id)
        elif data == "usage":
            self.show_usage(chat_id)
        elif data == "stop_ask":                       # остановка «всё» — через подтверждение
            self.bot.send(chat_id, "Остановить всё: текущую сборку и очередь, машины освободить?",
                          buttons=[[("⏹ Да, остановить", "stop_all"), ("↩️ Отмена", "menu")]])
        elif data == "stop_all":
            self.bot.send(chat_id, "Останавливаю всё…")
            self.in_background(chat_id, self.stop_all_jobs, "Остановка")
        elif data in ("repeat", "log"):                # из отчёта о неудаче: последнее задание
            jobs = self.jobs.load()
            if not jobs:
                self.bot.send(chat_id, "Заданий нет.")
                return
            if data == "repeat":
                self.repeat_job(chat_id, jobs[0].meta["session"])
            else:
                self.show_log(chat_id, jobs[0].meta["session"])
        else:
            self.send_menu(chat_id)

    # ────────────────────────────── команды ──────────────────────────────
    def command(self, chat_id, text: str) -> None:
        parts = text.split()
        head = parts[0].split("@")[0].lower()
        argument = parts[1] if len(parts) > 1 else ""
        if head in ("/start", "/help"):
            self.send_menu(chat_id)
        elif head == "/status":
            self.status(chat_id)
        elif head == "/jobs":
            self.list_jobs(chat_id)
        elif head == "/log":
            self.show_log(chat_id, argument)
        elif head == "/stop":
            self.stop_job(chat_id, argument)
        elif head in ("/delete", "/del", "/remove"):
            self.delete_job(chat_id, argument)
        elif head == "/repeat":
            self.repeat_job(chat_id, argument)
        elif head == "/login":
            self.start_login(chat_id)
        elif head == "/usage":
            self.show_usage(chat_id)
        else:
            self.send_menu(chat_id, "Не знаю такой команды.\n\n" + HELP)

    def status(self, chat_id) -> None:
        running = [job for job in self.jobs.load() if job.status == "running"]
        queued = [job for job in self.jobs.load() if job.status == "queued"]
        lines = []
        for job in running:
            minutes = (time.time() - float(job.meta.get("started") or time.time())) / 60
            last = [line.strip() for line in job.tail(400).splitlines() if line.strip()]
            lines.append(f"▶️ {job.meta['name']} — {minutes:.0f} мин\n   {last[-1][:120] if last else '…'}")
        for job in queued:
            lines.append(f"⏳ {job.meta['name']} — в очереди")
        head = f"Сейчас: {len(running)} в работе, {len(queued)} в очереди."
        if not running and not queued:
            head = "Свободно: ничего не собирается."
        self.bot.send(chat_id, head + ("\n\n" + "\n".join(lines) if lines else ""), buttons=MENU)

    def list_jobs(self, chat_id) -> None:
        jobs = self.jobs.load()[:10]
        if not jobs:
            self.bot.send(chat_id, "Заданий пока не было.", buttons=MENU)
            return
        icons = {"done": "✅", "failed": "⚠️", "running": "▶️", "queued": "⏳", "stopped": "⏹"}
        lines = [f"{icons.get(job.status, '•')} {job.meta['name']} · {job.status}"
                 f"{' · ' + job.meta['note'][:60] if job.meta.get('note') and job.status == 'failed' else ''}"
                 f"\n   /log {job.meta['session']}" for job in jobs]
        self.bot.send(chat_id, "\n".join(lines), buttons=MENU)

    def show_log(self, chat_id, argument: str) -> None:
        job = self.jobs.by_id(argument) if argument else None
        if job is None:
            jobs = self.jobs.load()
            job = jobs[0] if jobs else None
        if job is None:
            self.bot.send(chat_id, "Заданий нет. Укажите номер: /log <номер из /jobs>", buttons=MENU)
            return
        self.bot.send(chat_id, f"Журнал «{job.meta['name']}» ({job.status}):\n\n" + (job.tail(3200) or "пусто"),
                      buttons=MENU)

    def stop_job(self, chat_id, argument: str) -> None:
        if not argument:                                  # /stop без номера — остановить всё
            self.bot.send(chat_id, "Останавливаю всё…")
            self.in_background(chat_id, self.stop_all_jobs, "Остановка")
            return
        job = self.jobs.by_id(argument)
        if job is None:
            self.bot.send(chat_id, "Такого задания нет. /stop без номера остановит всё.")
            return
        self.bot.send(chat_id, f"Останавливаю «{job.meta['name']}»…")
        self.in_background(chat_id, lambda: f"⏹ «{job.meta['name']}» остановлено.\n" + self.stop_one(job),
                           "Остановка")

    def stop_one(self, job) -> str:
        """Гасит задание и освобождает его машину Colab."""
        self.jobs.stop(job)
        job.meta.update({"status": "stopped", "finished": time.time(), "note": "остановлено вручную"})
        job.save()
        return f"машина {job.meta['session']} освобождена"

    def stop_all_jobs(self) -> str:
        """Останавливает всё: текущую сборку, очередь и машины Colab на них."""
        jobs = [job for job in self.jobs.load() if job.status in ("running", "queued")]
        if not jobs:
            return "Останавливать нечего: сейчас ничего не собирается."
        for job in jobs:
            self.stop_one(job)
        names = ", ".join(f"«{job.meta['name']}»" for job in jobs)
        return (f"⏹ Остановлено заданий: {len(jobs)} — {names}.\n"
                f"Машины Colab освобождены, новые книги можно присылать.")

    def delete_job(self, chat_id, argument: str) -> None:
        """Убрать задание из списка: книга и архив остаются, пропадает только запись и журнал."""
        job = self.jobs.by_id(argument) if argument else None
        if job is None:
            self.bot.send(chat_id, "Что удалить? /delete <номер из /jobs>. Без номера не удаляю, "
                                   "чтобы не снести лишнее.")
            return
        self.bot.send(chat_id, self.jobs.delete(job))

    def repeat_job(self, chat_id, argument: str) -> None:
        """Повторить сборку книги: пригодится, когда сессия Colab прервалась или кончился лимит."""
        job = self.jobs.by_id(argument) if argument else None
        if job is None:
            self.bot.send(chat_id, "Что повторять? /repeat <номер из /jobs>")
            return
        book = pathlib.Path(job.meta.get("book") or "")
        if not book.is_file():
            self.bot.send(chat_id, f"Файла «{book.name}» больше нет в томе — пришлите книгу заново.")
            return
        if self.jobs.live_running():
            self.bot.send(chat_id, "Сейчас идёт другая сборка — сначала /stop или дождитесь её конца.")
            return
        again = self.jobs.add(book.name, chat_id, book)
        self.launch(again)
        self.bot.send(chat_id, f"🔁 Повторяю сборку «{book.name}» — о ходе напишу.")

    def show_usage(self, chat_id) -> None:
        self.in_background(chat_id, self.colab.usage, "Остаток аккаунта")

    # ────────────────────────────── вход в Colab ──────────────────────────────
    def start_login(self, chat_id) -> None:
        """Вход запускаем в фоне: CLI отвечает не мгновенно, а чат ждать не должен."""
        self.bot.send(chat_id, "Начинаю вход в Google Colab, ссылка придёт через несколько секунд…")
        self.in_background(chat_id, self.colab.begin, "Вход в Colab")

    def owner_chat(self) -> str:
        """Кому писать: чат из настроек, иначе тот, кто первым написал боту."""
        return ((os.environ.get("TELEGRAM_CHAT_ID", "").split(",")[0].strip())
                or (self.state.get("owner") or "").strip())

    def in_background(self, chat_id, work, title: str) -> None:
        """Долгие дела (вход, остаток, остановка) — в отдельном потоке: чат продолжает отвечать."""
        def task():
            try:
                answer = work()
            except Exception as exc:
                answer = f"{title}: не вышло — {exc}"
            if answer:
                self.bot.send(chat_id, str(answer))
        threading.Thread(target=task, name="bg", daemon=True).start()

    def notify_owner(self, text: str) -> None:
        """Сообщение от страницы: короткий отчёт в Telegram о том, что там нажали."""
        chat = self.owner_chat()
        if not text:
            return
        if not chat:
            print(f"[bot] некому писать в Telegram (нет TELEGRAM_CHAT_ID и владелец не определён): "
                  f"{text[:80]}", flush=True)
            return
        self.bot.send(chat, text)

    def stop_for_web(self, job) -> None:
        """Остановка задания со страницы: то же самое, что делает /stop в Telegram."""
        self.jobs.stop(job)
        job.meta.update({"status": "stopped", "finished": time.time(),
                         "note": "остановлено со страницы"})
        job.save()

    # ────────────────────────────── книги ──────────────────────────────
    def accept_document(self, chat_id, document: dict) -> None:
        name = document.get("file_name") or "book.epub"
        if not name.lower().endswith((".epub", ".epub3")):
            self.bot.send(chat_id, "Нужен EPUB (его и режет, и озвучивает конвейер). "
                                   "Если книга в MOBI/FB2, переведите её, например:\n"
                                   "ebook-convert book.mobi book.epub (Calibre)")
            return
        running = self.jobs.live_running()
        if running:                     # одна книга за раз: новую не берём, пока не остановят
            busy = running[0]
            minutes = (time.time() - float(busy.meta.get("started") or time.time())) / 60
            self.bot.send(chat_id, f"⏳ Сейчас идёт сборка «{busy.meta['name']}» ({minutes:.0f} мин).\n"
                                   "Пока она не закончится, новые книги не принимаю.\n"
                                   f"Остановить: /stop {busy.meta['session']}")
            return

        size = int(document.get("file_size") or 0)
        if size > 20 * 1024 * 1024:
            self.bot.send(chat_id, "Файл больше 20 МБ — Bot API не отдаёт такие. "
                                   "Положите книгу в том вручную (папка books) или пришлите часть.")
            return
        target = self.jobs.books / f"{safe_stem(name)}.epub"
        fresh = self.jobs.books / f"{safe_stem(name)}-{os.urandom(2).hex()}.epub"
        try:
            self.bot.download(document["file_id"], fresh)
        except Exception as exc:
            self.bot.send(chat_id, f"Не смог скачать файл: {exc}")
            return
        # Одинаковый файл не качаем и не храним дважды: сравниваем по содержимому, а не по имени.
        try:
            if target.exists() and same_file(fresh, target):
                fresh.unlink(missing_ok=True)
                book = target
            else:
                fresh.replace(target)
                book = target
        except OSError as exc:
            book = fresh
            print(f"[bot] не удалось сравнить книги ({exc}), работаю с копией {book.name}", flush=True)
        try:
            job = self.jobs.add(name, chat_id, book)
        except Exception as exc:
            self.bot.send(chat_id, f"Не смог создать задание: {exc}")
            return
        self.bot.send(chat_id, f"📖 Принял «{name}» ({book.stat().st_size / 1e6:.1f} МБ).\n"
                               "Запускаю сборку в фоне — о стадиях напишу сам.")
        self.launch(job)

    def launch(self, job) -> None:
        book = pathlib.Path(job.meta.get("book") or "")
        if not book.is_file():
            job.meta.update({"status": "failed", "note": "файл книги пропал"})
            job.save()
            self.report(job)
            return
        # Задание могло прийти со страницы — там чата нет. Подставляем владельца, иначе
        # об итоге и о стадиях никто не узнает (была именно такая жалоба).
        if not job.meta.get("chat_id"):
            owner = self.owner_chat()
            if owner:
                job.meta["chat_id"] = owner
                job.save()
        try:
            self.jobs.start(job, book, gpu=GPU, drive_dir=DRIVE_DIR, max_hours=SHRINK_HOURS)
        except Exception as exc:
            job.meta.update({"status": "failed", "note": f"не удалось запустить: {exc}"})
            job.save()
            self.report(job)

    def report(self, job) -> None:
        # Пишем владельцу и для заданий, запущенных со страницы (у них нет своего чата).
        chat_id = str(job.meta.get("chat_id") or "").strip() or self.owner_chat()
        if not chat_id:
            print(f"[bot] об итоге «{job.meta.get('name')}» сообщить некому: "
                  f"нет ни чата задания, ни владельца", flush=True)
            return
        minutes = ((job.meta.get("finished") or time.time()) - (job.meta.get("started") or time.time())) / 60
        if job.status == "done":
            result = job.meta.get("result") or ""
            local = pathlib.Path(result)
            try:
                local_copy = self.jobs.results / local.name
                if result and local != local_copy and local.is_file():
                    import shutil
                    shutil.copyfile(local, local_copy)
            except OSError:
                pass
            links = job.drive_links()
            text = [f"✅ «{job.meta['name']}» готова за {minutes:.0f} мин.",
                    f"Файл в томе: {self.jobs.results / pathlib.Path(result).name}"]
            if links:
                text.append("Google Drive:\n" + "\n".join(links))
            else:
                # Не гадаем: показываем последнюю строку журнала про Drive — там причина
                # («не подключился», «не скопировалось»), а не только наша догадка.
                note = job.drive_note()
                text.append("Ссылок на Drive нет — похоже, доступ не подтверждали; "
                            "архив лежит в томе."
                            + (f"\nЖурнал: {note}" if note else ""))
            self.bot.send(chat_id, "\n\n".join(text), buttons=MENU)
        elif job.status == "failed":
            self.bot.send(chat_id, f"⚠️ «{job.meta['name']}» не собралась.\n"
                                   f"{job.meta.get('note') or 'Причина неизвестна — смотрите журнал.'}\n\n"
                                   f"Повторить: /repeat {job.meta['session']} · журнал: "
                                   f"/log {job.meta['session']}", buttons=FAILED_MENU)


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("нет TELEGRAM_BOT_TOKEN — нечего запускать", flush=True)
        return 2
    app = App(token)
    try:
        app.username = app.bot.whoami()
    except TelegramError as exc:
        print(f"Telegram не отвечает: {exc}", flush=True)
        return 3
    for path in (app.jobs.books, app.jobs.results, app.jobs.jobs_dir):
        path.mkdir(parents=True, exist_ok=True)
    stale = app.jobs.fail_stale_queued()
    if stale:
        print(f"[bot] заданий, не успевших запуститься: {stale} — помечены неудачными, "
              f"их можно повторить", flush=True)
        app.notify_owner(f"⚠️ После перезапуска нашлось незапущенных заданий: {stale}. "
                         f"Нажмите «повторить» на странице или /repeat <номер>, "
                         f"чтобы собрать их заново.")
    print(f"[bot] @{app.username} на связи; конвейер {PIPELINE}; данные {DATA}", flush=True)
    print(f"[bot] tmux: {'есть' if app.jobs.has_tmux else 'нет — задания пойдут отдельными процессами'}",
          flush=True)

    if os.environ.get("WEB_ENABLED", "1") not in ("0", "no", "false"):
        import web
        port = int(os.environ.get("WEB_PORT", "8090"))
        page = web.WebApp(app.jobs, max_hours=SHRINK_HOURS, launch=app.launch,
                          stop=app.stop_for_web, login=app.colab, notify=app.notify_owner)
        web.start_server(page, "0.0.0.0", port)
        outside = os.environ.get("WEB_OUTSIDE_PORT", str(port))
        print(f"[bot] страница слушает 0.0.0.0:{port} внутри контейнера; снаружи открывайте "
              f"http://<адрес-машины>:{outside}/ (левая часть пары в ports: compose.yaml)", flush=True)
        # Проверяем себя же: если страница не отвечает изнутри, виноват не проброс портов.
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as check:
                print(f"[bot] самопроверка: страница отвечает {check.status} — значит, дело "
                      f"только в пробросе порта наружу", flush=True)
        except Exception as exc:
            print(f"[bot] ВНИМАНИЕ: страница не отвечает внутри контейнера на порту {port} ({exc}); "
                  f"проверьте WEB_PORT", flush=True)

    app.loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
