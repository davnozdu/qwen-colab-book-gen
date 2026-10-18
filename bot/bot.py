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
import sys
import threading
import time

import chains
import colab_auth
import settings as bot_settings
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
/logout — сбросить вход в Colab (сменить Google-аккаунт)
/eta — сколько уже сделано и сколько осталось (по сегментам книги)
/usage — остаток и активные машины аккаунта
/help — эта подсказка

Как обрабатывать книги (целиком или с разбивкой на части), по сколько часов резать длинные книги
и копировать ли их на Google Drive — настраивается на странице, в блоке «Настройки».

Параметры запуска: машина %s, папка Drive «%s», резать книги длиннее %s ч.
Меню висит внизу под полем ввода — «Статус», «Задания», «Сколько осталось», «Журнал», «Остаток»,
«Войти в Colab», «Сбросить вход», «Остановить всё», «Помощь»: нажимайте кнопку, команды помнить
не нужно.
""" % (GPU, DRIVE_DIR, f"{SHRINK_HOURS:g}")

# Инлайн-кнопки под сообщением: видны сразу под ответом (в истории чата остаются).
MENU = [[("📊 Статус", "status"), ("⏳ Сколько осталось", "eta")],
        [("📋 Задания", "jobs"), ("📜 Журнал", "log")],
        [("📈 Остаток", "usage"), ("🔑 Войти в Colab", "login")],
        [("🚪 Сбросить вход", "reset_ask"), ("❓ Помощь", "help")],
        [("⏹ Остановить всё", "stop_ask")]]

# Постоянная клавиатура ПОД полем ввода: висит всегда, пока пользователь сам её не свернёт.
# Подпись кнопки приходит обычным текстом, поэтому каждую подпись разбираем в menu_action().
REPLY_MENU = [["📊 Статус", "⏳ Сколько осталось"],
              ["📋 Задания", "📜 Журнал"],
              ["📈 Остаток", "🔑 Войти в Colab"],
              ["🚪 Сбросить вход", "❓ Помощь"],
              ["⏹ Остановить всё"]]

# Подпись кнопки → действие. Ключи без эмодзи и знаков: menu_action() чистит текст до букв,
# поэтому «📊 Статус» и набранное руками «статус» ведут себя одинаково.
REPLY_ACTIONS = {"статус": "status", "задания": "jobs", "журнал": "log", "логи": "log",
                 "остаток": "usage", "войтивcolab": "login", "вход": "login", "входвcolab": "login",
                 "сброситьвход": "reset_ask", "сбросвхода": "reset_ask", "разлогиниться": "reset_ask",
                 "сброситьавторизацию": "reset_ask", "выйти": "reset_ask", "сменитьаккаунт": "reset_ask",
                 "остановитьвсе": "stop_ask", "стоп": "stop_ask", "остановить": "stop_ask",
                 "сколькоосталось": "eta", "осталось": "eta", "прогресс": "eta", "сколькоеще": "eta",
                 "долго": "eta", "скоро": "eta", "помощь": "help", "меню": "menu"}

# Список команд для кнопки «☰» рядом с полем ввода (ставится один раз при запуске).
COMMANDS = [("status", "что делается прямо сейчас"), ("eta", "сколько сделано и сколько осталось"),
            ("jobs", "последние задания"), ("log", "хвост журнала задания"),
            ("stop", "остановить сборку"), ("repeat", "собрать книгу заново"),
            ("delete", "убрать задание из списка"), ("login", "войти в Google Colab"),
            ("logout", "сбросить вход в Colab (сменить аккаунт)"),
            ("usage", "остаток и активные машины"), ("help", "подсказка и меню")]


def menu_action(text: str) -> str:
    """Подпись кнопки постоянного меню → действие («📊 Статус» и «статус» — одно и то же).

    Кнопка ReplyKeyboardMarkup приходит ТЕКСТОМ, поэтому сравнивать подписи надо терпимо:
    эмодзи, регистр, лишние пробелы и «ё» на смысл не влияют. Пустая строка — «это не кнопка».
    """
    key = re.sub(r"[^0-9a-zа-яё]+", "", (text or "").lower()).replace("ё", "е")
    return REPLY_ACTIONS.get(key, "")


def failed_menu(session: str) -> list:
    """Кнопки под отчётом о неудаче — действие относится к ЭТОЙ сборке, а не к последней.

    Сессию вшиваем в данные кнопки (лимит Telegram — 64 байта, хватает с запасом): иначе нажатие
    под старым отчётом случайно повторило бы другую книгу.
    """
    return [[("🔁 Повторить", f"repeat:{session}"), ("📜 Журнал", f"log:{session}")],
            [("🏠 Меню", "menu")]]


def looks_like_code(text: str) -> bool:
    """Код Google длинный (обычно 40+ знаков), без пробелов; «4/0...» тоже подходит."""
    candidate = (text or "").strip()
    return len(candidate) >= 20 and " " not in candidate and "\n" not in candidate


def looks_like_chat_id(value) -> bool:
    """Похоже ли на номер чата Telegram: только цифры (у групп — со знаком минус).

    Пометки вроде «web» — не чат: Telegram отвечает на них «Bad Request: chat not found»,
    и все уведомления задания (включая ссылки на Google Drive) пропадают молча.
    Поэтому такую пометку считаем «чата нет» и подставляем владельца.
    """
    return bool(re.fullmatch(r"-?\d+", str(value or "").strip()))


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
        # Настройки обработки (режим, порог нарезки, Drive) — в томе: переживают обновление
        # образа. Меняются на странице, здесь объект общий со страницей.
        self.settings = bot_settings.Settings(DATA / "settings.json")
        # Цепочки частей: книга, разрезанная на части, и состояние этих частей (тоже в томе).
        self.chains = chains.Chains(DATA)
        self.menu_chats = set()          # чаты, где постоянное меню уже поставлено (на этот запуск)

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
        self.chains_pass()
        for job in reversed(self.jobs.load()):
            if job.status == "queued" and self.jobs.free_slot():
                self.launch(job)

    # ────────────────────────────── части книги ──────────────────────────────
    def start_chain(self, job, book: pathlib.Path) -> None:
        """Режим частей: книга режется ЗДЕСЬ (не на машине) и собирается частями.

        Нарезка — это минуты CPU в контейнере, поэтому машину на неё не тратим, а части кладём
        прямо в том: они переживут и перезапуск бота, и обновление образа.
        """
        chain = chains.pick_for(self.chains, book, job.id,
                                job.meta.get("name") or book.stem,
                                job.meta.get("chat_id") or self.owner_chat(),
                                self.settings.parts_hours)
        job.meta.update({"status": "splitting", "chain": chain.id,
                         "parts_total": 0, "parts_done": 0, "part_index": 0})
        job.save()
        job.log_line(f"режим частей: режу книгу на части по {self.settings.parts_hours:g} ч "
                     f"(в контейнере, машину на это не тратим)")
        self.send_to_job_chat(job, f"✂️ «{job.meta['name']}»: режу на части по "
                                   f"{self.settings.parts_hours:g} ч. Это минуты — потом части пойдут "
                                   f"в облако по очереди, а склейка будет здесь.")
        self.in_background(self.owner_chat(), lambda: self.split_chain(chain, job), "Нарезка на части")

    def split_chain(self, chain, job) -> None:
        """Нарезка и постановка частей в очередь. Идёт в отдельном потоке: минуты CPU."""
        code, plan = chains.shrink_step(sys.executable, PIPELINE, pathlib.Path(chain.meta["book"]),
                                        chain.out, chain.parts_hours, chain.log, env=self.jobs.env)
        if code != 0 or not plan:
            chain.meta["status"] = chains.FAILED
            chain.meta["note"] = "нарезка не удалась — смотрите журнал нарезки"
            chain.save()
            job.meta.update({"status": "failed", "finished": time.time(),
                             "note": "нарезка на части не удалась: смотрите журнал"})
            job.save()
            self.send_to_job_chat(job, f"⚠️ «{job.meta['name']}»: нарезка на части не удалась. "
                                       f"Журнал — на странице, кнопка «журнал».")
            return
        parts = chains.parts_from_plan(plan, pathlib.Path(chain.meta["book"]))
        chain.set_parts(parts)
        chain.log_line(f"частей: {len(parts)} — "
                       + ", ".join(f"{part['index']}:{part['hours']:.2f} ч" for part in parts))
        job.meta.update({"parts_total": len(parts), "parts_done": 0, "part_index": 0})
        job.save()
        for part in chain.parts:
            job_id = self.jobs.add(chain.title_of(part), job.meta.get("chat_id") or "",
                                   pathlib.Path(part["epub"]))
            job_id.meta.update({"chain": chain.id, "part_index": part["index"],
                                "parts_total": len(chain.parts), "parts_done": 0})
            job_id.log_line(f"часть {part['index']} из {len(chain.parts)} книги "
                            f"«{chain.meta.get('name')}» (≈{part['hours']:.2f} ч звука)")
            job_id.save()
            part["job_id"] = job_id.id
        chain.save()
        job.log_line(f"частей: {len(chain.parts)} — поставил в очередь, пойдут по одной")
        self.send_to_job_chat(job, f"✂️ «{job.meta['name']}» разрезана на {len(chain.parts)} "
                                   f"част. Первая часть уже в очереди, дальше пойдут сами.")

    def chains_pass(self) -> None:
        """Двигает цепочки частей: сверяет состояние частей, повторяет упавшую, закрывает книгу."""
        for chain in self.chains.active():
            job = self.jobs.by_id(chain.meta.get("job") or "")
            if job is None:
                continue
            if chain.status == chains.SPLIT:
                continue                                  # нарезка идёт в своём потоке
            changed = False
            for part in chain.parts:
                part_job = self.jobs.by_id(part.get("job_id") or "")
                if part_job is None:
                    continue
                if part_job.status != part.get("status"):
                    part["status"] = part_job.status
                    changed = True
                if part_job.status == chains.PART_DONE and part_job.meta.get("result"):
                    part["result"] = part_job.meta["result"]
            if changed:
                chain.save()
            done = [part for part in chain.parts if part.get("status") == chains.PART_DONE]
            job.meta["parts_done"] = len(done)
            current = next((part for part in chain.parts if part.get("status") == chains.PART_RUNNING),
                            None)
            job.meta["part_index"] = current["index"] if current else 0
            job.save()

            # Упавшую часть повторяем САМИ один раз: чаще всего это «заняты машины» или обрыв
            # сессии. Второй раз не упорствуем — зовём человека, иначе цепочка молотит впустую.
            for part in chain.failed():
                if int(part.get("retries") or 0) < chains.RETRIES:
                    part["retries"] = int(part.get("retries") or 0) + 1
                    part["status"] = chains.PART_QUEUED
                    part_job = self.jobs.by_id(part.get("job_id") or "")
                    if part_job is not None:
                        part_job.meta.update({"status": "queued", "note": "",
                                              "finished": None, "result": ""})
                        part_job.log_line("повтор после сбоя")
                        part_job.save()
                    chain.save()
                    job.log_line(f"часть {part['index']} упала — повторяю её сам (попытка 2)")
                    self.send_to_job_chat(job, f"↻ «{job.meta['name']}», часть {part['index']} "
                                               f"упала — повторяю её сам.")
                    return
                if chain.status != chains.FAILED:
                    reason = (f"часть {part['index']} из {chain.meta.get('total')} упала дважды. "
                              f"Дальше идти нельзя: книга получится с дырой. "
                              f"На странице — «продолжить генерацию».")
                    chain.hold(reason)
                    job.meta.update({"status": "waiting", "note": reason})
                    job.save()
                    job.log_line(f"остановлено: {reason}")
                    self.send_to_job_chat(job, f"⚠️ «{job.meta['name']}»: {reason}")
                    return

            if chain.all_done() and chain.status != chains.DONE:
                self.finish_chain(chain, job)
                return

    def parts_dir(self, chain) -> pathlib.Path:
        """Сырьё частей в томе: переживёт и перезапуск бота, и обновление образа."""
        return DATA / "parts" / str(chain.id)

    def parts_line(self, chain, index) -> str:
        return (f"режим частей: часть {index} из {chain.meta.get('total') or '?'} — на машину "
                f"только синтез, громкость, музыка и сборка будут в контейнере")

    def finish_chain(self, chain, job) -> None:
        """Все части готовы → финал: склейка, громкость по всей книге, музыка, сборка — в контейнере."""
        final = self.jobs.by_id(chain.meta.get("final_job") or "")
        if final is None:
            self.start_glue(chain, job)
            return
        if final.status in ("queued", "running"):
            return                                  # сборка идёт: следим дальше
        if final.status == "done":
            chain.finish()
            result = final.meta.get("result") or ""
            note = (f"книга собрана из {chain.meta.get('total')} частей"
                    + (f": {pathlib.Path(result).name}" if result else "")
                    + ". Сырьё частей осталось в томе: фильтр, громкость или музыку можно переделать "
                      "и собрать книгу заново, не трогая облако.")
            job.meta.update({"status": "done", "finished": time.time(), "note": note, "result": result})
            job.save()
            job.log_line(note)
            self.send_to_job_chat(job, f"✅ «{job.meta['name']}»: {note}")
            return
        reason = (f"сборка книги не удалась: {final.meta.get('note') or 'смотрите журнал сборки'}. "
                  f"Части целы — на странице есть «собрать книгу».")
        chain.hold(reason)
        job.meta.update({"status": "waiting", "note": reason})
        job.save()
        job.log_line(reason)
        self.send_to_job_chat(job, f"⚠️ «{job.meta['name']}»: {reason}")

    def start_glue(self, chain, job) -> None:
        """Запускает сборку книги из готовых частей. Повторный вызов только следит, не запускает второй раз."""
        running = self.jobs.by_id(chain.meta.get("final_job") or "")
        if running is not None and running.status in ("queued", "running"):
            return
        glue = PIPELINE / "glue_book.py"
        parts_dir = self.parts_dir(chain)
        final = self.jobs.add(f"{job.meta.get('name')} — сборка книги",
                              job.meta.get("chat_id") or self.owner_chat(),
                              pathlib.Path(chain.meta["book"]))
        final.meta.update({"chain": chain.id, "glue": True,
                           "parts_total": chain.meta.get("total"), "parts_done": chain.meta.get("total")})
        final.log_line(f"сборка книги из {chain.meta.get('total')} частей: склейка, громкость по всей "
                       f"книге, музыка, m4b и read-along — здесь, в контейнере, без облака")
        final.save()
        chain.meta["final_job"] = final.id
        chain.meta["status"] = chains.GLUING
        chain.save()
        try:
            self.jobs.start_local(final, glue, ["--mode", "final", "--parts-dir", str(parts_dir),
                                                "--book", str(chain.meta["book"]),
                                                "--out", str(self.jobs.results), "--polish", "soft",
                                                "--expect", str(chain.meta.get("total") or 0)])
        except Exception as exc:
            reason = f"сборку книги запустить не вышло: {exc}"
            chain.hold(reason)
            final.meta.update({"status": "failed", "note": reason})
            final.save()
            job.meta.update({"status": "waiting", "note": reason})
            job.save()
            job.log_line(reason)
            self.send_to_job_chat(job, f"⚠️ «{job.meta['name']}»: {reason}")
            return
        note = (f"все {chain.meta.get('total')} части готовы. Собираю книгу здесь: склейка, громкость "
                f"по всей книге, музыка, m4b и read-along — без облака и без GPU, это минуты. "
                f"Следить: «журнал» у строки сборки книги.")
        job.meta.update({"status": "gluing", "note": note})
        job.save()
        job.log_line(note)
        self.send_to_job_chat(job, f"🧩 «{job.meta['name']}»: {note}")

    def glue_chain(self, chain_id: str) -> str:
        """Кнопка «собрать книгу»: финал по готовым частям (в том числе после перезапуска контейнера)."""
        chain = self.chains.by_id(chain_id)
        if chain is None:
            return "Такой книги с частями уже нет."
        if chain.status == chains.DONE:
            return f"«{chain.meta.get('name')}» уже собрана — пересобирать нечего."
        if chain.status == chains.SPLIT:
            return "Сейчас идёт нарезка книги — сборку начнём, когда части будут готовы."
        missing = [str(part["index"]) for part in chain.parts if part.get("status") != chains.PART_DONE]
        if missing:
            return (f"Собирать нечего: части {', '.join(missing)} ещё не готовы. "
                    f"Нажмите «продолжить генерацию».")
        job = self.jobs.by_id(chain.meta.get("job") or "")
        if job is None:
            job = self.jobs.add(f"{chain.meta.get('name')} — сборка книги",
                                chain.meta.get("chat_id") or self.owner_chat(),
                                pathlib.Path(chain.meta["book"]))
            job.meta.update({"chain": chain.id})
            job.save()
            chain.meta["job"] = job.id
        self.start_glue(chain, job)
        return f"Собираю «{chain.meta.get('name')}» из готовых частей — журнал у строки сборки."

    def continue_chain(self, chain_id: str) -> str:
        """Кнопка «продолжить генерацию»: с первой неготовой части, готовые не трогаем."""
        chain = self.chains.by_id(chain_id)
        if chain is None:
            return "Такой книги с частями уже нет."
        job = self.jobs.by_id(chain.meta.get("job") or "")
        if chain.status == chains.DONE and chain.all_done():
            return f"«{chain.meta.get('name')}»: все части уже готовы, продолжать нечего."
        if chain.status == chains.SPLIT or not chain.parts:
            # Нарезка не доделалась (например, бота перезапускали) — начинаем её заново.
            if job is None:
                return "Задание этой книги потерялось — загрузите книгу заново."
            job.meta.update({"status": "splitting", "note": "", "finished": None})
            job.save()
            job.log_line("продолжаю: перезапускаю нарезку на части")
            self.in_background(self.owner_chat(), lambda: self.split_chain(chain, job), "Нарезка на части")
            return f"«{chain.meta.get('name')}»: перезапускаю нарезку на части."
        released = chain.release()
        for part in released:
            part_job = self.jobs.by_id(part.get("job_id") or "")
            if part_job is not None:
                part_job.meta.update({"status": "queued", "note": "", "finished": None})
                part_job.log_line("продолжаю по вашей команде")
                part_job.save()
        if job is not None:
            job.meta.update({"status": "waiting", "note": ""})
            job.save()
            job.log_line("продолжаю с первой неготовой части")
        where = released[0]["index"] if released else (chain.first_unfinished() or {}).get("index")
        return f"«{chain.meta.get('name')}»: продолжаю с части {where}."

    def send_to_job_chat(self, job, text: str) -> None:
        """Сообщение по книге: в чат задания, а если его нет — владельцу."""
        chat_id = str(job.meta.get("chat_id") or "").strip()
        if not looks_like_chat_id(chat_id):
            chat_id = self.owner_chat()
        if not chat_id:
            print(f"[bot] {text}", flush=True)
            return
        try:
            self.bot.send(chat_id, text)
        except Exception as exc:
            print(f"[bot] сообщение о частях не ушло в {chat_id}: {exc}", flush=True)

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
        # Чужим не отвечаем ВООБЩЕ: молчание не подтверждает даже того, что бот существует,
        # а в журнале контейнера остаётся строка — по ней видно, кто стучался.
        if not self.allowed(chat_id):
            print(f"[bot] чужой чат {chat_id}: не отвечаю (ни команд, ни книг, ни входа)", flush=True)
            return

        # Постоянное меню ставим ДО разбора текста: подпись кнопки приходит текстом, и её нельзя
        # принимать ни за код авторизации, ни за книгу.
        action = menu_action(text)
        if action:
            self.install_menu(chat_id)
            self.do_action(chat_id, action)
            return
        self.install_menu(chat_id)          # первый ответ в чате — заодно и меню

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
        """Помощь вместе с ПОСТОЯННЫМ меню: команды помнить не обязательно.

        Клавиатуру здесь ставим reply-меню, а не инлайном: она останется внизу и после того,
        как это сообщение уедет вверх по переписке.
        """
        self.menu_chats.add(str(chat_id))
        self.bot.send(chat_id, (text or HELP).strip(), menu=REPLY_MENU)

    def install_menu(self, chat_id) -> None:
        """Поставить постоянное меню под полем ввода — один раз за запуск бота и один раз на чат.

        Один короткий ответ вместо немого «напиши статус, чтобы увидеть кнопки». Дальше Telegram
        держит клавиатуру сам (is_persistent), пока пользователь не свернёт её; если свернул —
        /start или «Помощь» поставят её заново.
        """
        chat = str(chat_id)
        if chat in self.menu_chats:
            return
        self.menu_chats.add(chat)
        self.bot.send(chat, "Меню закрепил внизу под полем ввода: «Статус», «Задания», «Журнал», "
                            "«Остаток», «Войти в Colab», «Остановить всё», «Помощь». "
                            "Нажимайте кнопку — команды помнить не нужно.", menu=REPLY_MENU)

    def do_action(self, chat_id, data: str) -> None:
        """Что делает кнопка — ОДНО место и для инлайна, и для постоянного меню.

        В кнопках живут только «свои» действия — чужие данные игнорируем.
        """
        if data in ("help", "menu", ""):
            self.send_menu(chat_id)
        elif data == "status":
            self.status(chat_id)
        elif data == "eta":
            self.show_eta(chat_id)
        elif data == "jobs":
            self.list_jobs(chat_id)
        elif data == "log":
            self.show_log(chat_id, "")
        elif data == "login":
            self.start_login(chat_id)
        elif data == "reset_ask":
            # Сброс просит подтверждение: он рвёт вход и гасит машины, случайное нажатие дорого.
            self.bot.send(chat_id,
                          "Сбросить вход в Google Colab?\n"
                          "Токен и состояние CLI отложатся рядом в томе (с меткой времени), машины "
                          "будут остановлены, а следующий вход даст новую ссылку — можно выбрать другой "
                          "Google-аккаунт.\n"
                          "Важно: вход в Colab и подтверждение доступа к Drive проходите под ОДНИМ аккаунтом.",
                          buttons=[[("🚪 Да, сбросить вход", "reset_do"), ("↩️ Отмена", "menu")]])
        elif data == "reset_do":
            self.bot.send(chat_id, "Сбрасываю вход…")
            self.in_background(chat_id, self.colab.reset, "Сброс входа")
        elif data == "usage":
            self.show_usage(chat_id)
        elif data == "stop_ask":                       # остановка «всё» — через подтверждение
            self.bot.send(chat_id, "Остановить всё: текущую сборку и очередь, машины освободить?",
                          buttons=[[("⏹ Да, остановить", "stop_all"), ("↩️ Отмена", "menu")]])
        elif data == "stop_all":
            self.bot.send(chat_id, "Останавливаю всё…")
            self.in_background(chat_id, self.stop_all_jobs, "Остановка")
        elif data.startswith(("repeat:", "log:")):     # из отчёта о неудаче: ровно та сборка
            action, _, session = data.partition(":")
            if self.jobs.by_id(session) is None:
                self.bot.send(chat_id, "Такого задания уже нет.")
                return
            if action == "repeat":
                self.repeat_job(chat_id, session)
            else:
                self.show_log(chat_id, session)
        else:
            self.send_menu(chat_id)

    def on_button(self, query: dict) -> None:
        """Нажали ИНЛАЙН-кнопку под сообщением: делаем то же, что делает команда.

        Чужой чат не обслуживаем, как и в обычных сообщениях. Подпись кнопки постоянного меню
        приходит текстом и разбирается в handle() — оба пути ведут в do_action().
        """
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        data = (query.get("data") or "").strip()
        if chat_id is None:
            return
        if not self.allowed(chat_id):
            self.bot.answer(query.get("id", ""))          # пустой ответ: чужим — ничего
            print(f"[bot] чужой чат {chat_id} нажал кнопку: не отвечаю", flush=True)
            return
        self.bot.answer(query.get("id", ""), "смотрю…")
        self.do_action(chat_id, data)

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
        elif head in ("/logout", "/reset", "/signout"):
            self.do_action(chat_id, "reset_ask")          # тот же путь, что у кнопки «Сбросить вход»
        elif head in ("/eta", "/progress", "/осталось"):
            self.do_action(chat_id, "eta")
        elif head == "/usage":
            self.show_usage(chat_id)
        else:
            self.send_menu(chat_id, "Не знаю такой команды.\n\n" + HELP)

    def status(self, chat_id) -> None:
        jobs = self.jobs.load()
        running = [job for job in jobs if job.status == "running"]
        queued = [job for job in jobs if job.status == "queued"]
        # Книга в режиме частей машину не занимает: её состояние — «режется» или «жду части».
        chains_now = [job for job in jobs if job.status in ("splitting", "waiting")]
        lines = []
        for job in running:
            minutes = (time.time() - float(job.meta.get("started") or time.time())) / 60
            last = [line.strip() for line in job.tail(400).splitlines() if line.strip()]
            lines.append(f"▶️ {job.meta['name']} — {minutes:.0f} мин\n   {last[-1][:120] if last else '…'}")
        for job in chains_now:
            chain = self.chains.by_id(job.meta.get("chain") or "")
            where = chain.summary() if chain else "части"
            note = f"\n   {job.meta['note'][:120]}" if job.meta.get("note") else ""
            lines.append(f"✂️ {job.meta['name']} — {where}{note}")
        for job in queued:
            lines.append(f"⏳ {job.meta['name']} — в очереди")
        head = (f"Сейчас: {len(running) + len(chains_now)} в работе, {len(queued)} в очереди.")
        if not running and not queued and not chains_now:
            head = "Свободно: ничего не собирается."
        self.bot.send(chat_id, head + ("\n\n" + "\n".join(lines) if lines else ""), buttons=MENU)

    def show_eta(self, chat_id) -> None:
        """Сколько сделано и сколько осталось. Замеры копим: со второго раза точнее оценка."""
        jobs = [job for job in self.jobs.load() if job.status in ("running", "queued")]
        jobs.sort(key=lambda job: job.status != "running")     # сначала то, что реально идёт
        if not jobs:
            self.bot.send(chat_id, "Сейчас ничего не собирается — осталось 0 %.",
                          buttons=MENU)
            return
        blocks = []
        for job in jobs:
            state = job.progress()
            if job.status == "queued":
                blocks.append(f"⏳ {job.meta['name']} — в очереди, ещё не начиналось.")
                continue
            minutes = (time.time() - float(job.meta.get("started") or time.time())) / 60
            bar = "▰" * round(state["percent"] / 10) + "▱" * (10 - round(state["percent"] / 10))
            lines = [f"▶️ {job.meta['name']}",
                     f"{bar} {state['percent']} % · стадия {state['stage_index']}/{state['stages']}: "
                     f"{state['stage_title']}"]
            if state["done"] and state["segments"]:
                lines.append(f"сегментов озвучено: {state['done']} из {state['segments']} "
                             f"({round(100 * state['done'] / state['segments'])} %)")
            detail = f"идёт {minutes:.0f} мин"
            if state["measured_rate"]:
                detail += f", скорость {state['measured_rate']} сегм/мин"
            if state["remaining_minutes"]:
                detail += (f" · осталось ≈{state['remaining']}, закончится около {state['eta']}")
            if state.get("part") and state["part"].get("total", 1) > 1:
                part = state["part"]
                where = (f"часть {part['index']} из {part['total']}" if part.get("index")
                         else f"готово частей: {part.get('done', 0)} из {part['total']}")
                detail += f" · книга: {where}"
            lines.append(detail)
            # Замер для скорости: со второго запроса оценка считается по факту, а не по модели.
            samples = [pair for pair in (job.meta.get("progress_samples") or []) if len(pair) == 2]
            samples.append([time.time(), state["done"] or 0])
            job.meta["progress_samples"] = samples[-20:]
            try:
                job.save()
            except OSError:
                pass
            blocks.append("\n".join(lines))
        self.bot.send(chat_id, "\n\n".join(blocks)
                      + "\n\nОценка по сегментам книги: чем дальше, тем точнее.", buttons=MENU)

    def list_jobs(self, chat_id) -> None:
        jobs = self.jobs.load()[:10]
        if not jobs:
            self.bot.send(chat_id, "Заданий пока не было.", buttons=MENU)
            return
        icons = {"done": "✅", "failed": "⚠️", "running": "▶️", "queued": "⏳", "stopped": "⏹",
                 "splitting": "✂️", "waiting": "🧩"}
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
        """Гасит задание и освобождает его машину Colab.

        Если это книга в режиме частей, останавливаем и её части: иначе они поедут сами дальше —
        очередь видит их как обычные задания, и «остановил» превратилось бы в «остановил только
        то, что идёт сейчас».
        """
        chain = self.chains.by_id(job.meta.get("chain") or "")
        if chain is not None and not job.meta.get("part_index"):
            for part in chain.parts:
                part_job = self.jobs.by_id(part.get("job_id") or "")
                if part_job is None or part_job.status in ("done", "failed", "stopped"):
                    continue
                self.jobs.stop(part_job)
                part_job.meta.update({"status": "stopped", "finished": time.time(),
                                      "note": "остановлено вручную (вся книга)"})
                part_job.save()
                part["status"] = "stopped"
            chain.hold("остановлено вручную")
            chain.meta["status"] = "stopped"
            chain.save()
            job.meta.update({"status": "stopped", "finished": time.time(),
                             "note": "остановлено вручную"})
            job.save()
            return "части остановлены, нарезка осталась в томе"
        self.jobs.stop(job)
        job.meta.update({"status": "stopped", "finished": time.time(), "note": "остановлено вручную"})
        job.save()
        return f"машина {job.meta['session']} освобождена"

    def stop_all_jobs(self) -> str:
        """Останавливает всё: текущую сборку, очередь, части и машины Colab на них."""
        jobs = [job for job in self.jobs.load()
                if job.status in ("running", "queued", "splitting", "waiting")]
        if not jobs:
            return "Останавливать нечего: сейчас ничего не собирается."
        # Сначала книги с частями: их остановка сама гасит и части, и цепочку.
        jobs.sort(key=lambda job: bool(job.meta.get("part_index")))
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
        # Задание могло прийти со страницы — там чата нет (или стоит пометка «web»).
        # Подставляем владельца, иначе об итоге и о стадиях никто не узнает: Telegram на
        # такой «чат» отвечает «chat not found», и ссылки на Google Drive теряются.
        if not looks_like_chat_id(job.meta.get("chat_id")):
            owner = self.owner_chat()
            if owner:
                job.meta["chat_id"] = owner
                job.save()
        try:
            # Часть книги идёт на машину ТОЛЬКО на синтез: сырьё вернётся в том, а громкость, музыка
            # и сборка будут здесь. Книга целиком в режиме частей машину не занимает вовсе.
            if job.meta.get("part_index") and job.meta.get("chain"):
                chain = self.chains.by_id(job.meta["chain"])
                if chain is None:
                    raise RuntimeError("книга с частями больше не найдена — начните её заново")
                job.log_line(self.parts_line(chain, job.meta["part_index"]))
                self.jobs.start(job, book, gpu=GPU, drive_dir=DRIVE_DIR, max_hours=SHRINK_HOURS,
                                extra=["--parts-raw", "--part-index", str(job.meta["part_index"]),
                                       "--parts-dir", str(self.parts_dir(chain))])
                return
            if self.settings.mode == bot_settings.MODE_PARTS and not job.meta.get("part_index"):
                self.start_chain(job, book)
                return
            job.log_line(self.settings.job_line())
            self.jobs.start(job, book, gpu=GPU, drive_dir=DRIVE_DIR, max_hours=SHRINK_HOURS)
        except Exception as exc:
            job.meta.update({"status": "failed", "note": f"не удалось запустить: {exc}"})
            job.save()
            self.report(job)

    def report(self, job) -> None:
        # Пишем владельцу и для заданий, запущенных со страницы (у них нет своего чата).
        chat_id = str(job.meta.get("chat_id") or "").strip()
        if not looks_like_chat_id(chat_id):
            chat_id = self.owner_chat()
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
                search = job.drive_search_link()
                text.append("Ссылок на Drive нет — похоже, доступ не подтверждали; "
                            "архив лежит в томе."
                            + (f"\nПоиск на Drive по имени книги: {search}" if search else "")
                            + (f"\nЖурнал: {note}" if note else ""))
            self.bot.send(chat_id, "\n\n".join(text), buttons=MENU)
        elif job.status == "failed":
            self.bot.send(chat_id, f"⚠️ «{job.meta['name']}» не собралась.\n"
                                   f"{job.meta.get('note') or 'Причина неизвестна — смотрите журнал.'}\n\n"
                                   f"Повторить: /repeat {job.meta['session']} · журнал: "
                                   f"/log {job.meta['session']}", buttons=failed_menu(job.meta["session"]))


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
    # Команды видны кнопкой «☰» у поля ввода: без списка они есть, но о них надо помнить.
    print("список команд у поля ввода: " + ("поставлен" if app.bot.set_commands(COMMANDS)
                                            else "не поставился"), flush=True)
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
    print(f"[bot] владелец: {app.owner_chat() or 'пока не определён (первый написавший станет владельцем)'}"
          f"; остальным не отвечаю", flush=True)
    print(f"[bot] tmux: {'есть' if app.jobs.has_tmux else 'нет — задания пойдут отдельными процессами'}",
          flush=True)

    if os.environ.get("WEB_ENABLED", "1") not in ("0", "no", "false"):
        import web
        port = int(os.environ.get("WEB_PORT", "8090"))
        page = web.WebApp(app.jobs, max_hours=SHRINK_HOURS, launch=app.launch,
                          stop=app.stop_for_web, login=app.colab, notify=app.notify_owner,
                          settings=app.settings, chains=app.chains,
                          continue_chain=app.continue_chain, glue=app.glue_chain)
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
