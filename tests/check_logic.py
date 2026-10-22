#!/usr/bin/env python3
"""Быстрые проверки логики бота — без Telegram, без Colab, без сети.

Запускается из smoke-теста (и в CI, и локально). Проверяем то, что легко сломать незаметно:
разбор форм, защиту от путей, распознавание кода авторизации, объяснение ошибок,
жизненный цикл задания и удаление.
"""
import json, os, pathlib, sys, tempfile, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bot"))

import bot as botmod          # noqa: E402
import chains as chains_mod   # noqa: E402
import jobs as jobs_mod       # noqa: E402
import settings as settings_mod  # noqa: E402
import web as webmod          # noqa: E402
from jobs import Jobs, safe_stem, DRIVE_LINK_RE  # noqa: E402

results = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + extra) if extra and not ok else ''}")


# ── распознавание кода авторизации ──
check("код из браузера распознаётся", botmod.looks_like_code("4/0AY0e-g7abcdefghijklmnopqrstuvwxyz1234567890"))
check("длинный код без префикса распознаётся", botmod.looks_like_code("A" * 45))
check("случайная фраза кодом не считается", not botmod.looks_like_code("привет, как дела?"))
check("короткий текст кодом не считается", not botmod.looks_like_code("123456"))

# ── чат для уведомлений: пометка «web» — не чат, на неё Telegram отвечает «chat not found» ──
check("номер чата владельца распознаётся", botmod.looks_like_chat_id("235005324"))
check("чат группы со знаком минус распознаётся", botmod.looks_like_chat_id("-1001234567890"))
check("пометка «web» чатом не считается", not botmod.looks_like_chat_id("web"))
check("пустой чат чатом не считается", not botmod.looks_like_chat_id("") and not botmod.looks_like_chat_id(None))

# ── сравнение файлов (одинаковую книгу не храним дважды) ──
work = pathlib.Path(tempfile.mkdtemp())
first, second, third = work / "a.epub", work / "b.epub", work / "c.epub"
first.write_bytes(b"PK one and the same book")
second.write_bytes(b"PK one and the same book")
third.write_bytes("PK другой файл того же размера.....x".encode())
check("одинаковые файлы опознаются", botmod.same_file(first, second))
check("разные файлы не путаются", not botmod.same_file(first, third))

# ── имена файлов: никаких выходов из папки ──
check("путь вверх не проходит", "/" not in webmod.safe_name("../../etc/passwd"))
check("имя книги без слэшей", "/" not in safe_stem("../../плохая/книга.epub"))
check("нормальное имя сохраняется", safe_stem("Восставший из ада.epub") == "Восставший из ада")

# ── разбор форм: браузер шлёт оба вида ──
fields = webmod.form_fields("application/x-www-form-urlencoded", b"code=4%2F0Aabc&x=1")
check("обычная форма разбирается", fields.get("code", {}).get("data") == b"4/0Aabc")
boundary = "----probe"
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"book\"; filename=\"k.epub\"\r\n\r\n"
        f"PK fake\r\n--{boundary}--\r\n").encode()
fields = webmod.form_fields(f"multipart/form-data; boundary={boundary}", body)
check("multipart разбирается", fields.get("book", {}).get("filename") == "k.epub")

# ── объяснение сбоев ──
from jobs import diagnose  # noqa: E402
check("занятые машины объясняются", "подождите" in diagnose("TooManyAssignmentsError: too many")) 
check("обрыв сессии объясняется", "прервал" in diagnose("RuntimeError: Connection was lost."))
check("неизвестное не выдумываем", diagnose("что-то своё") == "")

# ── кнопки: клавиатура, а не текст, и данные в пределах лимита Telegram (64 байта) ──
import json as jsonmod        # noqa: E402
import tgapi                  # noqa: E402
markup = tgapi.keyboard([[("Статус", "status")], [("Повторить", "repeat")]])
keyboard_rows = jsonmod.loads(markup or "{}").get("inline_keyboard", [])
check("клавиатура собирается как inline_keyboard",
      keyboard_rows and keyboard_rows[0][0]["callback_data"] == "status", markup[:80])
