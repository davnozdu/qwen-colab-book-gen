#!/usr/bin/env python3
"""Режим частей в Telegram — то же, что на странице, без сети и без Colab.

Проверяем на живом App (Telegram и запуск машины подменены):
  1. длинная книга при включённом режиме частей — вопрос «резать?» с кнопками «да» / «нет»;
     короткая книга и режим «целиком» — без вопроса;
  2. «нет» — книга идёт одним файлом, машина её не режет (--max-hours 0); «да» — нарезка и части;
     повторное нажатие говорит «уже решено», а не запускает второй раз;
  3. готовая часть — «часть k из N готова» и что дальше; упавшая в первый раз — бот повторяет сам,
     без общего «не собралась»;
  4. упавшая дважды — цепочка стоит ПО-НАСТОЯЩЕМУ: следующие части не запускаются; в сообщении
     кнопки «повторить часть» / «нет» / «продолжить генерацию»; «повторить» поднимает цепочку;
  5. сбой сборки книги — ОДНО сообщение с кнопкой «собрать книгу», а не по сообщению на круг;
  6. страница: «повторить» у части — повтор в цепочке, а не новая книга; вопрос «резать?» есть и там.
"""
import os
import pathlib
import sys
import tempfile
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bot"))

import bot as botmod             # noqa: E402
import chains as chains_mod      # noqa: E402
import settings as settings_mod  # noqa: E402
import tgapi                     # noqa: E402
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
        self.sent.append((text, tgapi.keyboard(buttons) if buttons else ""))
        return True

    def answer(self, query_id, text=""):
        pass


