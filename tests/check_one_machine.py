#!/usr/bin/env python3
"""Одна машина Colab на все аккаунты — без сети, Colab CLI подменён.

  1. перед подъёмом машины бот смотрит сессии ВСЕХ аккаунтов со входом и гасит оставшиеся,
     в том числе «чужие» (без имени); потом запускает;
  2. машина не гаснет — новую не поднимаем: задание ждёт в очереди, в Telegram одно сообщение,
     проверка не чаще раза в минуту; погасла — запускаем;
  3. нарезку книги на части (в контейнере) чужая машина не задерживает;
  4. потолок: пока идёт машина, вторую не поднять и очередь не берёт задания — даже при MAX_JOBS > 1.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bot"))

import bot as botmod             # noqa: E402
import chains as chains_mod      # noqa: E402
import profiles as profiles_mod  # noqa: E402
import settings as settings_mod  # noqa: E402
import voices as voices_mod      # noqa: E402
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


class FakeColab:
    def drop(self):
        pass


class Done:
    def __init__(self, stdout=""):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


CHAT = "555"
work = pathlib.Path(tempfile.mkdtemp(prefix="one-machine-"))
botmod.DATA = work / "data"
botmod.STATE_FILE = work / "data" / "state.json"
app = botmod.App.__new__(botmod.App)
app.bot = FakeBot()
app.colab = FakeColab()
app.jobs = Jobs(work / "data", HERE / "pipeline", max_jobs=1, env=dict(os.environ))
app.state = {"offset": 0, "owner": CHAT, "seen": []}
app.menu_chats = set()
app.settings = settings_mod.Settings(work / "data" / "settings.json")
app.settings.change(profiles=3)
app.chains = chains_mod.Chains(work / "data")
app.profiles = profiles_mod.Profiles(work / "home" / ".config" / "colab-cli", count=lambda: app.settings.profiles)
app.profiles.prepare()
for number in (1, 2, 3):
    app.profiles.ensure(number)
    (app.profiles.folder(number) / "token.json").write_text('{"token": "x"}')
app.voice_packs = voices_mod.VoicePacks(work / "data")
app.awaiting_key = {}
app.profile_label = lambda number=0: f"профиль {number or app.profiles.active()}"

# Colab CLI: у каждого аккаунта свои живые машины; stuck — машины, которые не гаснут.
machines = {1: [], 2: [], 3: []}
stuck = set()
calls = []


def fake_cli(number, *arguments, timeout=90):
    calls.append((number, arguments))
    if arguments[0] == "sessions":
        return Done("".join(f"[{name}] {endpoint} | T4 | RUNNING\n" for name, endpoint in machines[number]))
    if arguments[0] == "stop":
        name = arguments[2]
        if name.startswith("orphan-"):
            state = json.loads((app.profiles.folder(number) / "sessions.json").read_text())
            endpoint = state[name]["endpoint"]
            machines[number] = [m for m in machines[number] if m[1] != endpoint or (number, endpoint) in stuck]
        else:
            machines[number] = [m for m in machines[number] if m[0] != name or (number, m[1]) in stuck]
    return Done()


app.colab_cli = fake_cli
real_which = shutil.which
botmod.shutil.which = lambda name, *a, **k: "/fake/colab" if name == "colab" else real_which(name, *a, **k)
started = []


def fake_start(job, book, gpu="T4", drive_dir="", max_hours=7.0, extra=None, secrets=None):
    started.append(job.id)
    job.meta.update({"status": "running", "started": time.time()})
    job.save()


app.jobs.start = fake_start
book = work / "data" / "books" / "Книга.epub"
book.parent.mkdir(parents=True, exist_ok=True)
book.write_bytes(b"PK the book itself")

check("у каждого аккаунта свой «дом» для CLI: его папка colab-cli — папка этого профиля",
      (app.profiles.home(2) / ".config" / "colab-cli").resolve() == app.profiles.folder(2).resolve()
      and (app.profiles.home(3) / ".config" / "colab-cli" / "token.json").is_file())

# ── 1. осталась машина на другом аккаунте — гасим и запускаем ───────────────
machines[2] = [("qwen-old", "https://colab/aaa")]
machines[3] = [("?", "https://colab/orphan")]
job = app.jobs.add("Книга.epub", CHAT, book)
app.launch(job)
check("перед запуском смотрим сессии всех трёх аккаунтов, а не только активного",
      {number for number, arguments in calls if arguments[0] == "sessions"} == {1, 2, 3}, str(calls))
check("машина на профиле 2 погашена до запуска", (2, ("stop", "-s", "qwen-old")) in calls)
check("и «чужая» (без имени) на профиле 3 — тоже", (3, ("stop", "-s", "orphan-1")) in calls
      and not machines[3])
check("после этого задание запущено", started == [job.id])
check("в журнале — почему гасили", "две машины сразу нельзя" in app.jobs.by_id(job.id).tail())

# ── 2. машина не гаснет — новую не поднимаем ────────────────────────────────
app.jobs.by_id(job.id).meta.update({"status": "done"})
app.jobs.by_id(job.id).save()
machines[2] = [("qwen-stuck", "https://colab/bbb")]
stuck.add((2, "https://colab/bbb"))
second = app.jobs.add("Книга.epub", CHAT, book)
app.bot.sent = []
app.launch(second)
second = app.jobs.by_id(second.id)
check("машина на профиле 2 не гаснет — вторую не поднимаем, задание ждёт в очереди",
      second.id not in started and second.status == "queued" and second.meta.get("machine_wait"))
check("в Telegram сказано один раз, почему ждём", len([t for t in app.bot.sent if "две сразу нельзя" in t]) == 1,
      str(app.bot.sent))
calls.clear()
app.launch(second)
check("повтор раньше минуты не дёргает Colab и не пишет снова",
      not calls and len([t for t in app.bot.sent if "две сразу нельзя" in t]) == 1)
stuck.clear()
machines[2] = []
second.meta["machine_checked"] = time.time() - 120
second.save()
app.launch(second)
check("машина погасла — запускаем, в журнале отмечено",
      second.id in started and "других машин Colab нет — запускаю" in app.jobs.by_id(second.id).tail())

# ── 3. нарезку чужая машина не задерживает ──────────────────────────────────
app.settings.change(mode="parts")
check("книга целиком в режиме частей (нарезка здесь) машину не поднимает",
      not app.needs_machine(app.jobs.add("Другая.epub", CHAT, book)))
part = app.jobs.add("Часть", CHAT, book)
part.meta.update({"chain": "c1", "part_index": 1})
check("а часть книги — поднимает", app.needs_machine(part))
app.settings.change(mode="book")

# ── 4. потолок: машина одна при любом MAX_JOBS ──────────────────────────────
wide = Jobs(work / "wide", HERE / "pipeline", max_jobs=3, env=dict(os.environ))
wide.alive = lambda job: True
busy = wide.add("Идёт.epub", CHAT, book)
busy.meta.update({"status": "running", "started": time.time()})
busy.save()
waiting = wide.add("Ждёт.epub", CHAT, book)
check("MAX_JOBS = 3, а машина уже идёт — очередь не берёт следующее", wide.free_slot() is False)
try:
    wide.start(waiting, book)
    refused = False
except RuntimeError as exc:
    refused = "вторую одновременно не поднимаю" in str(exc)
check("и прямой запуск второй машины отказан", refused)
local = wide.add("Сборка книги", CHAT, book)
busy.meta.update({"status": "done"})
busy.save()
local.meta.update({"status": "running", "local": True, "started": time.time()})
local.save()
check("местная сборка в контейнере машину не занимает — очередь свободна", wide.free_slot() is True)

# ── 5. очередь к машине — по порядку частей ─────────────────────────────────
# «Идиот»: часть 16 ждала, пока погаснет чужая машина (повтор проверки — раз в минуту), а очередь шла
# дальше, и следующая часть проверяла сама: машина погасла — уехала она. Так 26 и 44 обогнали 16.
app.settings.change(mode="parts")
app.chains_pass = lambda: None
chain = app.chains.create(book, "Идиот", CHAT, 0.5)
members = []
for index in (1, 2, 3):
    member = app.jobs.add(f"Идиот — часть {index}", CHAT, book)
    member.meta.update({"chain": chain.id, "part_index": index, "parts_total": 3})
    member.save()
    members.append(member)
chain.set_parts([{"index": m.meta["part_index"], "job_id": m.id, "epub": str(book), "hours": 0.5}
                 for m in members])
first = app.jobs.by_id(members[0].id)
first.meta.update({"machine_wait": True, "machine_checked": time.time()})   # только что проверяла
first.save()
machines[2] = []                                                            # а машина уже погасла
started.clear()
app.queue_pass()
check("первая часть ждёт своей минуты — следующие не уезжают вперёд неё", started == [], str(started))
first = app.jobs.by_id(members[0].id)
first.meta["machine_checked"] = time.time() - 120
first.save()
app.queue_pass()
check("подошла минута — запускается именно первая часть", started == [members[0].id], str(started))
third = app.jobs.by_id(members[2].id)
app.launch(third)
check("часть 3 не запускается, пока не готова часть 2 (даже кнопкой)",
      members[2].id not in started and app.jobs.by_id(members[2].id).status == "queued")
for member, status in ((members[0], "done"), ):
    member = app.jobs.by_id(member.id)
    member.meta["status"] = status
    member.save()
chain = app.chains.by_id(chain.id)
chain.parts[0]["status"] = chains_mod.PART_DONE
chain.save()
app.queue_pass()
check("часть 1 готова — дальше идёт часть 2, а не 3", started[-1:] == [members[1].id], str(started))
app.settings.change(mode="book")

failed = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)}, не прошло: {len(failed)})")
sys.exit(1 if failed else 0)