check("пустые кнопки ничего не ломают", tgapi.keyboard(None) == "" and tgapi.keyboard([]) == "")
menu_items = [item for row in botmod.MENU + botmod.failed_menu("b0123456789") for item in row]
check("в меню только короткие данные, понятные боту",
      bool(menu_items) and all(len(data.encode()) <= 64 and data for _, data in menu_items))
check("меню закрывает базовое управление",
      {"status", "jobs", "login", "usage", "stop_ask"} <= {data for _, data in menu_items})

# ── постоянное меню: клавиатура ПОД полем ввода, а не инлайн под сообщением ──
persistent = jsonmod.loads(tgapi.reply_menu(botmod.REPLY_MENU) or "{}")
flat_menu = [button["text"] for row in persistent.get("keyboard", []) for button in row]
check("постоянное меню собирается как ReplyKeyboardMarkup с is_persistent",
      persistent.get("is_persistent") is True and persistent.get("resize_keyboard") is True,
      tgapi.reply_menu(botmod.REPLY_MENU)[:80])
check("в постоянном меню есть все ходовые кнопки",
      {"📊 Статус", "📋 Задания", "📜 Журнал", "📈 Остаток", "🔑 Войти в Colab",
       "⏹ Остановить всё", "❓ Помощь"} <= set(flat_menu), str(flat_menu))
check("пустое меню ничего не ломает", tgapi.reply_menu(None) == "" and tgapi.reply_menu([]) == "")
check("подпись кнопки меню распознаётся и с эмодзи, и без",
      botmod.menu_action("📊 Статус") == "status" and botmod.menu_action(" статус ") == "status"
      and botmod.menu_action("Остановить всё") == "stop_ask")
check("код авторизации кнопкой меню не считается",
      botmod.menu_action("4/0AY0e-g7abcdefghijklmnopqrstuvwxyz1234567890") == ""
      and botmod.menu_action("") == "")
check("каждая подпись постоянного меню ведёт в действие",
      all(botmod.menu_action(label) for label in flat_menu), str(flat_menu))


# Нажатие кнопки должно доходить до дела — проверяем на живом App с «пустым» Telegram.
class FakeBot:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, preview=False, buttons=None, menu=None):
        # Как настоящий Bot: клавиатура превращается в reply_markup тут же (tgapi) — тогда
        # проверки видят ровно тот JSON, что ушёл бы в Telegram.
        self.sent.append((text, tgapi.reply_menu(menu) if menu else tgapi.keyboard(buttons)))
        return True

    def answer(self, query_id, text=""):
        pass


CHAT = "555"


class FakeLogin:
    """Заглушка входа в Colab: считает сбросы, чтобы проверка видела, что он ДЕЙСТВИТЕЛЬНО был."""

    def __init__(self):
        self.resets = 0

    def awaiting_code(self):
        return False

    def reset(self):
        self.resets += 1
        return "Сброс полный: отложены token.json, sessions.json."


app = botmod.App.__new__(botmod.App)                 # без __init__: сеть и токен не нужны
app.bot = FakeBot()
app.jobs = Jobs(work / "btn-data", HERE / "pipeline", max_jobs=1, env=dict(os.environ))
app.state = {"offset": 0, "owner": CHAT, "seen": []}
app.menu_chats = set()                               # ставится в __init__, здесь его нет
app.colab = FakeLogin()
app.profiles = __import__("profiles").Profiles(work / "colab-config")   # профили выключены


def press(data: str) -> list:
    app.bot.sent = []
    app.on_button({"id": "q1", "data": data, "message": {"message_id": 1, "chat": {"id": CHAT}}})
    return app.bot.sent


check("кнопка «Задания» отвечает как команда /jobs",
      any("Заданий" in text for text, _ in press("jobs")))
check("кнопка «Статус» отвечает как /status",
      any(("Свободно:" in text or "Сейчас:" in text) for text, _ in press("status")))
confirm = press("stop_ask")
check("остановка спрашивает подтверждение, а не рубит сразу",
      any(buttons and "stop_all" in str(buttons) for _, buttons in confirm))