def make_epub(path: pathlib.Path, chars: int) -> pathlib.Path:
    """Книга с заданным объёмом текста: по нему бот и оценивает длительность."""
    text = ("Он шёл по дороге и думал о своём. " * (chars // 34 + 1))[:chars]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OPS/c.opf"/>'
                                             '</rootfiles></container>')
        z.writestr("OPS/c.opf", '<package><manifest><item id="a" href="a.xhtml" media-type="application/'
                                'xhtml+xml"/></manifest><spine><itemref idref="a"/></spine></package>')
        z.writestr("OPS/a.xhtml", f"<html><body><p>{text}</p></body></html>")
    return path


CHAT = "555"
work = pathlib.Path(tempfile.mkdtemp(prefix="parts-tg-"))
botmod.PIPELINE = HERE / "pipeline"                    # нарезка — заглушка из tests/pipeline
app = botmod.App.__new__(botmod.App)
app.bot = FakeBot()
app.jobs = Jobs(work / "data", HERE / "pipeline", max_jobs=1, env=dict(os.environ))
app.state = {"offset": 0, "owner": CHAT, "seen": []}
app.menu_chats = set()
app.settings = settings_mod.Settings(work / "data" / "settings.json")
app.chains = chains_mod.Chains(work / "data")
app.in_background = lambda chat_id, task, title: (lambda answer: answer and app.bot.send(chat_id, answer))(task())
started = []


def fake_start(job, book, gpu="T4", drive_dir="", max_hours=7.0, extra=None):
    """Вместо машины: запоминаем, ЧТО запустили бы, и ставим задание «в работу»."""
    started.append({"job": job.id, "book": str(book), "max_hours": max_hours, "extra": extra or []})
    job.meta["status"] = "running"
    job.save()


app.jobs.start = fake_start


def press(data: str) -> list:
    app.bot.sent = []
    app.on_button({"id": "q", "data": data, "message": {"message_id": 1, "chat": {"id": CHAT}}})
    return app.bot.sent


def said(messages, *needles) -> bool:
    return any(all(needle in text for needle in needles) for text, _ in messages)


long_book = make_epub(work / "long.epub", 40000)        # ≈0.8 ч при 14 знаках в секунду
short_book = make_epub(work / "short.epub", 3000)

# ── 1. вопрос «резать?» ─────────────────────────────────────────────────────
app.settings.update({"mode": "parts", "parts_hours": "0.5", "drive": "on"})
job = app.jobs.add("long.epub", CHAT, long_book)
app.bot.sent = []
asked = app.ask_split(job, long_book)
question = app.bot.sent
check("длинная книга: бот спрашивает, резать ли её", asked and said(question, "длинная", "Резать на части"),
      str(question))
check("в вопросе две кнопки: «да, резать» и «нет, одним файлом»",
      any(f"cut:{job.id}" in markup and f"whole:{job.id}" in markup for _, markup in question))
check("пока ответа нет, книга машину не занимает и в очередь не встаёт",
      app.jobs.by_id(job.id).status == "choice" and not started)
check("ждущая ответа книга видна в «Статусе» вместе с кнопками",
      said(press("status"), "ждёт ответа") and any(f"cut:{job.id}" in markup for _, markup in app.bot.sent))
short_job = app.jobs.add("short.epub", CHAT, short_book)
check("короткая книга идёт без вопроса", not app.ask_split(short_job, short_book))
app.settings.update({"mode": "book", "parts_hours": "0.5", "drive": "on"})
other = app.jobs.add("long2.epub", CHAT, long_book)
check("в режиме «целиком» вопроса нет — режим частей выключен в админке", not app.ask_split(other, long_book))
app.settings.update({"mode": "parts", "parts_hours": "0.5", "drive": "on"})

# ── 2. ответы ───────────────────────────────────────────────────────────────
answer = press(f"whole:{job.id}")
check("«нет» — книга одним файлом", said(answer, "одним файлом"), str(answer))
check("и машина её не режет: --max-hours 0",
      started and started[-1]["job"] == job.id and started[-1]["max_hours"] == 0, str(started))
check("повторное нажатие не запускает второй раз", said(press(f"whole:{job.id}"), "уже решено")
      and len([s for s in started if s["job"] == job.id]) == 1)

cut_job = app.jobs.add("long.epub", CHAT, long_book)
app.ask_split(cut_job, long_book)
answer = press(f"cut:{cut_job.id}")
chain = app.chains.by_id(app.jobs.by_id(cut_job.id).meta.get("chain") or "")
check("«да» — книга режется на части", said(answer, "режу на части") and chain is not None
      and len(chain.parts) == 2, str(answer))
part_jobs = [app.jobs.by_id(part["job_id"]) for part in chain.parts]

# ── 3. часть готова / упала в первый раз ────────────────────────────────────
first = part_jobs[0]
first.meta.update({"status": "done", "result": str(work / "part1.zip"), "started": 0, "finished": 60})
first.save()
app.bot.sent = []
app.report(first)
check("готовая часть: «часть 1 из 2 готова» и что дальше",
      said(app.bot.sent, "часть 1", "из 2 готова", "Дальше — часть 2"), str(app.bot.sent))
check("про готовую часть нет общих слов про Google Drive", not said(app.bot.sent, "Drive"))
app.chains_pass()

second = part_jobs[1]
second.meta.update({"status": "failed", "note": "все машины заняты"})
second.save()
app.bot.sent = []
app.report(second)
check("упавшая часть не получает общего «не собралась»", not said(app.bot.sent, "не собралась"),
      str(app.bot.sent))
app.chains_pass()
check("упавшую в первый раз часть бот повторяет сам и пишет об этом",
      said(app.bot.sent, "повторяю её сам") and app.jobs.by_id(second.id).status == "queued")

# ── 4. упала дважды: цепочка стоит по-настоящему ────────────────────────────
three = app.jobs.add("long.epub", CHAT, long_book)           # третья книга — та же схема, 3 части
three.meta.update({"status": "queued"})
three.save()
chain3 = app.chains.create(long_book, "Трёхчастная", CHAT, 0.5)
chain3.meta["job"] = three.id
chain3.set_parts([{"index": i, "total": 3, "title": f"Трёхчастная — часть {i} из 3",
                   "epub": str(long_book), "hours": 0.3} for i in (1, 2, 3)])
jobs3 = []
for part in chain3.parts:
    item = app.jobs.add(part["title"], CHAT, long_book)
    item.meta.update({"chain": chain3.id, "part_index": part["index"], "parts_total": 3})
    item.save()
    part["job_id"] = item.id
    jobs3.append(item)
chain3.save()
three.meta.update({"status": "waiting", "chain": chain3.id})
three.save()
jobs3[0].meta.update({"status": "done", "result": str(work / "p1.zip")})
jobs3[0].save()
jobs3[1].meta.update({"status": "failed", "note": "сессия оборвалась"})
jobs3[1].save()
chain3.parts[1]["retries"] = 1                                # первый повтор уже был
chain3.save()
app.bot.sent = []
app.chains_pass()
chain3 = app.chains.by_id(chain3.id)
held_message = app.bot.sent
check("часть упала дважды — цепочка остановлена", chain3.status == chains_mod.FAILED, chain3.summary())
check("следующая часть придержана и в очередь не попадёт",
      app.jobs.by_id(jobs3[2].id).status == chains_mod.PART_HELD)
app.chains_pass()
check("и после следующего круга она по-прежнему придержана",
      app.jobs.by_id(jobs3[2].id).status == chains_mod.PART_HELD
      and app.chains.by_id(chain3.id).parts[2]["status"] == chains_mod.PART_HELD)
check("сообщение: причина и вопрос «повторить часть?»",
      said(held_message, "упала дважды", "сессия оборвалась", "Повторить часть?"), str(held_message))
check("под сообщением кнопки «повторить часть», «нет», «продолжить генерацию», «журнал»",
      any(f"part_retry:{jobs3[1].id}" in markup and "chain_continue:" in markup and "menu" in markup
          and "log:" in markup for _, markup in held_message), str(held_message))
answer = press(f"part_retry:{jobs3[1].id}")
check("«повторить часть» поднимает цепочку с этой части", said(answer, "продолжаю с части 2")
      and app.jobs.by_id(jobs3[1].id).status == "queued" and app.jobs.by_id(jobs3[2].id).status == "queued"
      and app.chains.by_id(chain3.id).status == chains_mod.RUNNING, str(answer))
check("повтор уже идущей части ничего не ломает", said(press(f"part_retry:{jobs3[1].id}"), "уже в работе"))

# ── 5. сбой сборки книги — одно сообщение ───────────────────────────────────
for item in jobs3:
    item.meta.update({"status": "done", "result": str(work / "p.zip")})
    item.save()
final = app.jobs.add("Трёхчастная — сборка книги", CHAT, long_book)
final.meta.update({"status": "failed", "note": "стык не сходится", "chain": chain3.id, "glue": True})
final.save()
chain3 = app.chains.by_id(chain3.id)
chain3.meta["final_job"] = final.id
chain3.save()
app.bot.sent = []
for _ in range(3):
    app.chains_pass()
glue_messages = [text for text, _ in app.bot.sent if "сборка книги не удалась" in text]
check("сбой сборки — одно сообщение за три круга, а не три", len(glue_messages) == 1, str(app.bot.sent))
check("под ним кнопка «собрать книгу»", any(f"chain_glue:{chain3.id}" in markup for _, markup in app.bot.sent))

# ── 6. страница — то же самое ───────────────────────────────────────────────
page = webmod.WebApp(app.jobs, launch=app.launch, settings=app.settings, chains=app.chains,
                     continue_chain=app.continue_chain, glue=app.glue_chain, ask_split=app.ask_split,
                     choose_split=app.choose_split, retry_part=app.retry_part)
lone = app.jobs.add("Одна — часть 1 из 2", CHAT, long_book)
chain4 = app.chains.create(long_book, "Одна", CHAT, 0.5)
chain4.set_parts([{"index": 1, "total": 2, "title": "Одна — часть 1 из 2", "epub": str(long_book), "hours": 0.3},
                  {"index": 2, "total": 2, "title": "Одна — часть 2 из 2", "epub": str(long_book), "hours": 0.3}])
chain4.parts[0]["job_id"] = lone.id
chain4.parts[0]["status"] = chains_mod.PART_FAILED
chain4.save()
lone.meta.update({"status": "failed", "chain": chain4.id, "part_index": 1})
lone.save()
before = len(app.jobs.load())
page.route("POST", f"/repeat/{lone.meta['session']}")
check("страница: «повторить» у части — повтор в цепочке, а не новая книга",
      len(app.jobs.load()) == before and app.jobs.by_id(lone.id).status == "queued",
      f"заданий было {before}, стало {len(app.jobs.load())}")
asking = app.jobs.add("long.epub", CHAT, long_book)
app.ask_split(asking, long_book)
html_page = page.route("GET", "/")[2].decode("utf-8", "replace")
check("страница: у ждущей книги те же две кнопки", f"/choice/{asking.meta['session']}/cut" in html_page
      and f"/choice/{asking.meta['session']}/whole" in html_page)
page.route("POST", f"/choice/{asking.meta['session']}/whole")
check("страница: ответ «одним файлом» запускает книгу без нарезки",
      started[-1]["job"] == asking.id and started[-1]["max_hours"] == 0)

# ── 7. сторож зависаний ─────────────────────────────────────────────────────
stopped = []
app.jobs.stop = lambda job: stopped.append(job.id)
check("порог зависания по умолчанию — 30 минут", settings_mod.DEFAULTS["stall_minutes"] == 30
      and "30 мин без движения" in app.settings.summary(), app.settings.summary())
alive = app.jobs.add("alive.epub", CHAT, short_book)
alive.meta.update({"status": "running", "started": __import__("time").time()})
alive.save()
alive.log_line("сегмент 10/200")
hung = app.jobs.add("hung.epub", CHAT, short_book)
hung.meta.update({"status": "running", "started": __import__("time").time() - 3 * 3600})
hung.save()
old = __import__("time").time() - 45 * 60
os.utime(hung.log, (old, old))
app.bot.sent = []
app.watchdog()
check("живую сборку (журнал растёт) сторож не трогает", alive.id not in stopped
      and app.jobs.by_id(alive.id).status == "running")
check("сборку без движения 45 мин сторож гасит и освобождает машину", hung.id in stopped
      and app.jobs.by_id(hung.id).status == "failed" and "зависла" in app.jobs.by_id(hung.id).meta["note"])
check("и пишет предупреждение с кнопкой «повторить»", said(app.bot.sent, "нет движения", "зависла")
      and any(f"repeat:{hung.meta['session']}" in markup for _, markup in app.bot.sent), str(app.bot.sent))
again = app.jobs.add("hung2.epub", CHAT, short_book)
again.meta.update({"status": "running", "started": old})
again.save()
os.utime(again.log, (old, old))
app.settings.update({"mode": "parts", "parts_hours": "0.5", "drive": "on", "stall_minutes": "0"})
app.watchdog()
check("порог 0 — сторож выключен", again.id not in stopped)
check("неверный порог админка не принимает",
      "не изменены" in app.settings.update({"mode": "parts", "parts_hours": "0.5", "stall_minutes": "2"}))

# ── 8. местная сборка книги жива, даже когда в образе есть tmux ─────────────
import subprocess as _sp
real = Jobs(work / "local-data", HERE / "pipeline", max_jobs=1, env=dict(os.environ))
real.has_tmux = True                                   # как в контейнере
local_job = real.add("сборка книги", CHAT, short_book)
script = work / "slow.py"
script.write_text("import time; time.sleep(3)", encoding="utf-8")
real.start_local(local_job, script, [])
check("местная сборка книги считается живой и при tmux в образе", real.alive(real.by_id(local_job.id)))
real.refresh()
check("и не падает с «архива нет», пока идёт", real.by_id(local_job.id).status == "running")
__import__("time").sleep(4)
check("а когда процесс закончился — завершается", not real.alive(real.by_id(local_job.id)))

bad = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)})")
sys.exit(1 if bad else 0)
