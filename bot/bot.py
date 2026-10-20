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
import housekeeping
import llm
import voices as voice_packs
import profiles as colab_profiles
import settings as bot_settings
from jobs import Jobs, diagnose, safe_stem
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
LAUNCH_LOCK = threading.RLock()     # запуск заданий: очередь бота и страница работают в разных потоках
SWEEP_SECONDS = 3600                # уборка томов — раз в час (и при старте)
VOICES_WAIT = 60                    # «старые голоса или с нуля?» — столько секунд ждём ответа
FAILS_PER_PROFILE = 2               # столько сбоев подряд на одном профиле — и переходим на следующий

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
/profiles — профили Google Colab: войти, переключить, выйти, удалить
/voices — многоголосая озвучка: вкл/выкл, поставщик LLM, ключ, модель
/books — последние книги: вся книга заново, продолжить, удалить голоса
/eta — сколько уже сделано и сколько осталось (по сегментам книги)
/usage — остаток и активные машины аккаунта
/help — эта подсказка

Как обрабатывать книги (целиком или с разбивкой на части), по сколько часов резать длинные книги
и копировать ли их на Google Drive — настраивается на странице, в блоке «Настройки».

Параметры запуска: машина %s, папка Drive «%s», резать книги длиннее %s ч.
Меню висит внизу под полем ввода — «Статус», «Задания», «Сколько осталось», «Журнал», «Остаток»,
«Войти в Colab», «Сбросить вход», «Профили», «Остановить всё», «Помощь»: нажимайте кнопку,
команды помнить не нужно.