check("неизвестное нажатие возвращает меню",
      any(markup and ("status" in str(markup) or "is_persistent" in str(markup))
          for _, markup in press("чужое")))
check("кнопка журнала на несуществующую сборку не падает",
      any("уже нет" in text for text, _ in press("log:no-such")))
check("в кнопках неудачи зашита именно та сборка",
      "repeat:b0123456789" in str(botmod.failed_menu("b0123456789"))
      and "log:b0123456789" in str(botmod.failed_menu("b0123456789")))


def press_text(text: str) -> list:
    """Как нажатие кнопки ПОСТОЯННОГО меню: приходит обычным текстом, а не callback_query."""
    app.bot.sent = []
    app.handle({"update_id": 1, "message": {"message_id": 1, "chat": {"id": CHAT}, "text": text}})
    return app.bot.sent


app.menu_chats.clear()                                # как при первом сообщении после запуска бота
first = press_text("📋 Задания")
check("постоянное меню ставится при первом же сообщении",
      any(markup and "is_persistent" in str(markup) for _, markup in first),
      str(first[:1])[:120])
check("подпись постоянного меню работает как команда",
      any("Заданий" in text for text, _ in first), str(first)[:160])
check("второй раз меню не переставляется (без спама)",
      not any(markup and "is_persistent" in str(markup) for _, markup in press_text("📊 Статус")))
check("«Остановить всё» из меню всё равно спрашивает подтверждение",
      any(markup and "stop_all" in str(markup) for _, markup in press_text("⏹ Остановить всё")))

# ── сброс входа в Colab: в вебе кнопка была, в Telegram её не было ──
check("подпись «Сбросить вход» распознаётся",
      botmod.menu_action("🚪 Сбросить вход") == "reset_ask"
      and botmod.menu_action("сбросить авторизацию") == "reset_ask"
      and botmod.menu_action("сменить аккаунт") == "reset_ask")
check("сброс есть и в постоянном меню, и в инлайн-меню",
      "🚪 Сбросить вход" in flat_menu
      and any(data == "reset_ask" for row in botmod.MENU for _, data in row)
      and any(name == "logout" for name, _ in botmod.COMMANDS))

app.colab.resets = 0
ask = press("reset_ask")
check("сброс спрашивает подтверждение, а не рвёт вход сразу",
      any(markup and "reset_do" in str(markup) for _, markup in ask) and app.colab.resets == 0,
      str(ask)[:160])
press_text("/logout")                                # команда ведёт туда же
check("команда /logout тоже просит подтверждение",
      any(markup and "reset_do" in str(markup) for _, markup in app.bot.sent)
      and app.colab.resets == 0)
press("reset_do")
deadline = time.time() + 5
while time.time() < deadline and app.colab.resets == 0:       # сброс идёт в отдельном потоке
    time.sleep(0.1)
check("подтверждение действительно сбрасывает вход",
      app.colab.resets == 1 and any("Сброс" in text for text, _ in app.bot.sent),
      str(app.bot.sent)[:160])

# ── прогресс и оценка остатка: считаем по сегментам, а не по времени ──
prog = work / "progress-data" / "j1"
(prog / "logs").mkdir(parents=True, exist_ok=True)
started = time.time() - 3600
(prog / "meta.json").write_text(json.dumps({"id": "j1", "session": "s1", "name": "Проба.epub",
                                            "status": "running", "created": started, "started": started}),
                                encoding="utf-8")
(prog / "log.txt").write_text(
    "[6/12] Нарезка по предложениям + book-text.epub\n"
    "сегментов на озвучку: 1496 | абзацев: 450 | голосов: 2 | ожидаемое время звука: 142 мин (2.4 ч)\n"
    "[9/12] Синтез речи (MLX/CUDA)\n"
    "сегмент 374/1496 · seg000374 narrator: 12.0s audio / 4.1s generation; sample 4000.0s\n",
    encoding="utf-8")
state = jobs_mod.Job(prog).progress()
check("прогресс считает сегменты из журнала",
      state["segments"] == 1496 and state["done"] == 374, str(state))
check("проценты считаются по озвученным сегментам книги",
      20 <= state["percent"] <= 30 and state["stage"] == "synth",
      f"{state['percent']} % на стадии {state['stage']}")
