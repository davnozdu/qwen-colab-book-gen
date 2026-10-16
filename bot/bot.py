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
import shutil
import subprocess
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
POLL = int(os.environ.get("POLL_SECONDS", "50"))

HELP = """Я собираю аудиокниги через Google Colab.

Пришлите EPUB — начну сборку в фоне, а по ходу дела напишу о каждой стадии.
Собираю по одной книге: пока идёт сборка, новые не принимаю — дождитесь конца или остановите
текущую командой /stop.
В конце пришлю архив (он же ляжет в том) и ссылки на Google Drive.

/status — что делается прямо сейчас
/jobs — последние задания
/log <номер> — хвост журнала задания
/stop <номер> — остановить задание
/login — войти в Google Colab (если вход истёк)
/usage — остаток и активные машины аккаунта
/help — эта подсказка

Параметры запуска: машина %s, папка Drive «%s», резать книги длиннее %s ч.
""" % (GPU, DRIVE_DIR, f"{SHRINK_HOURS:g}")


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

    def state(self) -> dict:
        signed = True if self.login.has_token() else (False if self.session else None)
        return {"signed_in": signed, "busy": bool(self.session),
                "url": self.url if self.session else "", "note": self.note}

    def begin(self) -> str:
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
                "3. Google покажет страницу с кодом — пришлите этот код следующим сообщением.")

    def submit(self, code: str) -> str:
        if not self.session:
            return "Сначала начните вход: /login (или кнопка «войти» на странице)."
        session, self.session, self.url = self.session, None, ""
        session.submit_code(code)
        answer = session.wait_finish(120)
        session.close()
        if self.login.has_token() and "Enter the authorization code" not in answer:
            self.note = "вход выполнен"
            return "✅ Вход выполнен, token.json сохранён в томе конфигов."
        self.note = "не получилось войти"
        return "Не получилось войти. Ответ CLI: " + session.summary()

    def reset(self) -> str:
        if self.session:
            self.session.close()
            self.session, self.url = None, ""
        if not self.token_file.exists():
            self.note = "входа и не было"
            return "Входа и не было — можно сразу входить заново."
        backup = self.token_file.with_name(
            f"{self.token_file.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            self.token_file.rename(backup)
        except OSError as exc:
            return f"Не смог отложить текущий вход: {exc}"
        self.note = f"вход отложен как {backup.name}"
        return (f"Вход сброшен: старый token.json лежит рядом как {backup.name}. "
                f"Дальше нажмите «войти» и войдите заново (можно другим аккаунтом).")

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
        return f"Вход в Colab: {'есть' if signed else 'нет'}\n{output[-600:]}"


class App:
    def __init__(self, token: str):
        self.bot = Bot(token)
        self.username = ""
        self.jobs = Jobs(DATA, PIPELINE, max_jobs=MAX_JOBS)
        self.state = self.load_state()
        self.pending_login = None       # чат, от которого ждём код авторизации
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
            self.bot.send(chat_id, "Вы назначены владельцем этого бота — дальше книги буду "
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
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return
        text = (message.get("text") or "").strip()
        document = message.get("document") or {}

        if self.pending_login and str(chat_id) == self.pending_login and text and not text.startswith("/"):
            self.pending_login = None
            self.bot.send(chat_id, self.colab.submit(text))
            return
        if not self.allowed(chat_id):
            self.bot.send(chat_id, "Это бот владельца, книги я не принимаю.")
            return
        if text.startswith("/"):
            self.command(chat_id, text)
            return
        if document:
            self.accept_document(chat_id, document)
            return
        if text:
            self.bot.send(chat_id, HELP)

    # ────────────────────────────── команды ──────────────────────────────
    def command(self, chat_id, text: str) -> None:
        parts = text.split()
        head = parts[0].split("@")[0].lower()
        argument = parts[1] if len(parts) > 1 else ""
        if head in ("/start", "/help"):
            self.bot.send(chat_id, HELP)
        elif head == "/status":
            self.status(chat_id)
        elif head == "/jobs":
            self.list_jobs(chat_id)
        elif head == "/log":
            self.show_log(chat_id, argument)
        elif head == "/stop":
            self.stop_job(chat_id, argument)
        elif head == "/login":
            self.start_login(chat_id)
        elif head == "/usage":
            self.show_usage(chat_id)
        else:
            self.bot.send(chat_id, "Не знаю такой команды.\n\n" + HELP)

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
        self.bot.send(chat_id, head + ("\n\n" + "\n".join(lines) if lines else ""))

    def list_jobs(self, chat_id) -> None:
        jobs = self.jobs.load()[:10]
        if not jobs:
            self.bot.send(chat_id, "Заданий пока не было.")
            return
        icons = {"done": "✅", "failed": "⚠️", "running": "▶️", "queued": "⏳", "stopped": "⏹"}
        lines = [f"{icons.get(job.status, '•')} {job.meta['name']} · {job.status}"
                 f"{' · ' + job.meta['note'][:60] if job.meta.get('note') and job.status == 'failed' else ''}"
                 f"\n   /log {job.meta['session']}" for job in jobs]
        self.bot.send(chat_id, "\n".join(lines))

    def show_log(self, chat_id, argument: str) -> None:
        job = self.jobs.by_id(argument) if argument else None
        if job is None:
            jobs = self.jobs.load()
            job = jobs[0] if jobs else None
        if job is None:
            self.bot.send(chat_id, "Заданий нет. Укажите номер: /log <номер из /jobs>")
            return
        self.bot.send(chat_id, f"Журнал «{job.meta['name']}» ({job.status}):\n\n" + (job.tail(3200) or "пусто"))

    def stop_job(self, chat_id, argument: str) -> None:
        job = self.jobs.by_id(argument) if argument else None
        if job is None:
            self.bot.send(chat_id, "Кого останавливать? /stop <номер из /jobs>")
            return
        self.jobs.stop(job)
        job.meta.update({"status": "stopped", "finished": time.time(),
                         "note": "остановлено вручную"})
        job.save()
        self.bot.send(chat_id, f"⏹ «{job.meta['name']}» остановлено.")

    def show_usage(self, chat_id) -> None:
        self.bot.send(chat_id, self.colab.usage())

    # ────────────────────────────── вход в Colab ──────────────────────────────
    def start_login(self, chat_id) -> None:
        message = self.colab.begin()
        self.pending_login = str(chat_id) if self.colab.session else None
        self.bot.send(chat_id, message, preview=True)

    def finish_login(self, code: str) -> None:
        self.bot.send(self.pending_login or "", self.colab.submit(code))

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
        if target.exists() and target.stat().st_size == size:
            book = target                        # тот же файл уже был: не качаем заново
        else:
            try:
                self.bot.download(document["file_id"], target)
            except Exception as exc:
                self.bot.send(chat_id, f"Не смог скачать файл: {exc}")
                return
            book = target
        job = self.jobs.add(name, chat_id)
        job.meta["book"] = str(book)
        job.save()
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
        try:
            self.jobs.start(job, book, gpu=GPU, drive_dir=DRIVE_DIR, max_hours=SHRINK_HOURS)
        except Exception as exc:
            job.meta.update({"status": "failed", "note": f"не удалось запустить: {exc}"})
            job.save()
            self.report(job)

    def report(self, job) -> None:
        chat_id = job.meta.get("chat_id")
        if not chat_id:
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
                text.append("Ссылок на Drive нет — похоже, доступ не подтверждали; архив лежит в томе.")
            self.bot.send(chat_id, "\n\n".join(text))
        elif job.status == "failed":
            self.bot.send(chat_id, f"⚠️ «{job.meta['name']}» не собралась.\n"
                                   f"Причина: {job.meta.get('note') or 'смотрите журнал'}\n\n"
                                   f"/log {job.meta['session']}")


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
    print(f"[bot] @{app.username} на связи; конвейер {PIPELINE}; данные {DATA}", flush=True)
    print(f"[bot] tmux: {'есть' if app.jobs.has_tmux else 'нет — задания пойдут отдельными процессами'}",
          flush=True)

    if os.environ.get("WEB_ENABLED", "1") not in ("0", "no", "false"):
        import web
        port = int(os.environ.get("WEB_PORT", "8080"))
        page = web.WebApp(app.jobs, max_hours=SHRINK_HOURS, launch=app.launch,
                          stop=app.stop_for_web, login=app.colab)
        web.start_server(page, "0.0.0.0", port)
        outside = os.environ.get("WEB_OUTSIDE_PORT", "")
        where = f"наружу порт {outside}" if outside else "наружу — как указано в ports: compose.yaml"
        print(f"[bot] страница поднялась: внутри контейнера 0.0.0.0:{port}, {where}. "
              f"Открывайте ВНЕШНИЙ порт (в compose это левая часть пары), например "
              f"http://<адрес-машины>:{outside or port}/", flush=True)

    app.loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