Профили — несколько аккаунтов Google Colab. Если сборка падает дважды подряд, бот сам переходит
на следующий профиль; прошёл все — ставит книгу на паузу и пишет об этом.
""" % (GPU, DRIVE_DIR, f"{SHRINK_HOURS:g}")

# Инлайн-кнопки под сообщением: видны сразу под ответом (в истории чата остаются).
MENU = [[("📊 Статус", "status"), ("⏳ Сколько осталось", "eta")],
        [("📋 Задания", "jobs"), ("📜 Журнал", "log")],
        [("📈 Остаток", "usage"), ("🔑 Войти в Colab", "login")],
        [("🚪 Сбросить вход", "reset_ask"), ("👥 Профили", "profiles")],
        [("🎭 Голоса", "voices"), ("📚 Книги", "books")],
        [("⏹ Остановить всё", "stop_ask"), ("❓ Помощь", "help")]]

# Постоянная клавиатура ПОД полем ввода: висит всегда, пока пользователь сам её не свернёт.
# Подпись кнопки приходит обычным текстом, поэтому каждую подпись разбираем в menu_action().
REPLY_MENU = [["📊 Статус", "⏳ Сколько осталось"],
              ["📋 Задания", "📜 Журнал"],
              ["📈 Остаток", "🔑 Войти в Colab"],
              ["🚪 Сбросить вход", "👥 Профили"],
              ["🎭 Голоса", "📚 Книги"],
              ["⏹ Остановить всё", "❓ Помощь"]]

# Подпись кнопки → действие. Ключи без эмодзи и знаков: menu_action() чистит текст до букв,
# поэтому «📊 Статус» и набранное руками «статус» ведут себя одинаково.
REPLY_ACTIONS = {"статус": "status", "задания": "jobs", "журнал": "log", "логи": "log",
                 "остаток": "usage", "войтивcolab": "login", "вход": "login", "входвcolab": "login",
                 "сброситьвход": "reset_ask", "сбросвхода": "reset_ask", "разлогиниться": "reset_ask",
                 "сброситьавторизацию": "reset_ask", "выйти": "reset_ask", "сменитьаккаунт": "reset_ask",
                 "остановитьвсе": "stop_ask", "стоп": "stop_ask", "остановить": "stop_ask",
                 "сколькоосталось": "eta", "осталось": "eta", "прогресс": "eta", "сколькоеще": "eta",
                 "долго": "eta", "скоро": "eta", "помощь": "help", "меню": "menu",
                 "профили": "profiles", "профиль": "profiles", "аккаунты": "profiles",
                 "переключитьпрофиль": "profiles", "голоса": "voices", "многоголосие": "voices",
                 "llm": "voices", "книги": "books", "книга": "books"}

# Список команд для кнопки «☰» рядом с полем ввода (ставится один раз при запуске).
COMMANDS = [("status", "что делается прямо сейчас"), ("eta", "сколько сделано и сколько осталось"),
            ("jobs", "последние задания"), ("log", "хвост журнала задания"),
            ("stop", "остановить сборку"), ("repeat", "собрать книгу заново"),
            ("delete", "убрать задание из списка"), ("login", "войти в Google Colab"),
            ("logout", "сбросить вход в Colab (сменить аккаунт)"),
            ("profiles", "профили Google Colab"),
            ("voices", "многоголосая озвучка и LLM"),
            ("books", "книги: заново, продолжить, голоса"),
            ("usage", "остаток и активные машины"), ("help", "подсказка и меню")]


def _number(data: str) -> int:
    """Номер профиля из данных кнопки «profile_use:2»."""
    tail = data.partition(":")[2]
    return int(tail) if tail.isdigit() else 0


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
        self.profiles = None             # профили Colab: вход идёт в активный, адрес узнаём после входа

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

    def where(self) -> str:
        """В какой профиль идёт вход — чтобы было видно, под каким аккаунтом подтверждать."""
        if self.profiles is None or not self.profiles.enabled:
            return ""
        return f" (профиль {self.profiles.active()})"

    def drop(self) -> None:
        """Закрыть начатый вход: профиль переключили, а терминал CLI ждёт код для прежнего."""
        if self.session:
            self.session.close()
            self.session, self.url = None, ""
            self.note = "вход прежнего профиля закрыт"

    def begin(self) -> str:
        if self.session and self.session.expired():       # протухшую попытку закрываем
            self.session.close()
            self.session, self.url = None, ""
            self.note = "прежняя попытка входа просрочена"
        if not self.session and self.login.terminal_logged_in():
            self.note = "вход есть"
            return f"Вход в Colab{self.where()} уже есть — авторизоваться заново не нужно."
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
        return (f"Вход в Google Colab{self.where()}\n\n1. Откройте ссылку (можно с телефона):\n" + url +
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
            account = ""
            if self.profiles is not None:
                email = self.profiles.lookup(self.profiles.active())
                account = f" Аккаунт: {email}." if email else ""
            return f"✅ Вход{self.where()} выполнен, token.json сохранён в томе конфигов.{account}"
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
        # Профили Colab: папка CLI — ссылка на папку активного профиля; число задаётся в админке.
        self.profiles = colab_profiles.Profiles(pathlib.Path(TOKEN_FILE).parent,
                                                count=lambda: self.settings.profiles)
        self.colab.profiles = self.profiles
        # Пакеты голосов многоголосых книг (разметка ролей LLM + образцы героев) — в томе.
        self.voice_packs = voice_packs.VoicePacks(DATA)
        self.awaiting_key = {}           # чат → поставщик LLM, чей ключ ждём следующим сообщением
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
        self.watchdog()
        self.housekeeping()
        for job in self.jobs.refresh().values():
            if job.meta.get("cast"):
                continue                         # «голоса» решает проход по книгам ниже
            if self.can_failover(job):
                self.after_failure(job)
            else:
                self.report(job)
        self.resume_after_login()
        self.voices_timeouts()
        self.casting_pass()
        if self.profiles.enabled and self.profiles.active() > self.profiles.count() \
                and not self.machine_busy():
            self.profiles.fit_count()            # число профилей уменьшили на странице
        self.chains_pass()
        for job in self.queue_order(self.jobs.load()):
            if self.jobs.free_slot():
                self.launch(job)

    @staticmethod
    def queue_order(jobs) -> list:
        """Ждущие задания — от старых к новым, части одной книги — по номеру части.

        Номер задания — секунда создания плюс случайный хвост, а части одной книги создаются в одну
        секунду: по номеру очередь однажды запустила вторую часть раньше первой. Порядок даёт точное
        время создания, а при совпадении — номер части.
        """
        queued = [job for job in jobs if job.status == "queued"]
        return sorted(queued, key=lambda job: (float(job.meta.get("created") or 0),
                                               int(job.meta.get("part_index") or 0), job.id))

    def housekeeping(self, force: bool = False) -> list:
        """Уборка томов раз в час: старые задания, книги по частям, сырьё, архивы, журналы CLI."""
        now = time.time()
        if not force and now - float(getattr(self, "last_sweep", 0)) < SWEEP_SECONDS:
            return []
        self.last_sweep = now
        settings = getattr(self, "settings", None)
        days = settings.keep_days if settings is not None else bot_settings.DEFAULTS["keep_days"]
        try:
            done = housekeeping.sweep(DATA, pathlib.Path(TOKEN_FILE).parent.parent, self.jobs,
                                      self.chains, days, now)
        except Exception as exc:                 # уборка не должна ронять бота
            print(f"[bot] уборка не удалась: {exc}", flush=True)
            return []
        if done:
            print(f"[bot] уборка (хранить {days} дн.): " + ", ".join(done), flush=True)
        return done

    def activity(self, job) -> float:
        """Последний признак жизни. Сборка книги в контейнере кодирует звук ffmpeg-ом молча
        (журнал минутами не растёт), поэтому для неё движение — это и растущие файлы склейки."""
        moment = job.last_activity()
        chain = self.chains.by_id(job.meta.get("chain") or "") if job.meta.get("glue") else None
        if chain is not None:
            work = self.parts_dir(chain) / "glue"
            try:
                for path in work.rglob("*"):
                    moment = max(moment, path.stat().st_mtime)
            except OSError:
                pass
        return moment

    def watchdog(self) -> None:
        """Сторож зависаний: у идущей сборки журнал растёт (сегменты, стадии). Не растёт дольше
        порога из админки — машина, скорее всего, зависла: гасим её, освобождаем ускоритель и
        пишем предупреждение. Задание становится упавшим: обычная книга получает кнопку «повторить»,
        часть книги бот повторит сам (а после второго раза спросит), сборка книги — «собрать книгу».
        """
        settings = getattr(self, "settings", None)
        limit = settings.stall_minutes if settings is not None else 0
        if not limit:
            return
        now = time.time()
        for job in self.jobs.load():
            if job.status != "running":
                continue
            idle = now - self.activity(job)
            if idle < limit * 60:
                continue
            note = (f"нет движения {idle / 60:.0f} мин (порог {limit} мин) — похоже, машина зависла: "
                    f"остановил её и освободил ускоритель")
            job.log_line("сторож: " + note)
            try:
                self.jobs.stop(job)
            except Exception as exc:
                job.log_line(f"сторож: остановить не вышло ({exc})")
            job.meta.update({"status": "failed", "finished": now, "note": note, "stalled": True})
            job.save()
            self.send_to_job_chat(job, f"⏱ «{job.meta['name']}»: {note}.")
            if self.can_failover(job):
                self.after_failure(job)          # повтор, следующий профиль или пауза — там же

    # ────────────────────────────── профили Colab ──────────────────────────────
    def machine_busy(self) -> bool:
        """Идёт ли сборка на машине Colab: под ней профиль не меняем — её сессия в его папке."""
        return any(not job.meta.get("local") for job in self.jobs.live_running())

    def profile_action(self, action: str, number: int = 0) -> str:
        """Действия с профилями — одно место для Telegram и страницы."""
        profiles = self.profiles
        if not profiles.enabled:
            return "Профили недоступны: вход Colab хранится не в ~/.config/colab-cli."
        busy = ("Сейчас идёт сборка на машине Colab — профиль под ней менять нельзя: её сессия "
                "живёт в папке активного профиля. Дождитесь конца или остановите сборку.")
        if action == "add":
            count = self.settings.profiles + 1
            if count > bot_settings.PROFILES_MAX:
                return f"Профилей не больше {bot_settings.PROFILES_MAX}."
            self.settings.set_profiles(count)
            profiles.ensure(count)
            return f"Добавлен профиль {count}. Войдите в него кнопкой «войти» у профиля {count}."
        if action == "logout_all":
            if self.machine_busy():
                return busy
            self.colab.drop()
            return "\n".join(profiles.logout(number) for number in profiles.numbers())
        number = int(number or 0)
        if not 1 <= number <= profiles.count():
            return f"Профиля {number} нет: их {profiles.count()}."
        switching = action in ("activate", "login") and number != profiles.active()
        if (switching or action == "delete" or (action == "logout" and number == profiles.active())) \
                and self.machine_busy():
            return busy
        if switching:
            self.colab.drop()
            profiles.activate(number)
        if action == "activate":
            email = profiles.lookup(number) if profiles.signed_in(number) else ""
            return (f"Активен профиль {number}" + (f" ({email})" if email else "")
                    + (" — вход есть." if profiles.signed_in(number)
                       else " — входа нет: нажмите «войти» у этого профиля."))
        if action == "login":
            return self.colab.begin()
        if action == "logout":
            if number == profiles.active():
                self.colab.drop()
            return profiles.logout(number)
        if action == "delete":
            self.colab.drop()
            message, count = profiles.delete(number)
            if count != self.settings.profiles:
                self.settings.set_profiles(count)
            return message
        return "Неизвестное действие с профилем."

    def profile_label(self, number: int = 0) -> str:
        """«профиль 2 (name@gmail.com)»: адрес узнаём у Google один раз и держим в папке профиля."""
        number = number or self.profiles.active()
        if self.profiles.signed_in(number):
            self.profiles.lookup(number, timeout=10)
        return self.profiles.label(number)

    def announce_profile(self, job) -> None:
        """Перед запуском на машине: в журнал и в Telegram — под каким аккаунтом идёт сборка."""
        number = self.profiles.active()
        label = self.profile_label(number)
        job.meta["profile"] = number
        job.save()
        job.log_line(f"Google Colab: {label}")
        if self.profiles.enabled:
            what = (f"часть {job.meta['part_index']}" if job.meta.get("part_index") else "сборка")
            self.send_to_job_chat(job, f"👤 «{job.meta['name']}»: {what} идёт под {label}.")

    def can_failover(self, job) -> bool:
        """Отдельная книга, которая реально уходила на машину и упала: ей — повтор и профили.

        Части книги повторяет проход по цепочкам, а то, что не запустилось вовсе (файл пропал),
        повторять бессмысленно — это не сбой аккаунта.
        """
        return (job.status == "failed" and bool(job.meta.get("started"))
                and not job.meta.get("chain") and not job.meta.get("local"))

    def requeue(self, job, line: str) -> None:
        job.meta.update({"status": "queued", "note": "", "finished": None, "result": ""})
        job.save()
        job.log_line(line)

    def next_profile(self, tried) -> tuple:
        """Следующий профиль по кругу: ("switch", n) — вход есть, ("login", n) — входа нет,
        ("exhausted", 0) — круг пройден. На найденный профиль переключаемся сразу: так кнопка
        «войти» поведёт именно в него."""
        number = self.profiles.next_after(tried) if self.profiles.enabled else 0
        if not number:
            return "exhausted", 0
        self.colab.drop()
        self.profiles.activate(number)
        return ("switch" if self.profiles.signed_in(number) else "login"), number

    def after_failure(self, job) -> None:
        """Сборка упала: второй раз на том же профиле, затем следующий профиль, затем пауза.

        Круг по профилям один: прошли все — книга встаёт на паузу и ждёт «продолжить». Профиль
        без входа тоже ставит паузу: бот просит войти в него и продолжает сам, как только вход есть.
        """
        name, session = job.meta["name"], job.meta["session"]
        reason = job.meta.get("note") or "причина в журнале"
        current = self.profiles.active()
        label = self.profile_label(current)
        failures = int(job.meta.get("profile_failures") or 0) + 1
        if failures < FAILS_PER_PROFILE:
            job.meta["profile_failures"] = failures
            self.requeue(job, f"сбой на {label} ({failures} из {FAILS_PER_PROFILE}): {reason} — повторяю")
            self.send_to_job_chat(job, f"↻ «{name}» упала на {label}.\nПричина: {reason}\n"
                                       f"Повторяю на том же профиле.")
            return
        tried = list(job.meta.get("profiles_tried") or []) + [current]
        job.meta.update({"profile_failures": 0, "profiles_tried": tried})
        decision, number = self.next_profile(tried)
        if decision == "switch":
            following = self.profile_label(number)
            self.requeue(job, f"на {label} упала {FAILS_PER_PROFILE} раза — перехожу на {following}")
            self.send_to_job_chat(job, f"🔀 «{name}» упала {FAILS_PER_PROFILE} раза на {label}.\n"
                                       f"Причина: {reason}\nПерехожу на {following} и повторяю.")
            return
        job.meta.update({"status": "paused", "finished": time.time()})
        if decision == "login":
            job.meta.update({"await_login": number,
                             "note": f"ждёт входа в профиль {number}; последняя причина: {reason}"})
            job.save()
            job.log_line(f"на {label} упала {FAILS_PER_PROFILE} раза; следующий профиль {number} без входа — пауза")
            self.send_to_job_chat(job, f"⏸ «{name}» упала {FAILS_PER_PROFILE} раза на {label}.\n"
                                       f"Причина: {reason}\n\nСледующий — профиль {number}, но в него не "
                                       f"выполнен вход. Войдите — и сборка продолжится сама.",
                                  buttons=[[(f"🔑 Войти в профиль {number}", f"profile_login:{number}")],
                                           [("▶️ Продолжить", f"resume:{session}"),
                                            ("📜 Журнал", f"log:{session}")]])
            return
        job.meta["note"] = f"пауза: не справился ни один профиль; последняя причина: {reason}"
        job.save()
        job.log_line(f"пауза: упала на всех профилях ({', '.join(map(str, tried))})")
        self.send_to_job_chat(job, f"⏸ «{name}»: пауза. Сборка упала на всех профилях "
                                   f"({', '.join(map(str, tried))}) по {FAILS_PER_PROFILE} раза.\n"
                                   f"Последняя причина: {reason}\n\n«Продолжить» — пройду по профилям заново.",
                              buttons=[[("▶️ Продолжить", f"resume:{session}"), ("📜 Журнал", f"log:{session}")],
                                       [("🏠 Меню", "menu")]])

    def resume_job(self, job_id: str) -> str:
        """«Продолжить» книгу на паузе: круг по профилям начинается заново, с активного."""
        job = self.jobs.by_id(job_id)
        if job is None:
            return "Такого задания уже нет."
        if job.status != "paused":
            return f"«{job.meta['name']}» не на паузе — продолжать нечего."
        for key in ("profile_failures", "profiles_tried", "await_login"):
            job.meta.pop(key, None)
        self.requeue(job, "продолжаю по вашей команде")
        return f"▶️ «{job.meta['name']}»: продолжаю под {self.profile_label()}."

    def resume_after_login(self) -> None:
        """Вошли в профиль, которого ждала книга (или цепочка частей), — продолжаем сами."""
        if not self.profiles.enabled:
            return
        active = self.profiles.active()
        if not self.profiles.signed_in(active):
            return
        for job in self.jobs.load():
            if job.status == "paused" and int(job.meta.get("await_login") or 0) == active:
                job.meta.pop("await_login", None)
                self.requeue(job, f"вход в профиль {active} есть — продолжаю")
                self.send_to_job_chat(job, f"▶️ «{job.meta['name']}»: вход есть, продолжаю под "
                                           f"{self.profile_label(active)}.")
        for chain in self.chains.active():
            if int(chain.meta.get("await_login") or 0) == active:
                chain.meta.pop("await_login", None)
                chain.save()
                message = self.continue_chain(chain.id, keep_tried=True)
                job = self.jobs.by_id(chain.meta.get("job") or "")
                if job is not None:
                    self.send_to_job_chat(job, f"▶️ Вход в профиль {active} есть. {message}")

    def show_voices(self, chat_id, text: str = "") -> None:
        """«🎭 Голоса» в Telegram — те же настройки, что на странице: режим, поставщик, ключ, модель."""
        settings = self.settings
        provider = settings.llm_provider
        key = settings.llm_key
        lines = [f"Озвучка: {bot_settings.VOICES[settings.voices]}.",
                 f"LLM: {llm.PROVIDERS[provider]} · модель {settings.llm_model} · ключ "
                 + (llm.masked(key) if key else "не задан"),
                 "Многоголосая книга идёт на двух машинах по очереди: сначала разметка ролей и голоса "
                 "героев, потом озвучка. Без ключа LLM книга озвучивается одним голосом."]
        other = "deepseek" if provider == "ollama" else "ollama"
        rows = [[("🎭 Многоголосая: " + ("вкл → выключить" if settings.voices == "multi" else "выкл → включить"),
                  "voices_toggle")],
                [(f"🔁 Поставщик: {llm.PROVIDERS[other]}", f"llm_provider:{other}")],
                [("🔑 Ввести ключ", "llm_key"), ("📋 Считать модели", "llm_models")],
                [("🏠 Меню", "menu")]]
        self.bot.send(chat_id, (text + "\n\n" if text else "") + "\n".join(lines), buttons=rows)

    def refresh_models(self) -> str:
        """«Считать модели» со страницы: список поставщика по ключу — в настройки, для выбора."""
        provider = self.settings.llm_provider
        try:
            names = llm.list_models(provider, self.settings.llm_key)
        except ValueError as exc:
            return f"Модели не считались: {exc}"
        self.settings.change(llm_models=dict(self.settings.values.get("llm_models") or {}, **{provider: names}))
        mark = "есть" if self.settings.llm_model in names else "НЕТ в списке — выберите другую"
        return (f"{llm.PROVIDERS[provider]}: моделей {len(names)}; выбранная {self.settings.llm_model} — {mark}. "
                f"Выберите модель в настройках и сохраните.")

    def show_models(self, chat_id) -> None:
        provider, key = self.settings.llm_provider, self.settings.llm_key
        try:
            names = llm.list_models(provider, key)
        except ValueError as exc:
            self.show_voices(chat_id, f"Модели не считались: {exc}")
            return
        self.settings.change(llm_models=dict(self.settings.values.get("llm_models") or {}, **{provider: names}))
        current = self.settings.llm_model
        rows = [[(("✅ " if name == current else "") + name, f"llm_model:{name}")] for name in names[:40]
                if len(f"llm_model:{name}".encode()) <= 64]
        rows.append([("↩️ Назад", "voices")])
        self.bot.send(chat_id, f"Модели {llm.PROVIDERS[provider]}: {len(names)}. Выберите:", buttons=rows)

    def accept_llm_key(self, chat_id, text: str, message_id=None) -> None:
        provider = self.awaiting_key.pop(str(chat_id), self.settings.llm_provider)
        self.settings.change(**{f"{provider}_key": text.strip()})
        if message_id is not None:
            try:                                  # ключ не должен висеть в переписке
                self.bot.call("deleteMessage", {"chat_id": str(chat_id), "message_id": str(message_id)})
            except Exception:
                pass
        self.show_voices(chat_id, f"Ключ {llm.PROVIDERS[provider]} сохранён ({llm.masked(text)}). "
                                  f"Сообщение с ключом удалено из переписки.")

    def show_profiles(self, chat_id, text: str = "") -> None:
        """Профили в Telegram: у каждого «войти», «выбрать», «выйти», «удалить»."""
        profiles = self.profiles
        if not profiles.enabled:
            self.bot.send(chat_id, "Профили недоступны: вход Colab хранится не в ~/.config/colab-cli.")
            return
        rows = []
        for number in profiles.numbers():
            rows.append([(f"🔑 Войти {number}", f"profile_login:{number}"),
                         (f"▶️ Выбрать {number}", f"profile_use:{number}"),
                         (f"🚪 Выйти {number}", f"profile_logout:{number}"),
                         (f"🗑 {number}", f"profile_delete_ask:{number}")])
        rows.append([("➕ Добавить профиль", "profile_add"), ("🚪 Выйти из всех", "profile_logout_all_ask")])
        rows.append([("🏠 Меню", "menu")])
        head = (text + "\n\n" if text else "") + "Профили Google Colab (▶️ — активный):\n"
        self.bot.send(chat_id, head + "\n".join(profiles.lines())
                      + f"\n\nСборка падает {FAILS_PER_PROFILE} раза подряд — бот сам переходит на "
                        f"следующий профиль; прошёл все — пауза.", buttons=rows)

    # ────────────────────────────── многоголосая книга ──────────────────────────────
    WAITING = object()                   # книга ждёт пакет голосов: озвучка — после первой машины

    def voice_pack_for(self, job, book: pathlib.Path):
        """Пакет голосов для книги: путь, None (один голос) или WAITING (поставлено задание «голоса»).

        Многоголосие требует LLM: нет ключа — книга озвучивается одним голосом, с пояснением.
        Пакет хранится по хешу книги, поэтому повтор той же книги машину на разметку не тратит.
        """
        if self.settings.voices != "multi" or job.meta.get("single_voice"):
            return None
        if job.meta.get("voice_pack") and pathlib.Path(job.meta["voice_pack"]).is_file():
            return pathlib.Path(job.meta["voice_pack"])
        ready = self.voice_packs.pack(book)
        if ready is not None and job.meta.get("ask_voices") and not job.meta.get("voices_choice"):
            # Повторная сборка, а голоса уже есть: спрашиваем; молчание минуту — голоса с нуля.
            job.meta.update({"status": "choice_voices", "voices_deadline": time.time() + VOICES_WAIT,
                             "note": "ждёт ответа: старые голоса или с нуля"})
            job.save()
            job.log_line("голоса этой книги уже есть — спрашиваю: использовать их или создать заново")
            self.send_to_job_chat(job, f"🎭 «{job.meta['name']}»: голоса этой книги уже готовы. Использовать "
                                       f"их или создать с нуля (заново разметка ролей и голоса)?\n"
                                       f"Если ответа не будет {VOICES_WAIT} с — создаю с нуля.",
                                  buttons=[[("♻️ Старые голоса", f"voices_keep:{job.id}"),
                                            ("🆕 С нуля", f"voices_new:{job.id}")]])
            return self.WAITING
        if ready is not None and job.meta.get("voices_choice") == "new" and not job.meta.get("voices_deleted"):
            self.voice_packs.delete(ready.parent.name)
            job.meta["voices_deleted"] = True
            job.save()
            job.log_line("голоса удалены по выбору «с нуля» — создаю заново")
            ready = None
        if ready is not None:
            job.meta["voice_pack"] = str(ready)
            job.save()
            job.log_line(f"многоголосая: беру готовый пакет голосов этой книги ({ready.name})")
            return ready
        if not self.settings.llm_key:
            reason = (f"многоголосая озвучка включена, но нет ключа "
                      f"{llm.PROVIDERS[self.settings.llm_provider]} — озвучиваю одним голосом")
            job.meta["single_voice"] = reason
            job.save()
            job.log_line(reason)
            self.send_to_job_chat(job, f"ℹ️ «{job.meta['name']}»: {reason}. Ключ задаётся в настройках "
                                       f"(страница или «🎭 Голоса» в Telegram).")
            return None
        cast = self.jobs.add(f"{job.meta['name']} — голоса", job.meta.get("chat_id") or "", book)
        cast.meta.update({"cast": True, "parent": job.id, "pack_dir": str(self.voice_packs.folder(book))})
        cast.save()
        cast.log_line(f"многоголосая книга «{job.meta['name']}»: первая машина — разметка ролей "
                      f"({self.settings.llm_provider}, {self.settings.llm_model}) и образцы голосов")
        job.meta.update({"status": "casting", "cast_job": cast.id,
                         "note": "первая машина: разметка ролей и голоса героев"})
        job.save()
        job.log_line(f"многоголосая: сначала голоса (задание {cast.meta['session']}), потом озвучка")
        self.send_to_job_chat(job, f"🎭 «{job.meta['name']}»: многоголосая книга. Сначала одна машина "
                                   f"размечает роли ({llm.PROVIDERS[self.settings.llm_provider]}, "
                                   f"{self.settings.llm_model}) и создаёт голоса героев, потом другая озвучивает.")
        return self.WAITING

    def choose_voices(self, job_id: str, keep: bool, why: str = "") -> str:
        """Ответ на «старые голоса или с нуля» — из Telegram, со страницы или по истечении минуты."""
        job = self.jobs.by_id(job_id)
        if job is None:
            return "Такого задания уже нет."
        if job.status != "choice_voices":
            return f"«{job.meta['name']}»: уже решено — " + ("старые голоса." if job.meta.get("voices_choice") == "keep"
                                                            else "голоса с нуля.")
        job.meta.update({"voices_choice": "keep" if keep else "new", "status": "queued", "note": ""})
        job.save()
        job.log_line(("решение: старые голоса" if keep else "решение: голоса с нуля") + (f" ({why})" if why else ""))
        self.launch(job)
        return (f"♻️ «{job.meta['name']}»: беру готовые голоса — начинаю сборку." if keep else
                f"🆕 «{job.meta['name']}»: " + (why + " — " if why else "") + "создаю голоса с нуля, потом сборка.")

    def voices_timeouts(self) -> None:
        for job in self.jobs.load():
            if job.status == "choice_voices" and time.time() >= float(job.meta.get("voices_deadline") or 0):
                self.send_to_job_chat(job, self.choose_voices(job.id, False, "ответа не было минуту"))

    def delete_voices(self, key: str) -> str:
        """Кнопка «удалить голоса»: следующая сборка книги создаст их с нуля. Голоса книги в работе не трогаем."""
        folder = self.voice_packs.root / key
        if not key.isalnum() or not folder.is_dir():
            return "Голосов этой книги уже нет."
        for job in self.jobs.load():
            used = [job.meta.get("pack_dir") or "", str(pathlib.Path(job.meta.get("voice_pack") or "/").parent)]
            if job.status in housekeeping.LIVE_JOBS and any(pathlib.Path(u).name == key for u in used if u):
                return f"Голоса нужны идущей сборке «{job.meta['name']}» — сначала остановите её."
        title = ""
        try:
            title = self.voice_packs.describe(folder).get("title") or ""
        except (OSError, ValueError, KeyError):
            pass
        self.voice_packs.delete(key)
        return f"🗑 Голоса книги «{title or key}» удалены: следующая сборка создаст их с нуля."

    def show_books(self, chat_id) -> None:
        """«📚 Книги» в Telegram — управление книгой, как на странице: заново, продолжить, голоса, журнал."""
        seen, books = set(), []
        for job in self.jobs.load():
            if job.meta.get("part_index") or job.meta.get("cast") or job.meta.get("glue"):
                continue
            book = str(job.meta.get("book") or "")
            if not book or book in seen:
                continue
            seen.add(book)
            books.append(job)
            if len(books) >= 5:
                break
        if not books:
            self.send_menu(chat_id, "Книг пока нет — пришлите EPUB.")
            return
        for job in books:
            chain = self.chains.by_id(job.meta.get("chain") or "") if job.meta.get("chain") else None
            book = pathlib.Path(job.meta["book"])
            pack = self.voice_packs.pack(book) if book.is_file() else None
            where = chain.summary() if chain is not None else (job.meta.get("note") or "")
            rows = [[("🔄 Вся книга заново", f"chain_restart:{chain.id}" if chain is not None
                      else f"repeat:{job.meta['session']}")]]
            if chain is not None and chain.status not in (chains.DONE, chains.REPLACED):
                rows[0].append(("▶️ Продолжить", f"chain_continue:{chain.id}"))
            rows.append([("📜 Журнал", f"log:{job.meta['session']}")]
                        + ([("🗑 Удалить голоса", f"voices_delete:{pack.parent.name}")] if pack else []))
            self.bot.send(chat_id, f"📚 «{job.meta['name']}» · {job.status}" + (f"\n{where}" if where else "")
                          + ("\n🎭 голоса книги готовы" if pack else ""), buttons=rows)

    def llm_secrets(self):
        """Ключ LLM для машины: разметка ролей и прочтение иностранных вставок. Нет ключа — None."""
        if not self.settings.llm_key:
            return None
        return {"LLM_PROVIDER": self.settings.llm_provider, "LLM_MODEL": self.settings.llm_model,
                "LLM_API_KEY": self.settings.llm_key}

    def start_cast(self, job, book: pathlib.Path) -> None:
        """Первая машина: ключ LLM — заданию через окружение, не в командную строку."""
        folder = pathlib.Path(job.meta.get("pack_dir") or self.voice_packs.folder(book))
        folder.mkdir(parents=True, exist_ok=True)
        self.announce_profile(job)
        self.jobs.start(job, book, gpu=GPU, drive_dir=DRIVE_DIR, max_hours=0,
                        extra=["--cast", "--out-dir", str(folder)],
                        secrets=self.llm_secrets())

    @staticmethod
    def llm_failure(job) -> str:
        """Что сказал конвейер про LLM (ключ, баланс, сеть) — строка для человека."""
        for line in reversed(job.tail(20000).splitlines()):
            if re.search(r"(?i)(отклонил ключ|нет ключа|недостаточно средств|ошибка запроса \(HTTP|"
                         r"попытки исчерпаны|не удалось получить корректный ответ|нет связи с (Ollama|DeepSeek))",
                         line):
                return line.strip()[:300]
        return ""

    @staticmethod
    def pipeline_error(job) -> str:
        """Последняя строка ошибки Python в журнале («ValueError: …») — причина для человека."""
        for line in reversed(job.tail(20000).splitlines()):
            if re.match(r"\s*\w+(Error|Exception): ", line):
                return line.strip()[:300]
        return ""

    def casting_pass(self) -> None:
        """Книги, ждущие голоса: пакет готов — озвучка; сбой машины — повтор и профили; иное — один голос."""
        for job in self.jobs.load():
            if job.status != "casting":
                continue
            cast = self.jobs.by_id(job.meta.get("cast_job") or "")
            if cast is None:
                self.single_voice(job, "задание «голоса» пропало")
                continue
            if cast.status in ("queued", "running", "paused") or cast.meta.get("handled"):
                continue
            if cast.status == "done":
                self.cast_ready(job, cast)
            elif cast.status == "stopped":
                cast.meta["handled"] = True
                cast.save()
                job.meta.update({"status": "stopped", "finished": time.time(), "note": "остановлено на голосах"})
                job.save()
            elif cast.status == "failed":
                if "бот перезапускали" in str(cast.meta.get("note") or ""):
                    self.requeue(cast, "бот перезапускали — запускаю голоса заново")
                    continue
                tail = cast.tail(20000)
                # «Конвейер упал» — ошибка разметки или голосов, а не машины: «архив не забрался»
                # тогда лишь следствие, и повтор на другом профиле упадёт так же.
                crashed = "конвейер упал с кодом" in tail
                trouble = cast.meta.get("stalled") or (diagnose(tail) and not crashed)
                if trouble and not self.llm_failure(cast) and self.can_failover(cast):
                    self.after_failure(cast)     # машина Colab: повтор, следующий профиль, пауза
                    continue
                cast.meta["handled"] = True
                cast.save()
                reason = (self.llm_failure(cast) or (self.pipeline_error(cast) if crashed else "")
                          or cast.meta.get("note") or cast.last_error() or "причина в журнале")
                self.single_voice(job, f"разметка ролей не удалась ({reason})")

    def single_voice(self, job, reason: str) -> None:
        job.meta.update({"single_voice": reason, "status": "queued", "note": ""})
        job.save()
        job.log_line(f"{reason} — озвучиваю одним голосом")
        self.send_to_job_chat(job, f"⚠️ «{job.meta['name']}»: {reason}.\nОзвучиваю одним голосом — сборка "
                                   f"не останавливается.")

    def cast_ready(self, job, cast) -> None:
        """Пакет пришёл: состав героев — в Telegram, образцы — на страницу, книга — на озвучку."""
        cast.meta["handled"] = True
        cast.save()
        try:
            described = self.voice_packs.accept(cast.meta["result"])
        except (OSError, ValueError, KeyError) as exc:
            self.single_voice(job, f"пакет голосов не читается ({exc})")
            return
        job.meta.update({"voice_pack": cast.meta["result"], "status": "queued", "note": ""})
        job.save()
        heroes = [v for v in described["voices"] if not v["fixed"]]
        job.log_line(f"голоса готовы: героев {len(heroes)} — " + ", ".join(v["name"] for v in heroes))
        lines = [f"• {v['name']} — реплик {v['lines']}" + (f": {v['about']}" if v["about"] else "")
                 for v in heroes]
        self.send_to_job_chat(job, f"🎭 «{job.meta['name']}»: голоса готовы — героев {len(heroes)}, рассказчик "
                                   f"прежний.\n" + "\n".join(lines[:40])
                                   + "\n\nОбразцы голосов — на странице. Начинаю озвучку.")
        # Сами образцы в Telegram не шлём: на книгу их до пятнадцати, а слушают их на странице.

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
        if job.meta.get("voice_pack") and not job.meta.get("single_voice"):
            chain.meta["voice_pack"] = job.meta["voice_pack"]    # все части — одними голосами
        else:
            chain.meta.pop("voice_pack", None)
        chain.save()
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
            # Ход нарезки пишется в журнал цепочки, а кнопка «журнал» показывает журнал задания:
            # переносим туда конец вывода нарезки, а причину (последнюю строку ошибки) — в сообщение.
            lines = [line.rstrip() for line in chain.tail(6000).splitlines() if line.strip()]
            cause = next((line.strip() for line in reversed(lines)
                          if re.search(r"(Error|Exception|ошибк|не удалось)", line)), "")
            job.log_line(f"нарезка не удалась (код {code}); конец журнала нарезки:")
            job.log_line("\n".join(lines[-30:]) or "журнал нарезки пуст")
            reason = "нарезка на части не удалась" + (f": {cause[:200]}" if cause else "")
            chain.meta["status"] = chains.FAILED
            chain.meta["note"] = reason
            chain.save()
            job.meta.update({"status": "failed", "finished": time.time(), "note": reason})
            job.save()
            self.send_to_job_chat(job, f"⚠️ «{job.meta['name']}»: нарезка на части не удалась."
                                       + (f"\nПричина: {cause[:300]}" if cause else "")
                                       + "\nКонец журнала нарезки — в журнале задания (кнопка «журнал»).",
                                  buttons=[[("▶️ Нарезать заново", f"chain_continue:{chain.id}"),
                                            ("✖️ Не надо", "menu")]])
            return
        parts = chains.parts_from_plan(plan, pathlib.Path(chain.meta["book"]))
        # Сначала задания частей, потом ОДНО сохранение цепочки — уже с номерами заданий. Раньше
        # цепочка сохранялась с частями без заданий, и проход демона в этот миг искал задание по
        # пустому номеру: by_id("") отдаёт первое попавшееся (родителя), части получали его статус
        # «splitting», номера заданий терялись — и книга не собиралась никогда.
        for part in parts:
            part_job = self.jobs.add(chain.title_of(part), job.meta.get("chat_id") or "",
                                     pathlib.Path(part["epub"]))
            part_job.meta.update({"chain": chain.id, "part_index": part["index"],
                                  "parts_total": len(parts), "parts_done": 0})
            part_job.log_line(f"часть {part['index']} из {len(parts)} книги "
                              f"«{chain.meta.get('name')}» (≈{part['hours']:.2f} ч звука)")
            part_job.save()
            part["job_id"] = part_job.id
        chain.set_parts(parts)
        chain.log_line(f"частей: {len(parts)} — "
                       + ", ".join(f"{part['index']}:{part['hours']:.2f} ч" for part in parts))
        job.meta.update({"parts_total": len(parts), "parts_done": 0, "part_index": 0})
        job.save()
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
            if any(not part.get("job_id") for part in chain.parts):
                continue                                  # части ещё раздаются заданиям
            for part in chain.parts:
                part_job = self.jobs.by_id(part["job_id"])
                if part_job is None:
                    continue
                if part_job.status != part.get("status"):
                    part["status"] = part_job.status
                    changed = True
                if part_job.status == chains.PART_DONE and part_job.meta.get("result"):
                    part["result"] = part_job.meta["result"]
                    chain.meta.pop("profiles_tried", None)      # часть удалась — круг профилей заново
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
                    # Дважды на одном профиле — пробуем следующий; круг профилей один на часть.
                    current = self.profiles.active()
                    label = self.profile_label(current)
                    tried = list(chain.meta.get("profiles_tried") or []) + [current]
                    chain.meta["profiles_tried"] = tried
                    decision, number = self.next_profile(tried)
                    part_job = self.jobs.by_id(part.get("job_id") or "")
                    note = (part_job.meta.get("note") or "") if part_job else ""
                    if decision == "switch":
                        part["retries"] = 0
                        part["status"] = chains.PART_QUEUED
                        if part_job is not None:
                            self.requeue(part_job, f"повтор на {self.profile_label(number)}")
                        chain.save()
                        job.log_line(f"часть {part['index']} упала дважды на {label} — перехожу на профиль {number}")
                        self.send_to_job_chat(job, f"🔀 «{job.meta['name']}», часть {part['index']} упала "
                                                   f"дважды на {label}." + (f"\nПричина: {note}" if note else "")
                                                   + f"\nПерехожу на {self.profile_label(number)} и повторяю.")
                        return
                    if decision == "login":
                        chain.meta["await_login"] = number
                        reason = (f"часть {part['index']} из {chain.meta.get('total')} упала дважды на {label}; "
                                  f"следующий — профиль {number}, но в него не выполнен вход. Войдите — "
                                  f"и генерация продолжится сама.")
                        chain.hold(reason)
                        self.hold_part_jobs(chain)
                        job.meta.update({"status": "waiting", "note": reason})
                        job.save()
                        job.log_line(f"остановлено: {reason}")
                        self.send_to_job_chat(job, f"⏸ «{job.meta['name']}»: {reason}"
                                                   + (f"\nПричина: {note}" if note else ""),
                                              buttons=[[(f"🔑 Войти в профиль {number}", f"profile_login:{number}")]]
                                              + self.part_buttons(chain, part))
                        return
                    reason = (f"часть {part['index']} из {chain.meta.get('total')} упала дважды"
                              + (f" на всех профилях ({', '.join(map(str, tried))})" if self.profiles.enabled
                                 else "")
                              + f". Дальше идти нельзя: книга получится с дырой. "
                              f"Повторить её — кнопкой здесь или «продолжить генерацию» на странице.")
                    chain.hold(reason)
                    self.hold_part_jobs(chain)
                    job.meta.update({"status": "waiting", "note": reason})
                    job.save()
                    job.log_line(f"остановлено: {reason}")
                    self.send_to_job_chat(job, f"⚠️ «{job.meta['name']}»: {reason}"
                                               + (f"\nПричина: {note}" if note else "")
                                               + "\n\nПовторить часть?",
                                          buttons=self.part_buttons(chain, part))
                    return

            if chain.all_done() and chain.status != chains.DONE:
                self.finish_chain(chain, job)
                return

    def hold_part_jobs(self, chain) -> None:
        """Остановленная цепочка не должна запускать следующие части сама.

        hold() помечает части в цепочке, но их задания стоят в очереди как «queued» — очередь их и
        запускала, а проход по цепочкам тут же перетирал пометку статусом задания. Придерживаем
        сами задания; «продолжить генерацию» и «повторить» ставят их обратно в очередь.
        """
        for part in chain.held():
            part_job = self.jobs.by_id(part.get("job_id") or "")
            if part_job is not None and part_job.status == "queued":
                part_job.meta.update({"status": chains.PART_HELD, "note": part.get("note") or ""})
                part_job.log_line("придержано: цепочка остановлена, ждёт решения")
                part_job.save()

    def part_buttons(self, chain, part) -> list:
        """Кнопки под сообщением об упавшей части — те же действия, что на странице."""
        part_job = self.jobs.by_id(part.get("job_id") or "")
        rows = []
        if part_job is not None:
            rows.append([("🔁 Повторить часть", f"part_retry:{part_job.id}"),
                         ("✖️ Нет", "menu")])
            rows.append([("📜 Журнал части", f"log:{part_job.meta.get('session') or part_job.id}")])
        rows.append([("▶️ Продолжить генерацию", f"chain_continue:{chain.id}"),
                     ("🔄 Вся книга заново", f"chain_restart:{chain.id}")])
        return rows

    def retry_part(self, job_id: str) -> str:
        """«Повторить» у части книги: перезапускает ИМЕННО её, в своей цепочке.

        Отдельное задание из epub части тут не годится: цепочка о нём не узнала бы, а в режиме
        частей такая «книга» ещё и пошла бы резаться сама. Если цепочка остановилась на этой части,
        продолжаем её целиком — дальше автоматика идёт сама.
        """
        part_job = self.jobs.by_id(job_id)
        if part_job is None:
            return "Такой части уже нет."
        chain = self.chains.by_id(part_job.meta.get("chain") or "")
        part = chain.part_by_job(part_job.id) if chain else None
        if chain is None or part is None:
            return "Книга этой части больше не найдена — загрузите книгу заново."
        if part_job.status in ("queued", "running") or part.get("status") in (chains.PART_QUEUED,
                                                                              chains.PART_RUNNING):
            return f"«{chain.meta.get('name')}»: часть {part['index']} уже в работе."
        if part.get("status") == chains.PART_DONE:
            return f"«{chain.meta.get('name')}»: часть {part['index']} уже готова — повторять нечего."
        part["retries"] = 0
        chain.save()
        if chain.status == chains.FAILED:
            return self.continue_chain(chain.id)
        part["status"] = chains.PART_QUEUED
        part["note"] = ""
        chain.save()
        part_job.meta.update({"status": "queued", "note": "", "finished": None, "result": ""})
        part_job.log_line("повтор части по вашей команде")
        part_job.save()
        return f"«{chain.meta.get('name')}»: повторяю часть {part['index']}."

    def parts_dir(self, chain) -> pathlib.Path:
        """Сырьё частей в томе: переживёт и перезапуск бота, и обновление образа."""
        return DATA / "parts" / str(chain.id)

    def parts_line(self, chain, index) -> str:
        return (f"режим частей: часть {index} из {chain.meta.get('total') or '?'} — на машину "
                f"только синтез, громкость, музыка и сборка будут в контейнере")

    def finish_chain(self, chain, job) -> None:
        """Все части готовы → финал: склейка, громкость по всей книге, музыка, сборка — в контейнере."""
        # Ищем задание сборки только по НЕПУСТОЙ метке: by_id("") отдаёт первое попавшееся задание,
        # и книга «собиралась» бы из чужого результата, ничего на самом деле не собирая.
        marker = str(chain.meta.get("final_job") or "")
        final = self.jobs.by_id(marker) if marker else None
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
            self.send_to_job_chat(job, f"✅ «{job.meta['name']}»: {note}", buttons=MENU)
            return
        # Об этой неудачной сборке уже сообщили: цепочка стоит, пока человек не решит. Без этой
        # метки проход по цепочкам слал бы одно и то же сообщение на каждом круге.
        if chain.meta.get("glue_reported") == final.id:
            return
        reason = (f"сборка книги не удалась: {final.meta.get('note') or 'смотрите журнал сборки'}. "
                  f"Части целы — собрать заново можно кнопкой здесь или «собрать книгу» на странице.")
        chain.hold(reason)
        chain.meta["glue_reported"] = final.id
        chain.save()
        job.meta.update({"status": "waiting", "note": reason})
        job.save()
        job.log_line(reason)
        self.send_to_job_chat(job, f"⚠️ «{job.meta['name']}»: {reason}",
                              buttons=[[("🧩 Собрать книгу", f"chain_glue:{chain.id}"), ("✖️ Нет", "menu")],
                                       [("📜 Журнал сборки", f"log:{final.meta.get('session') or final.id}")]])

    def start_glue(self, chain, job) -> None:
        """Запускает сборку книги из готовых частей. Повторный вызов только следит, не запускает второй раз."""
        marker = str(chain.meta.get("final_job") or "")
        running = self.jobs.by_id(marker) if marker else None
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
                                                "--expect", str(chain.meta.get("total") or 0),
                                                # имя архива как у файла книги: по абсолютному пути в
                                                # журнале бот видит, что архив готов
                                                "--name", pathlib.Path(chain.meta["book"]).stem])
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

    def restart_book(self, chain_id: str, chat_id: str = "") -> str:
        """«Вся книга заново»: всё, что идёт по книге, останавливается, старая нарезка уходит в
        заменённые, книга начинается с нуля — новая нарезка, все части. Готовый пакет голосов
        (многоголосая книга) берётся повторно: разметка LLM та же книга, тот же хеш."""
        chain = self.chains.by_id(chain_id)
        if chain is None:
            return "Такой книги с частями уже нет."
        book = pathlib.Path(chain.meta.get("book") or "")
        if not book.is_file():
            return f"Файла «{book.name}» больше нет в томе — пришлите книгу заново."
        parent = self.jobs.by_id(chain.meta.get("job") or "")
        others = [self.jobs.by_id(part.get("job_id") or "") for part in chain.parts]
        final = self.jobs.by_id(str(chain.meta.get("final_job") or "")) if chain.meta.get("final_job") else None
        for item in [parent, final] + others:
            if item is not None and item.status in ("running", "queued", "splitting", "waiting", "gluing",
                                                    "held", "paused", "casting"):
                self.jobs.stop(item)
                item.meta.update({"status": "stopped", "finished": time.time(),
                                  "note": "остановлено: книга начата заново"})
                item.save()
        chain.meta.update({"status": chains.REPLACED, "note": "книга начата заново"})
        chain.save()
        name = (parent.meta.get("name") if parent else "") or chain.meta.get("name") or book.name
        chat = (parent.meta.get("chat_id") if parent else "") or chat_id or self.owner_chat()
        fresh = self.jobs.add(name, chat, book)
        fresh.meta["ask_voices"] = True              # голоса есть — спросить: старые или с нуля
        fresh.save()
        fresh.log_line(f"книга начата заново (прежняя нарезка {chain.id} заменена)")
        self.launch(fresh)
        return f"🔄 «{name}»: начинаю всю книгу заново — новая нарезка и все части."

    def continue_chain(self, chain_id: str, keep_tried: bool = False) -> str:
        """Кнопка «продолжить генерацию»: с первой неготовой части, готовые не трогаем.

        Человек нажал «продолжить» — круг по профилям начинается заново. После входа в профиль,
        которого ждала цепочка, круг продолжается (keep_tried): иначе он не кончился бы никогда.
        """
        chain = self.chains.by_id(chain_id)
        if chain is None:
            return "Такой книги с частями уже нет."
        if not keep_tried:
            chain.meta.pop("profiles_tried", None)
            chain.meta.pop("await_login", None)
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

    def send_to_job_chat(self, job, text: str, buttons=None) -> None:
        """Сообщение по книге: в чат задания, а если его нет — владельцу."""
        chat_id = str(job.meta.get("chat_id") or "").strip()
        if not looks_like_chat_id(chat_id):
            chat_id = self.owner_chat()
        if not chat_id:
            print(f"[bot] {text}", flush=True)
            return
        try:
            if buttons:
                self.bot.send(chat_id, text, buttons=buttons)
            else:
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

        if text and not text.startswith("/") and str(chat_id) in getattr(self, "awaiting_key", {}):
            self.accept_llm_key(chat_id, text, message.get("message_id"))
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
        elif data == "profiles":
            self.show_profiles(chat_id)
        elif data == "voices":
            self.show_voices(chat_id)
        elif data == "voices_toggle":
            turned = "one" if self.settings.voices == "multi" else "multi"
            self.settings.change(voices=turned)
            self.show_voices(chat_id, "Многоголосая озвучка " + ("включена." if turned == "multi" else "выключена."))
        elif data.startswith("llm_provider:"):
            provider = data.partition(":")[2]
            if provider in llm.PROVIDERS:
                self.settings.change(llm_provider=provider)
            self.show_voices(chat_id, f"Поставщик LLM: {llm.PROVIDERS.get(provider, provider)}.")
        elif data == "llm_key":
            self.awaiting_key[str(chat_id)] = self.settings.llm_provider
            self.bot.send(chat_id, f"Пришлите ключ {llm.PROVIDERS[self.settings.llm_provider]} следующим "
                                   f"сообщением — сохраню его и удалю сообщение из переписки.")
        elif data == "llm_models":
            self.show_models(chat_id)
        elif data.startswith("llm_model:"):
            self.settings.change(llm_model=data.partition(":")[2])
            self.show_voices(chat_id, f"Модель: {self.settings.llm_model}.")
        elif data == "profile_add":
            self.show_profiles(chat_id, self.profile_action("add"))
        elif data.startswith("profile_login:"):
            number = _number(data)
            self.bot.send(chat_id, f"Начинаю вход в профиль {number}, ссылка придёт через несколько секунд…")
            self.in_background(chat_id, lambda: self.profile_action("login", number), "Вход в Colab")
        elif data.startswith("profile_use:"):
            self.show_profiles(chat_id, self.profile_action("activate", _number(data)))
        elif data.startswith("profile_logout:"):
            self.show_profiles(chat_id, self.profile_action("logout", _number(data)))
        elif data.startswith("profile_delete_ask:"):
            number = _number(data)
            self.bot.send(chat_id, f"Удалить профиль {number}? Его вход уйдёт в корзину профилей, "
                                   f"профили с большими номерами сдвинутся на один.",
                          buttons=[[("🗑 Да, удалить", f"profile_delete:{number}"), ("↩️ Отмена", "profiles")]])
        elif data.startswith("profile_delete:"):
            self.show_profiles(chat_id, self.profile_action("delete", _number(data)))
        elif data == "profile_logout_all_ask":
            self.bot.send(chat_id, "Выйти из всех профилей Google Colab? Вход каждого отложится рядом "
                                   "с меткой времени; потом войти можно снова.",
                          buttons=[[("🚪 Да, выйти из всех", "profile_logout_all"), ("↩️ Отмена", "profiles")]])
        elif data == "profile_logout_all":
            self.show_profiles(chat_id, self.profile_action("logout_all"))
        elif data.startswith("resume:"):              # книга на паузе: пройти по профилям заново
            self.bot.send(chat_id, self.resume_job(data.partition(":")[2]))
        elif data == "stop_ask":                       # остановка «всё» — через подтверждение
            self.bot.send(chat_id, "Остановить всё: текущую сборку и очередь, машины освободить?",
                          buttons=[[("⏹ Да, остановить", "stop_all"), ("↩️ Отмена", "menu")]])
        elif data == "stop_all":
            self.bot.send(chat_id, "Останавливаю всё…")
            self.in_background(chat_id, self.stop_all_jobs, "Остановка")
        elif data.startswith(("cut:", "whole:")):      # длинная книга: резать или одним файлом
            action, _, job_id = data.partition(":")
            self.bot.send(chat_id, self.choose_split(job_id, action == "cut"))
        elif data.startswith("part_retry:"):           # упавшая часть книги: повторить именно её
            self.bot.send(chat_id, self.retry_part(data.partition(":")[2]))
        elif data.startswith(("voices_keep:", "voices_new:")):
            action, _, job_id = data.partition(":")
            self.bot.send(chat_id, self.choose_voices(job_id, action == "voices_keep"))
        elif data.startswith("voices_delete:"):
            key = data.partition(":")[2]
            self.bot.send(chat_id, "Удалить голоса этой книги? Следующая сборка заново разметит роли и "
                                   "создаст голоса (это время машины и запросы LLM).",
                          buttons=[[("🗑 Да, удалить", f"voices_delete_do:{key}"), ("↩️ Отмена", "menu")]])
        elif data.startswith("voices_delete_do:"):
            self.bot.send(chat_id, self.delete_voices(data.partition(":")[2]))
        elif data == "books":
            self.show_books(chat_id)
        elif data.startswith("chain_restart:"):        # «вся книга заново»: с подтверждением
            self.bot.send(chat_id, "Начать всю книгу заново? Всё, что идёт по ней, остановится, части "
                                   "озвучатся снова с новой нарезки.",
                          buttons=[[("🔄 Да, заново", f"chain_restart_do:{data.partition(':')[2]}"),
                                    ("↩️ Отмена", "menu")]])
        elif data.startswith("chain_restart_do:"):
            self.bot.send(chat_id, self.restart_book(data.partition(":")[2], str(chat_id)))
        elif data.startswith("chain_continue:"):       # «продолжить генерацию», как на странице
            self.bot.send(chat_id, self.continue_chain(data.partition(":")[2]))
        elif data.startswith("chain_glue:"):           # «собрать книгу», как на странице
            self.bot.send(chat_id, self.glue_chain(data.partition(":")[2]))
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
        elif head in ("/books", "/book"):
            self.show_books(chat_id)
        elif head in ("/voices", "/llm"):
            self.show_voices(chat_id)
        elif head in ("/profiles", "/profile"):
            if argument.isdigit():
                self.show_profiles(chat_id, self.profile_action("activate", int(argument)))
            else:
                self.show_profiles(chat_id)
        elif head in ("/resume", "/continue"):
            self.bot.send(chat_id, self.resume_job(argument) if argument
                          else "Что продолжить? /resume <номер из /jobs>")
        else:
            self.send_menu(chat_id, "Не знаю такой команды.\n\n" + HELP)

    def status(self, chat_id) -> None:
        jobs = self.jobs.load()
        running = [job for job in jobs if job.status == "running"]
        queued = [job for job in jobs if job.status == "queued"]
        # Книга в режиме частей машину не занимает: её состояние — «режется» или «жду части».
        chains_now = [job for job in jobs if job.status in ("splitting", "waiting", "gluing")]
        asking = [job for job in jobs if job.status == "choice"]
        paused = [job for job in jobs if job.status == "paused"]
        casting = [job for job in jobs if job.status == "casting"]
        voices_asking = [job for job in jobs if job.status == "choice_voices"]
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
        for job in voices_asking:
            lines.append(f"🎭 {job.meta['name']} — ждёт ответа: старые голоса или с нуля")
        for job in casting:
            lines.append(f"🎭 {job.meta['name']} — первая машина: разметка ролей и голоса героев")
        for job in paused:
            lines.append(f"⏸ {job.meta['name']} — {job.meta.get('note') or 'пауза'}"[:200])
        for job in asking:
            lines.append(f"❓ {job.meta['name']} — ≈{float(job.meta.get('hours_estimate') or 0):.1f} ч, "
                         f"ждёт ответа: резать на части или одним файлом")
        head = (f"Сейчас: {len(running) + len(chains_now)} в работе, {len(queued)} в очереди.")
        if not running and not queued and not chains_now and not asking and not casting and not voices_asking:
            head = "Свободно: ничего не собирается."
        if self.profiles.enabled:
            head += f"\nGoogle Colab: {self.profile_label()}."
        buttons = [[("✂️ Резать «" + job.meta["name"][:20] + "»", f"cut:{job.id}"),
                    ("📄 Одним файлом", f"whole:{job.id}")] for job in asking[:3]] + \
                  [[("▶️ Продолжить «" + job.meta["name"][:20] + "»", f"resume:{job.meta['session']}")]
                   for job in paused[:3]] + \
                  [[("♻️ Старые голоса", f"voices_keep:{job.id}"), ("🆕 С нуля", f"voices_new:{job.id}")]
                   for job in voices_asking[:3]] + MENU
        self.bot.send(chat_id, head + ("\n\n" + "\n".join(lines) if lines else ""), buttons=buttons)

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
        if job.meta.get("chain") and job.meta.get("part_index"):
            self.bot.send(chat_id, self.retry_part(job.id))     # часть книги — повтор в своей цепочке
            return
        if job.meta.get("chain"):                               # книга по частям — вся заново
            self.bot.send(chat_id, self.restart_book(job.meta["chain"], str(chat_id)))
            return
        book = pathlib.Path(job.meta.get("book") or "")
        if not book.is_file():
            self.bot.send(chat_id, f"Файла «{book.name}» больше нет в томе — пришлите книгу заново.")
            return
        if self.jobs.live_running():
            self.bot.send(chat_id, "Сейчас идёт другая сборка — сначала /stop или дождитесь её конца.")
            return
        again = self.jobs.add(book.name, chat_id, book)
        again.meta["ask_voices"] = True              # повтор: голоса есть — спросить, старые или с нуля
        again.save()
        if self.ask_split(again, book):
            return                                  # длинная книга: сначала спросили, резать ли
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
        if self.ask_split(job, book):
            return
        self.bot.send(chat_id, f"📖 Принял «{name}» ({book.stat().st_size / 1e6:.1f} МБ).\n"
                               "Запускаю сборку в фоне — о стадиях напишу сам.")
        self.launch(job)

    def ask_split(self, job, book: pathlib.Path) -> bool:
        """Длинная книга при включённом режиме частей: режем сразу, без вопроса.

        Режим частей и порог выбираются в админке — это и есть ответ на «резать ли», поэтому
        переспрашивать при каждой книге (и при «повторить») не нужно. Короткая книга и режим
        «целиком» ничего не меняют. Возвращает True, если запуск надо отложить до ответа; теперь
        такого не бывает — False всегда, но задания, ждавшие ответа до обновления, кнопки
        «резать / одним файлом» по-прежнему решают (choose_split).
        """
        if self.settings.mode != bot_settings.MODE_PARTS:
            return False
        hours = chains.estimate_hours(book)
        limit = self.settings.parts_hours
        if hours <= limit:
            return False
        count = max(2, int(-(-hours // limit)))
        job.meta.update({"split": "parts", "hours_estimate": round(hours, 2)})
        job.save()
        job.log_line(f"книга длинная (≈{hours:.1f} ч звука, порог {limit:g} ч) — режу на части: "
                     f"так задано в настройках")
        self.send_to_job_chat(
            job,
            f"✂️ «{job.meta['name']}» длинная: ≈{hours:.1f} ч звука. Режу на части по {limit:g} ч "
            f"(≈{count}) — так задано в настройках. Части озвучатся по одной, книга целиком соберётся "
            f"в конце.")
        return False

    def choose_split(self, job_id: str, cut: bool) -> str:
        """Ответ на «резать ли»: из Telegram и со страницы — одно место."""
        job = self.jobs.by_id(job_id)
        if job is None:
            return "Такого задания уже нет — пришлите книгу заново."
        if job.status != "choice":
            decided = {"parts": "резать на части", "whole": "одним файлом"}.get(job.meta.get("split"), "")
            return (f"«{job.meta['name']}»: уже решено" + (f" — {decided}." if decided else ".")
                    + " Ход сборки — «Статус».")
        job.meta.update({"split": "parts" if cut else "whole", "status": "queued", "note": ""})
        job.save()
        job.log_line("решение: " + ("резать на части" if cut else "одним файлом, без нарезки"))
        self.launch(job)
        if cut:
            return (f"✂️ «{job.meta['name']}»: режу на части по {self.settings.parts_hours:g} ч — "
                    f"о каждой части напишу.")
        return f"📄 «{job.meta['name']}»: собираю одним файлом, без нарезки — о стадиях напишу."

    def launch(self, job) -> None:
        """Запуск задания. Одно задание запускают двое — очередь бота и страница (или кнопка):
        без блокировки оба успевали увидеть «в очереди» и, например, дважды резали книгу —
        с двумя цепочками на одного родителя. Запуск — под общей блокировкой и по статусу с
        диска: уже запущенное задание второй раз не запускается.
        """
        with LAUNCH_LOCK:
            fresh = self.jobs.by_id(job.id)
            if fresh is not None and fresh.status != "queued":
                return
            self._launch(fresh or job)

    def _launch(self, job) -> None:
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
            if job.meta.get("cast"):
                self.start_cast(job, book)        # первая машина многоголосой книги
                return
            # Часть книги идёт на машину ТОЛЬКО на синтез: сырьё вернётся в том, а громкость, музыка
            # и сборка будут здесь. Книга целиком в режиме частей машину не занимает вовсе.
            if job.meta.get("part_index") and job.meta.get("chain"):
                chain = self.chains.by_id(job.meta["chain"])
                if chain is None:
                    raise RuntimeError("книга с частями больше не найдена — начните её заново")
                job.log_line(self.parts_line(chain, job.meta["part_index"]))
                self.announce_profile(job)
                pack = chain.meta.get("voice_pack")
                self.jobs.start(job, book, gpu=GPU, drive_dir=DRIVE_DIR, max_hours=SHRINK_HOURS,
                                extra=["--parts-raw", "--part-index", str(job.meta["part_index"]),
                                       "--parts-dir", str(self.parts_dir(chain))]
                                + (["--voice-pack", pack] if pack and pathlib.Path(pack).is_file() else []),
                                secrets=self.llm_secrets())
                return
            pack = self.voice_pack_for(job, book)
            if pack is self.WAITING:
                return                            # сначала голоса: озвучка — после первой машины
            voices_extra = ["--voice-pack", str(pack)] if pack else []
            split = job.meta.get("split")
            if split == "whole":
                # Ответ «нет, одним файлом»: машина не режет книгу и сама (порог 0 — не резать).
                job.log_line("режим: одна книга целиком — так решили на вопрос «резать ли»")
                self.announce_profile(job)
                self.jobs.start(job, book, gpu=GPU, drive_dir=DRIVE_DIR, max_hours=0, extra=voices_extra,
                                secrets=self.llm_secrets())
                return
            if (split == "parts" or self.settings.mode == bot_settings.MODE_PARTS) \
                    and not job.meta.get("part_index"):
                self.start_chain(job, book)
                return
            job.log_line(self.settings.job_line())
            self.announce_profile(job)
            self.jobs.start(job, book, gpu=GPU, drive_dir=DRIVE_DIR, max_hours=SHRINK_HOURS, extra=voices_extra,
                            secrets=self.llm_secrets())
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
        if job.meta.get("chain") and (job.meta.get("part_index") or job.meta.get("glue")):
            self.report_chain_member(job, chat_id, minutes)
            return
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


    def report_chain_member(self, job, chat_id, minutes: float) -> None:
        """Итог части книги или сборки книги: свои слова, без «ссылок на Drive» и кнопок книги.

        Готовая часть — сообщение «часть k из N готова» с архивом и тем, что дальше. Упавшую часть
        повторяет или останавливает проход по цепочкам — он и пишет (с кнопкой «повторить»);
        общий отчёт «не собралась» тут сбивал бы с толку: часть, может быть, уже повторяется.
        Сборку книги целиком сообщает finish_chain.
        """
        if job.meta.get("glue") or job.status != "done":
            return
        chain = self.chains.by_id(job.meta.get("chain") or "")
        total = (chain.meta.get("total") if chain else None) or job.meta.get("parts_total") or "?"
        index = job.meta.get("part_index")
        result = pathlib.Path(job.meta.get("result") or "")
        try:                                       # архив части — в результаты, как у обычной книги
            if result.name and result.is_file() and result != self.jobs.results / result.name:
                shutil.copyfile(result, self.jobs.results / result.name)
        except OSError:
            pass
        following = None
        if chain is not None:
            following = next((part for part in chain.parts if part.get("index", 0) > int(index)
                              and part.get("status") != chains.PART_DONE), None)
        then = (f"Дальше — часть {following['index']}, она уже в очереди." if following
                else "Это последняя часть: дальше склейка и сборка всей книги здесь, в контейнере.")
        self.bot.send(chat_id, f"✅ «{chain.meta.get('name') if chain else job.meta['name']}»: часть {index} "
                               f"из {total} готова за {minutes:.0f} мин.\n"
                               f"Архив части: {self.jobs.results / result.name if result.name else 'в томе'}\n{then}")


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
    try:
        print("[bot] " + app.profiles.prepare(), flush=True)
    except OSError as exc:
        print(f"[bot] профили Colab не подготовились: {exc}", flush=True)
    swept = app.housekeeping(force=True)
    print(f"[bot] уборка томов: {', '.join(swept) if swept else 'убирать нечего'} "
          f"(хранить {app.settings.keep_days} дн., дальше — раз в час)", flush=True)
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
                          continue_chain=app.continue_chain, glue=app.glue_chain,
                          ask_split=app.ask_split, choose_split=app.choose_split,
                          retry_part=app.retry_part, profiles=app.profiles,
                          profile_action=app.profile_action, resume=app.resume_job,
                          profile_label=app.profile_label, voice_packs=app.voice_packs,
                          refresh_models=app.refresh_models, restart_book=app.restart_book,
                          choose_voices=app.choose_voices, delete_voices=app.delete_voices)
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