check("остаток и время окончания оцениваются",
      state["remaining_minutes"] > 60 and state["eta"] != "" and state["stage_title"] == "синтез речи",
      f"осталось {state['remaining']} до {state['eta']}")
empty = work / "progress-empty"
empty.mkdir(parents=True, exist_ok=True)
(empty / "meta.json").write_text(json.dumps({"id": "j2", "session": "s2", "name": "Пусто",
                                             "status": "running"}), encoding="utf-8")
blank = jobs_mod.Job(empty).progress()
check("пустой журнал не ломает прогресс", blank["segments"] is None and blank["percent"] == 0,
      str(blank))
check("кнопка «Сколько осталось» распознаётся",
      botmod.menu_action("⏳ Сколько осталось") == "eta"
      and botmod.menu_action("сколько еще") == "eta" and botmod.menu_action("прогресс") == "eta")
answer = press_text("/eta")
check("команда /eta отвечает процентами и остатком",
      any("не собирается" in text or "осталось" in text for text, _ in answer), str(answer)[:160])

# ── настройки обработки: режим, порог нарезки, Drive ─────────────────────
settings_path = work / "settings.json"
options = settings_mod.Settings(settings_path)
check("умолчания: книга целиком, части по 60 мин, Drive включён",
      (options.mode, options.parts_minutes, options.drive) == (settings_mod.MODE_BOOK, 60, True)
      and options.parts_hours == 1 and "части по 60 мин" in options.summary()
      and "одна книга целиком" in options.summary(), options.summary())
message = options.update({"mode": "parts", "parts_hours": "2.5", "drive": "off"})
check("настройки меняются и сохраняются в том",
      options.mode == settings_mod.MODE_PARTS and options.parts_hours == 2.5 and options.drive is False
      and "сохранены" in message and settings_path.is_file(), message)
check("настройки переживают перезапуск (читаются из файла)",
      settings_mod.Settings(settings_path).mode == settings_mod.MODE_PARTS)
bad_options = options.update({"mode": "what", "parts_hours": "99", "drive": "on"})
check("неправильные настройки не применяются и объясняют причину",
      options.mode == settings_mod.MODE_PARTS and options.parts_hours == 2.5 and not options.drive
      and "не изменены" in bad_options and "режим" in bad_options and "длина части" in bad_options,
      bad_options)
options.update({"mode": "parts", "parts_minutes": "45", "drive": "on"})
check("длина части задаётся в минутах (45 мин — нарезке это 0.75 ч)",
      options.parts_minutes == 45 and options.parts_hours == 0.75, options.summary())
check("меньше 10 минут не принимается", "не изменены" in options.update({"mode": "parts", "parts_minutes": "5"}))
legacy = work / "legacy-settings.json"
legacy.write_text('{"mode": "parts", "parts_hours": 1.5}', encoding="utf-8")
check("прежние настройки в часах переводятся в минуты: 1.5 ч → 90 мин",
      settings_mod.Settings(legacy).parts_minutes == 90)
settings_path.write_text("{это не json", encoding="utf-8")
check("битый файл настроек не мешает работать",
      settings_mod.Settings(settings_path).mode == settings_mod.MODE_BOOK)
parts_options = settings_mod.Settings(settings_path)
parts_options.update({"mode": "parts", "parts_hours": "1.5", "drive": "on"})
check("режим печатается в журнал задания словами",
      "разбивкой на части" in parts_options.job_line() and "90 мин" in parts_options.job_line(),
      parts_options.job_line())
whole_options = settings_mod.Settings(settings_path)
whole_options.update({"mode": "book", "parts_hours": "1.5", "drive": "on"})
check("режим «целиком» пишет, что нарезка не действует",
      "без нарезки" in whole_options.job_line() and "не действует" in whole_options.job_line(),
      whole_options.job_line())

# ── части книги: план нарезки, состояние цепочки, кнопка «продолжить» ─────
plan_two = {"parts": 2, "estimated_hours": 3.0,
            "files": [{"part": "Книга — часть 1 из 2", "epub": "/tmp/p1.epub", "estimated_hours": 1.5},
                      {"part": "Книга — часть 2 из 2", "epub": "/tmp/p2.epub", "estimated_hours": 1.5}]}
