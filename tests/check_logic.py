#!/usr/bin/env python3
"""Быстрые проверки логики бота — без Telegram, без Colab, без сети.

Запускается из smoke-теста (и в CI, и локально). Проверяем то, что легко сломать незаметно:
разбор форм, защиту от путей, распознавание кода авторизации, объяснение ошибок,
жизненный цикл задания и удаление.
"""
import os, pathlib, sys, tempfile, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bot"))

import bot as botmod          # noqa: E402
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
app = botmod.App.__new__(botmod.App)                 # без __init__: сеть и токен не нужны
app.bot = FakeBot()
app.jobs = Jobs(work / "btn-data", HERE / "pipeline", max_jobs=1, env=dict(os.environ))
app.state = {"offset": 0, "owner": CHAT, "seen": []}
app.menu_chats = set()                               # ставится в __init__, здесь его нет
app.colab = type("Login", (), {"awaiting_code": lambda self: False})()


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


# ── незапустившееся задание не висит вечно ──
lost = jobs.add("lost.epub", "test")
check("застрявшее задание помечается неудачным", jobs.fail_stale_queued() == 1 and
      jobs.by_id(lost.id).status == "failed")

# ── мелочи форматирования ──
check("время пишется по-русски", webmod.human_delta(90) == "1 мин" and webmod.human_delta(30) == "30 с")
check("размер пишется по-русски", webmod.human_size(2048).endswith("КБ"))

bad = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)})")
sys.exit(1 if bad else 0)
