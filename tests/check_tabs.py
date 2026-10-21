#!/usr/bin/env python3
"""Страница по вкладкам и логика её кнопок — без сети, без Colab и без Telegram.

  1. шесть вкладок, у каждой свой адрес; сама обновляется только «Сборки»;
  2. строка «сейчас» над вкладками: что идёт, очередь, ждущие решения;
  3. настройки по разделам; форма одного раздела не сбрасывает остальные;
     «считать модели» сначала сохраняет введённый ключ;
  4. кнопки по состоянию: в очереди — «отменить» (не «повторить»), на голосах — «остановить»,
     статус словами;
  5. «остановить» книгу по частям со страницы гасит и части (как /stop в Telegram);
     «остановить всё» — тот же путь, что в Telegram; ответ возвращает на нужную вкладку.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bot"))

import bot as botmod             # noqa: E402
import chains as chains_mod      # noqa: E402
import profiles as profiles_mod  # noqa: E402
import settings as settings_mod  # noqa: E402
import voices as voices_mod      # noqa: E402
import web as webmod             # noqa: E402
from jobs import Jobs            # noqa: E402

results = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + extra) if extra and not ok else ''}")


class FakeBot:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, preview=False, buttons=None, menu=None):
        self.sent.append(text)
        return True


CHAT = "555"
work = pathlib.Path(tempfile.mkdtemp(prefix="tabs-"))
botmod.DATA = work / "data"
app = botmod.App.__new__(botmod.App)
app.bot = FakeBot()
app.jobs = Jobs(work / "data", HERE / "pipeline", max_jobs=1, env=dict(os.environ))
app.state = {"offset": 0, "owner": CHAT, "seen": []}
app.menu_chats = set()
app.settings = settings_mod.Settings(work / "data" / "settings.json")
app.chains = chains_mod.Chains(work / "data")
app.profiles = profiles_mod.Profiles(work / "colab-config")
app.voice_packs = voices_mod.VoicePacks(work / "data")
app.awaiting_key = {}
stopped_machines = []
app.jobs.stop = lambda job: stopped_machines.append(job.id)     # машин Colab в проверке нет
notes = []
book = work / "data" / "books" / "Книга.epub"
book.parent.mkdir(parents=True, exist_ok=True)
book.write_bytes(b"PK the book itself")
models_asked = []
page = webmod.WebApp(app.jobs, settings=app.settings, chains=app.chains, voice_packs=app.voice_packs,
                     stop=app.stop_for_web, stop_all=app.stop_all_jobs, notify=notes.append,
                     refresh_models=lambda: models_asked.append(app.settings.llm_key) or "моделей 3")


def get(path: str) -> str:
    return page.route("GET", path)[2].decode("utf-8", "replace")


def post(path: str, fields: dict | None = None):
    body = urllib.parse.urlencode(fields or {}).encode()
    return page.route("POST", path, content_type="application/x-www-form-urlencoded", body=body)


# ── 1. вкладки ──────────────────────────────────────────────────────────────
tabs = {path: get(path) for path, _ in webmod.TABS}
check("все шесть вкладок открываются", all("<nav class=\"tabs\">" in body for body in tabs.values()))
check("у каждой вкладки подсвечена она сама",
      all(f'<a href="{path}" class="on">' in body for path, body in tabs.items()))
check("сама обновляется только «Сборки»",
      'data-live="1"' in tabs["/"] and all('data-live="0"' in body for path, body in tabs.items() if path != "/"))
check("на «Сборках» нет настроек и списков файлов — они на своих вкладках",
      'name="mode"' not in tabs["/"] and 'id="list-books"' not in tabs["/"] and "Новая книга" in tabs["/"])
check("«Голоса» без многоголосых книг объясняют, где включить", "Настройки → Голоса" in tabs["/voices"])

# ── 2. строка «сейчас» ──────────────────────────────────────────────────────
check("ничего не идёт — так и написано", "Сейчас ничего не собирается" in tabs["/files"])
waiting = app.jobs.add("Книга.epub", CHAT, book)
waiting.meta.update({"status": "choice"})
waiting.save()
body = get("/results")
check("ждущие решения видны с любой вкладки, со счётчиком у «Сборок»",
      "ждут вашего решения: 1" in body and 'class="count"' in body)
check("и статус в строке сборки — словами", "ждёт решения: резать ли" in get("/"))
app.jobs.delete(waiting)

# ── 3. настройки ────────────────────────────────────────────────────────────
settings_tab = tabs["/settings"]
check("настройки разбиты на разделы",
      all(f"<legend>{name}</legend>" in settings_tab
          for name in ("Как собирать книгу", "Голоса и произношение", "LLM", "Обслуживание")))
app.settings.change(keep_days=90, stall_minutes=45, stress="arbiter")
code, _, _, headers = post("/settings", {"voices": "multi"})
check("форма одного поля не сбрасывает остальные настройки",
      app.settings.voices == "multi" and app.settings.keep_days == 90 and app.settings.stall_minutes == 45
      and app.settings.stress == "arbiter" and app.settings.drive is True, str(app.settings.values))
check("после сохранения — обратно на «Настройки»", "/settings?msg=" in headers.get("Location", ""))
post("/settings", {"mode": "book", "parts_hours": "1.5"})
check("снятый флажок Drive в полной форме — «нет»", app.settings.drive is False)
post("/settings", {"mode": "book", "parts_hours": "1.5", "drive": "on"})
post("/llm/models", {"llm_provider": "ollama", "ollama_key": "new-key-777"})
check("«считать модели» сначала сохраняет введённый ключ",
      models_asked == ["new-key-777"] and app.settings.llm_key == "new-key-777", str(models_asked))
check("кнопка «считать модели» — внутри формы, ключ не теряется",
      'formaction="/llm/models"' in get("/settings"))

# ── 4. кнопки по состоянию ──────────────────────────────────────────────────
queued = app.jobs.add("Книга.epub", CHAT, book)
row = page.job_row(queued)
check("в очереди — «отменить», а не «повторить» (второй такой же книги не будет)",
      "отменить" in row and "/repeat/" not in row)
code, _, _, _ = post(f"/stop/{queued.meta['session']}")
check("«отменить» снимает задание, машину не трогает (её ещё нет)",
      app.jobs.by_id(queued.id).status == "stopped" and queued.id not in stopped_machines)

parent = app.jobs.add("Голоса.epub", CHAT, book)
cast = app.jobs.add("Голоса — голоса", CHAT, book)
cast.meta.update({"status": "running", "cast": True, "parent": parent.id})
cast.save()
parent.meta.update({"status": "casting", "cast_job": cast.id})
parent.save()
check("книга ждёт голоса — у неё «остановить»", "остановить" in page.job_row(parent))
post(f"/stop/{parent.meta['session']}")
check("и это гасит машину, которая готовит голоса",
      app.jobs.by_id(parent.id).status == "stopped" and app.jobs.by_id(cast.id).status == "stopped"
      and cast.id in stopped_machines)

# ── 5. книга по частям ──────────────────────────────────────────────────────
whole = app.jobs.add("Части.epub", CHAT, book)
chain = app.chains.create(book, "Части", CHAT, 0.5)
chain.meta["job"] = whole.id
chain.set_parts([{"index": i, "total": 2, "title": f"Части — часть {i} из 2", "epub": str(book),
                  "hours": 0.3} for i in (1, 2)])
parts = []
for part in chain.parts:
    job = app.jobs.add(part["title"], CHAT, book)
    job.meta.update({"status": "running" if part["index"] == 1 else "queued",
                     "chain": chain.id, "part_index": part["index"]})
    job.save()
    part["job_id"] = job.id
    parts.append(job)
chain.save()
whole.meta.update({"status": "waiting", "chain": chain.id})
whole.save()
row = page.job_row(app.jobs.by_id(whole.id))
check("у книги по частям — «продолжить генерацию» и «остановить»",
      "/chain/continue/" in row and f"/stop/{whole.meta['session']}" in row)
code, _, _, headers = post(f"/stop/{whole.meta['session']}")
check("«остановить» со страницы гасит и части книги, а не только её строку",
      all(app.jobs.by_id(job.id).status == "stopped" for job in parts)
      and app.chains.by_id(chain.id).status == "stopped", str([app.jobs.by_id(j.id).status for j in parts]))
check("и об этом сказано в Telegram", any("Части" in text and "остановлено" in text for text in notes))

again = app.jobs.add("Ещё.epub", CHAT, book)
again.meta.update({"status": "splitting"})
again.save()
code, _, _, headers = post("/stop")
location = urllib.parse.unquote(headers.get("Location", ""))
check("«остановить всё» — путь Telegram: останавливает и книгу в нарезке",
      app.jobs.by_id(again.id).status == "stopped" and "Остановлено заданий: 1" in location, location)
check("вход и профили возвращают на вкладку Colab",
      post("/login/reset")[3].get("Location", "").startswith("/colab")
      and post("/profile/nonsense")[3].get("Location", "").startswith("/colab"))

failed = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)}, не прошло: {len(failed)})")
sys.exit(1 if failed else 0)