parts_two = chains_mod.parts_from_plan(plan_two, pathlib.Path("/tmp/book.epub"))
check("план нарезки превращается в части с номерами",
      [part["index"] for part in parts_two] == [1, 2]
      and parts_two[1]["title"].endswith("часть 2 из 2")
      and parts_two[0]["hours"] == 1.5, str(parts_two))
parts_one = chains_mod.parts_from_plan({"parts": 1, "estimated_hours": 0.8, "files": []},
                                       pathlib.Path("/tmp/short.epub"))
check("короткая книга остаётся одной частью",
      len(parts_one) == 1 and parts_one[0]["total"] == 1 and parts_one[0]["epub"].endswith("short.epub"),
      str(parts_one))

chain_store = chains_mod.Chains(work / "data")
chain = chain_store.create("/tmp/book.epub", "Книга", "42", 1.5)
chain.set_parts(parts_two)
# Бот пользуется этими полями цепочки: пропажа любого — это ошибка в потоке нарезки,
# которую в чате видно только как «Не вышло — ...».
check("у цепочки есть всё, чем пользуется бот",
      all(hasattr(chain, name) for name in ("parts", "parts_hours", "out", "log", "status",
                                            "id", "title_of", "summary", "hold", "release",
                                            "finish", "all_done", "failed", "held",
                                            "first_unfinished", "set_parts")))
check("части сохраняются в томе", len(chain_store.by_id(chain.id).parts) == 2
      and chain.status == chains_mod.RUNNING, chain.summary())
# Цепочку двигает chains_pass, а находит он её по метке «job»: без неё части соберутся,
# а книгу никто не закроет.
linked = chains_mod.pick_for(chain_store, "/tmp/book.epub", "job-1", "Книга", "42", 1.5)
check("цепочка всегда привязана к заданию-родителю",
      linked.meta.get("job") == "job-1" and linked.id == chain.id
      and chain_store.by_id(chain.id).meta.get("job") == "job-1")
new_chain = chains_mod.pick_for(chain_store, "/tmp/other.epub", "job-2", "Другая", "42", 1.5)
check("для другой книги заводится своя цепочка",
      new_chain.id != chain.id and new_chain.meta.get("job") == "job-2")
check("повторная загрузка той же книги находит прежнюю нарезку",
      len(chain_store.of_book("/tmp/book.epub")) == 1)
chain.parts[0]["status"] = chains_mod.PART_DONE
chain.parts[1]["status"] = chains_mod.PART_RUNNING
check("строка цепочки говорит, где идёт книга",
      "часть 2 из 2" in chain.summary(), chain.summary())
chain.parts[1]["status"] = chains_mod.PART_FAILED
chain.hold("часть 2 упала дважды")
check("упавшая дважды часть останавливает цепочку, а не гонит дальше",
      chain.status == chains_mod.FAILED and chain.held() == [] and len(chain.failed()) == 1
      and "упала дважды" in chain.meta["note"], chain.summary())
check("«продолжить генерацию» возвращает в работу упавшие и придержанные части",
      [part["index"] for part in chain.release()] == [2] and chain.status == chains_mod.RUNNING
      and chain.failed() == [])
chain.parts[1]["status"] = chains_mod.PART_HELD
chain.parts[1]["retries"] = 1
check("придержанная часть тоже поднимается кнопкой",
      [part["index"] for part in chain.release()] == [2])
chain.parts[1]["status"] = chains_mod.PART_DONE
check("когда готовы все части, цепочка закрывается", chain.all_done())
chain.finish()
check("готовая цепочка не висит в активных", chain.status == chains_mod.DONE
      and chain not in chain_store.active(), chain.summary())

# Проценты книги целиком: готовые части плюс доля текущей.
part_job = jobs_mod.Job(prog)
part_job.meta.update({"parts_total": 2, "parts_done": 1, "part_index": 2, "status": "running"})
part_job.save()
label = jobs_mod.Job(prog).progress()
check("проценты считаются по книге целиком, а не по одной части",
      label["part"] == {"index": 2, "total": 2, "done": 1}, str(label.get("part")))

