#!/usr/bin/env python3
"""Обновление образа (перезапуск контейнера) — без сети, без Colab и без Telegram.

После перезапуска связь с прежней машиной Colab потеряна, процесс задания умер вместе с контейнером.
Бот сам продолжает, и это не сбой аккаунта:
  1. часть книги на Colab — только она заново, на том же аккаунте; готовые части не трогаются;
     брошенная машина гасится до повтора;
  2. часть, чья озвучка уже в томе, — пересобирается здесь, без повторной озвучки;
  3. отдельная книга на Colab — заново на том же аккаунте, круг профилей не тронут;
  4. сборка книги из частей и нарезка — заново сами;
  5. очередь остаётся очередью; уже готовое (архив лежит) не трогается;
  6. обрывается снова и снова — после пяти раз обычный путь сбоя.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

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


CHAT = "555"
work = pathlib.Path(tempfile.mkdtemp(prefix="restart-"))
botmod.DATA = work / "data"
botmod.STATE_FILE = work / "data" / "state.json"
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
app.in_background = lambda chat_id, task, title: task()
released, glued, split = [], [], []
app.jobs.release_machine = lambda job, timeout=60: released.append(job.id)
app.jobs.alive = lambda job: False                  # после перезапуска контейнера живых окон нет
book = work / "data" / "books" / "Книга.epub"
book.parent.mkdir(parents=True, exist_ok=True)
book.write_bytes(b"PK the book itself")
app.split_chain = lambda chain, job: split.append(chain.id)


def running(job, **meta):
    job.meta.update({"status": "running", "started": 1.0, **meta})
    job.save()
    return job


# ── книга по частям: часть 1 готова, часть 2 озвучивалась, часть 3 ждёт ─────
parent = app.jobs.add("Части.epub", CHAT, book)
chain = app.chains.create(book, "Части", CHAT, 0.5)
chain.meta["job"] = parent.id
chain.set_parts([{"index": i, "total": 3, "title": f"Части — часть {i} из 3", "epub": str(book),
                  "hours": 0.3} for i in (1, 2, 3)])
parts = []
for part in chain.parts:
    item = app.jobs.add(part["title"], CHAT, book)
    item.meta.update({"chain": chain.id, "part_index": part["index"], "parts_total": 3})
    item.save()
    part["job_id"] = item.id
    parts.append(item)
archive = work / "data" / "results" / "часть-1.zip"
archive.parent.mkdir(parents=True, exist_ok=True)
archive.write_bytes(b"PK")
parts[0].meta.update({"status": "done", "result": str(archive)})
parts[0].save()
chain.parts[0]["status"] = "done"
chain.parts[1]["status"] = "running"
chain.save()
running(parts[1], profile=1)
parent.meta.update({"status": "waiting", "chain": chain.id})
parent.save()

# ── отдельная книга на Colab и задание в очереди ────────────────────────────
whole = running(app.jobs.add("Целиком.epub", CHAT, book), profile=1)
waiting = app.jobs.add("В очереди.epub", CHAT, book)
finished = running(app.jobs.add("Успела.epub", CHAT, book))
finished.log_line(f"архив: {archive}")

lines = app.recover_after_restart()
part2 = app.jobs.by_id(parts[1].id)
check("прерванная часть — снова в очереди, на том же аккаунте",
      part2.status == "queued" and app.profiles.active() == 1 and not app.chains.by_id(chain.id).meta.get("profiles_tried"),
      part2.status)
check("заново идёт только эта часть: готовая часть 1 не тронута, вся книга с нуля не начинается",
      app.jobs.by_id(parts[0].id).status == "done" and app.jobs.by_id(parts[0].id).meta.get("result") == str(archive)
      and app.jobs.by_id(parts[2].id).status == "queued" and app.chains.by_id(chain.id).status != chains_mod.FAILED)
check("брошенную машину гасим до повтора", parts[1].id in released and whole.id in released)
check("это не сбой: в журнале сказано про обновление образа",
      "обновление образа" in part2.tail() and "запускаю заново на том же аккаунте" in part2.tail())
check("отдельная книга — тоже заново на том же аккаунте", app.jobs.by_id(whole.id).status == "queued")
check("очередь осталась очередью", app.jobs.by_id(waiting.id).status == "queued")
check("уже готовое (архив лежит) не трогается — его закроет обычная проверка",
      app.jobs.by_id(finished.id).status == "running" and finished.id not in released)
check("владельцу — список, что продолжено", len(lines) == 2 and all("на том же аккаунте" in line for line in lines),
      str(lines))
app.chains_pass()
check("часть в цепочке тоже снова в очереди (а не «упала»)",
      app.chains.by_id(chain.id).parts[1]["status"] == "queued")

# ── часть, чья озвучка уже в томе: пересобираем здесь ───────────────────────
raw = app.parts_dir(app.chains.by_id(chain.id)) / "part-3"
raw.mkdir(parents=True, exist_ok=True)
(raw / "segment.flac").write_bytes(b"fLaC")
running(app.jobs.by_id(parts[2].id), profile=1, reglued=True)
app.jobs.by_id(parts[2].id).log_line("сырьё части 3 распаковано в том")
released.clear()
started_local = []
app.jobs.start_local = lambda job, script, arguments: started_local.append((job.id, arguments))
app.recover_after_restart()
app.chains_pass()
check("озвучка части уже в томе — часть собирается здесь, без повторной озвучки и без машины",
      started_local and started_local[-1][0] == parts[2].id and "--mode" in started_local[-1][1]
      and "part" in started_local[-1][1] and parts[2].id not in released, str(started_local))

# ── сборка книги из частей прервалась — заново ──────────────────────────────
final = running(app.jobs.add("Части — сборка книги", CHAT, book), chain=chain.id, glue=True, local=True)
chain = app.chains.by_id(chain.id)
for part in chain.parts:
    part["status"] = "done"
chain.meta.update({"status": chains_mod.GLUING, "final_job": final.id})
chain.save()
started_local.clear()
app.recover_after_restart()
check("сборка книги прервалась — запущена заново сама",
      app.jobs.by_id(final.id).status == "failed" and started_local
      and "final" in started_local[-1][1], str(started_local))

# ── нарезка прервалась — режем заново ───────────────────────────────────────
other = work / "data" / "books" / "Другая.epub"
other.write_bytes(b"PK other")
slicing = app.jobs.add("Другая.epub", CHAT, other)
fresh = app.chains.create(other, "Другая", CHAT, 0.5)
fresh.meta["job"] = slicing.id
fresh.save()
slicing.meta.update({"status": "splitting", "chain": fresh.id})
slicing.save()
app.recover_after_restart()
check("нарезка прервалась — режем заново сами", fresh.id in split, str(split))

# ── обрывается снова и снова ────────────────────────────────────────────────
stubborn = running(app.jobs.add("Упрямая.epub", CHAT, book), restarts=botmod.App.RESTARTS_MAX)
app.recover_after_restart()
check("после пяти перезапусков подряд — обычный путь сбоя, без бесконечного круга",
      app.jobs.by_id(stubborn.id).status == "running")

failed = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)}, не прошло: {len(failed)})")
sys.exit(1 if failed else 0)