# ── жизненный цикл задания на заглушке конвейера ──
data = work / "data"
pipeline = HERE / "pipeline"
env = dict(os.environ, FAKE_SECONDS="1")
jobs = Jobs(data, pipeline, max_jobs=1, env=env)
book = jobs.books / "probe.epub"
book.write_bytes(b"PK fake book")
job = jobs.add("probe.epub", "test")
job.meta["book"] = str(book)
job.save()
jobs.start(job, book, gpu="T4", drive_dir="Audiobooks-colab", max_hours=7.0)
deadline = time.time() + 60
while time.time() < deadline:
    time.sleep(1)
    if jobs.refresh():
        break
done = [item for item in jobs.load() if item.id == job.id][0]
check("задание завершается и получает результат", done.status == "done" and done.meta.get("result"),
      f"статус {done.status}")
payload = done.log_payload(0)
check("журнал читается куском", payload["text"].startswith("==") and payload["offset"] > 0)
check("новых строк в журнале нет", done.log_payload(payload["offset"])["text"] == "")
check("ссылка на Drive вытаскивается из журнала", any("/folders/" in url for url in done.drive_links()))
check("ссылка-поиск Drive тоже считается ссылкой",
      bool(DRIVE_LINK_RE.search("папка: https://drive.google.com/drive/search?q=Книга")))
check("ссылка на файл Drive распознаётся",
      bool(DRIVE_LINK_RE.search("book.m4b: https://drive.google.com/file/d/"
                                "1MmNnOoPpQqRrSsTtUuVvWwXxYyZz1234/view")))
# Свежесозданная папка отдаёт от drivefs пометку «local-105»: ссылка по ней не открывается,
# такие ссылки в отчёт не берём — вместо них даём рабочий поиск по имени книги.
check("ссылка с пометкой drivefs в отчёт не попадает",
      not DRIVE_LINK_RE.search("папка: https://drive.google.com/drive/folders/local-105"))
check("поиск по имени книги собирается без идентификатора",
      done.drive_search_link().startswith("https://drive.google.com/drive/search?q=")
      and "probe" in done.drive_search_link())
check("причина про Drive берётся из журнала", "drive.google.com" in done.drive_note())
check("задание удаляется из списка", "удалено" in jobs.delete(done) and not jobs.by_id(done.id))

# ── одна книга за раз: повторный запуск при занятой машине не проходит (проверка в start()) ──
data2 = work / "data2"
jobs2 = Jobs(data2, pipeline, max_jobs=1, env=dict(os.environ, FAKE_SECONDS="30"))
long_book = jobs2.books / "долгая.epub"
long_book.write_bytes(b"PK long book")
job2 = jobs2.add("долгая.epub", "test")
job2.meta["book"] = str(long_book)
job2.save()
jobs2.start(job2, long_book)
time.sleep(0.3)                                     # дать заглушке подняться
second = jobs2.add("вторая.epub", "test")
second.meta["book"] = str(long_book)
second.save()
raised = False
try:
    jobs2.start(second, long_book)
except RuntimeError as exc:
    raised = "идёт другая сборка" in str(exc)
check("пока идёт сборка, вторую книгу запустить нельзя", raised)
jobs2.stop(job2)                                    # прибираем: tmux-сессию и машину


# ── перезапуск контейнера: очередь остаётся очередью, оборванное — находится ──
lost = jobs.add("lost.epub", "test")
cut = jobs.add("cut.epub", "test")
cut.meta.update({"status": "running", "started": 1.0, "session": "nosuchsession"})
cut.save()
check("после перезапуска задание из очереди остаётся в очереди, а оборванное находится",
      jobs.by_id(lost.id).status == "queued" and [job.id for job in jobs.interrupted()] == [cut.id])

# ── мелочи форматирования ──
check("время пишется по-русски", webmod.human_delta(90) == "1 мин" and webmod.human_delta(30) == "30 с")
check("размер пишется по-русски", webmod.human_size(2048).endswith("КБ"))

bad = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)})")
sys.exit(1 if bad else 0)
