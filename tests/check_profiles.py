#!/usr/bin/env python3
"""Профили Google Colab — без сети, без Colab и без Telegram.

Проверяем на живом App (Telegram и запуск машины подменены, папка настроек — временная):
  1. прежний вход переносится в профиль 1, папка CLI становится ссылкой на активный профиль;
  2. «выбрать», «выйти», «удалить» (номера сдвигаются), «добавить», «выйти из всех»;
  3. сбой: второй раз на том же профиле, потом следующий профиль, профиль без входа — пауза
     с кнопкой «войти», после входа — продолжение само; круг пройден — пауза с «продолжить»;
  4. часть книги: дважды упала — следующий профиль, а не остановка цепочки;
  5. страница: у каждого профиля своя кнопка «войти», и запуск пишет, под каким профилем идёт.
"""
import json
import os
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bot"))

import bot as botmod             # noqa: E402
import chains as chains_mod      # noqa: E402
import profiles as profiles_mod  # noqa: E402
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


class FakeLogin:
    def __init__(self):
        self.begun = []
        self.dropped = 0

    def drop(self):
        self.dropped += 1

    def begin(self):
        self.begun.append(app.profiles.active())
        return f"ссылка для входа в профиль {app.profiles.active()}"

    def awaiting_code(self):
        return False


def said(sent, *words) -> bool:
    return any(all(word in text for word in words) for text, _ in sent)


def sign_in(number: int, email: str = "") -> None:
    folder = app.profiles.path(number)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "token.json").write_text('{"refresh_token": "x"}')
    if email:                                  # адрес, как его запомнил бы lookup() после входа
        (folder / "account.json").write_text(json.dumps(
            {"email": email, "token_mtime": (folder / "token.json").stat().st_mtime}))


CHAT = "555"
work = pathlib.Path(tempfile.mkdtemp(prefix="profiles-"))
botmod.STATE_FILE = work / "data" / "state.json"     # счёт частей на аккаунт — в томе проверки
config = work / "home" / ".config"
old = config / "colab-cli"
old.mkdir(parents=True)
(old / "token.json").write_text('{"refresh_token": "old"}')
(old / "sessions.json").write_text("{}")

app = botmod.App.__new__(botmod.App)
app.bot = FakeBot()
app.jobs = Jobs(work / "data", HERE / "pipeline", max_jobs=1, env=dict(os.environ))
app.state = {"offset": 0, "owner": CHAT, "seen": []}
app.menu_chats = set()
app.settings = settings_mod.Settings(work / "data" / "settings.json")
app.chains = chains_mod.Chains(work / "data")
app.colab = FakeLogin()
app.profiles = profiles_mod.Profiles(old, count=lambda: app.settings.profiles)
app.in_background = lambda chat_id, task, title: (lambda answer: answer and app.bot.send(chat_id, answer))(task())
started = []


def fake_start(job, book, gpu="T4", drive_dir="", max_hours=7.0, extra=None, secrets=None):
    started.append({"job": job.id, "profile": app.profiles.active()})
    job.meta.update({"status": "running", "started": 1.0, "book": str(book)})
    job.save()


app.jobs.start = fake_start
app.jobs.live_running = lambda: []          # машина свободна: профиль менять можно

# ── 1. перенос прежнего входа ────────────────────────────────────────────────
check("по умолчанию профиль один", settings_mod.DEFAULTS["profiles"] == 1 and app.profiles.count() == 1)
app.profiles.prepare()
check("прежний вход перенесён в профиль 1",
      (config / "colab-profiles" / "1" / "token.json").read_text() == '{"refresh_token": "old"}')
check("папка CLI — ссылка на профиль 1, вход виден через неё",
      old.is_symlink() and (old / "token.json").exists() and app.profiles.active() == 1)
app.profiles.prepare()
check("повторная подготовка ничего не ломает", old.is_symlink() and app.profiles.active() == 1)

# ── 2. кнопки профилей ───────────────────────────────────────────────────────
message = app.profile_action("add")
check("«добавить профиль» — профилей стало два, число сохранено в настройках",
      app.settings.profiles == 2 and app.profiles.count() == 2 and "профиль 2" in message, message)
app.profile_action("add")
check("третий профиль", app.profiles.count() == 3 and app.profiles.path(3).is_dir())
message = app.profile_action("activate", 2)
check("«выбрать» переключает ссылку и сообщает, что входа нет",
      app.profiles.active() == 2 and "входа нет" in message and os.readlink(old).endswith("2"), message)
check("при переключении начатый вход прежнего профиля закрывается", app.colab.dropped >= 1)
app.bot.sent = []
app.do_action(CHAT, "profile_login:3")
check("«войти» у профиля 3 переключает на него и даёт ссылку именно для него",
      app.colab.begun[-1] == 3 and said(app.bot.sent, "профиль 3"), str(app.bot.sent))
sign_in(2, "second@gmail.com")
sign_in(3)
app.bot.sent = []
app.do_action(CHAT, "profiles")
markup = app.bot.sent[-1][1] if app.bot.sent else ""
check("Telegram: у каждого из трёх профилей своя кнопка «войти»",
      all(f"profile_login:{n}" in markup for n in (1, 2, 3)) and "profile_add" in markup
      and "profile_logout_all_ask" in markup, markup)
check("Telegram: в списке адрес аккаунта и отметка активного",
      said(app.bot.sent, "second@gmail.com", "▶️ Профиль 3"), str(app.bot.sent))
message = app.profile_action("logout", 3)
check("«выйти» — вход профиля отложен, папка осталась",
      not app.profiles.signed_in(3) and app.profiles.path(3).is_dir()
      and any(p.name.startswith("token.json.backup-") for p in app.profiles.path(3).iterdir()), message)
sign_in(3, "third@gmail.com")
message = app.profile_action("delete", 2)
check("«удалить» профиль 2 — профиль 3 стал вторым, число уменьшилось",
      app.settings.profiles == 2 and app.profiles.account(2) == "third@gmail.com"
      and not app.profiles.path(3).exists(), message)
check("активный профиль сдвинулся вместе с номером", app.profiles.active() == 2)
check("последний профиль не удаляется",
      "не удаляется" in profiles_mod.Profiles(old, count=lambda: 1).delete(1)[0])
app.jobs.live_running = lambda: [type("J", (), {"meta": {}})()]
message = app.profile_action("activate", 1)
check("под идущей сборкой профиль не меняется", app.profiles.active() == 2 and "нельзя" in message, message)
app.jobs.live_running = lambda: []
app.profile_action("logout_all")
check("«выйти из всех» — ни в одном профиле нет входа",
      not any(app.profiles.signed_in(n) for n in app.profiles.numbers()))

# ── 3. сбои отдельной книги: повтор, следующий профиль, пауза ───────────────
app.profile_action("add")                       # профили 1, 2, 3; вход есть в 1 и 2, в 3 — нет
sign_in(1, "first@gmail.com")
sign_in(2, "second@gmail.com")
app.profiles.activate(1)
book = work / "book.epub"
book.write_bytes(b"PK fake")
job = app.jobs.add("book.epub", CHAT, book)
app.bot.sent = []
app.launch(job)
check("запуск пишет в Telegram, под каким профилем и аккаунтом идёт сборка",
      said(app.bot.sent, "book.epub", "профиль 1", "first@gmail.com"), str(app.bot.sent))
check("и это же есть в журнале задания", "first@gmail.com" in app.jobs.by_id(job.id).tail())


def fail(note: str = "Все машины Colab заняты") -> None:
    current = app.jobs.by_id(job.id)
    current.meta.update({"status": "failed", "note": note, "started": 1.0})
    current.save()
    app.bot.sent = []
    app.after_failure(current)


fail()
check("сбой при нескольких профилях — сразу следующий профиль, без повтора на том же",
      app.profiles.active() == 2 and app.jobs.by_id(job.id).status == "queued"
      and said(app.bot.sent, "Перехожу на профиль 2") and not said(app.bot.sent, "на том же профиле"),
      str(app.bot.sent))
fail()
check("сбой на профиле 2, у профиля 3 нет входа — пауза с кнопкой «войти в профиль 3»",
      app.jobs.by_id(job.id).status == "paused" and app.profiles.active() == 3
      and any("profile_login:3" in markup for _, markup in app.bot.sent), str(app.bot.sent))
app.bot.sent = []
app.resume_after_login()
check("без входа пауза держится", app.jobs.by_id(job.id).status == "paused")
sign_in(3)
app.resume_after_login()
check("вошли в профиль 3 — сборка продолжается сама",
      app.jobs.by_id(job.id).status == "queued" and said(app.bot.sent, "продолжаю"), str(app.bot.sent))
fail()
paused = app.jobs.by_id(job.id)
check("упала и на профиле 3 — круг пройден, пауза с «продолжить»",
      paused.status == "paused"
      and any(f"resume:{job.meta['session']}" in markup for _, markup in app.bot.sent)
      and said(app.bot.sent, "пауза", "1, 2, 3"), str(app.bot.sent))
check("и бот сам назначил повтор через 4 часа (по умолчанию)",
      3.9 * 3600 < float(paused.meta.get("retry_at") or 0) - time.time() <= 4 * 3600
      and said(app.bot.sent, "Сама попробую снова", "через 4 ч"), str(paused.meta.get("retry_at")))
app.scheduled_retries()
check("до назначенного времени ничего не происходит", app.jobs.by_id(job.id).status == "paused")
paused.meta["retry_at"] = time.time() - 1          # время подошло
paused.save()
app.bot.sent = []
app.scheduled_retries()
current = app.jobs.by_id(job.id)
check("время вышло — круг заново со следующего профиля, сам, без нажатия",
      current.status == "queued" and not current.meta.get("profiles_tried") and not current.meta.get("retry_at")
      and app.profiles.active() == 1 and said(app.bot.sent, "⏰"), f"{current.status}, {app.profiles.active()}")
app.settings.change(retry_hours=0)
for _ in range(3):
    fail()
check("повтор выключен (0 ч) — пауза ждёт человека, без расписания",
      app.jobs.by_id(job.id).status == "paused" and not app.jobs.by_id(job.id).meta.get("retry_at"))
app.settings.change(retry_hours=4)
app.bot.sent = []
app.do_action(CHAT, f"resume:{job.meta['session']}")
current = app.jobs.by_id(job.id)
check("«продолжить» — круг заново", current.status == "queued" and not current.meta.get("profiles_tried"),
      str(app.bot.sent))
web = webmod.WebApp(app.jobs, settings=app.settings, chains=app.chains, profiles=app.profiles,
                    profile_action=app.profile_action, resume=app.resume_job,
                    profile_label=app.profile_label, login=None)
for _ in range(3):                              # круг заново с активного: по одному сбою на профиль
    fail()
check("пауза снова после полного круга", app.jobs.by_id(job.id).status == "paused")
page = web.jobs_page(1) + web.colab_card()      # «Сборки» и вкладка «Google Colab»
check("страница: у паузы кнопка «продолжить»", f"/resume/{job.meta['session']}" in page)
check("страница: у каждого профиля своя кнопка «войти», есть «добавить» и «выйти из всех»",
      all(f"/profile/login/{n}" in page for n in (1, 2, 3)) and "/profile/add" in page
      and "/profile/logout_all" in page)
check("страница: профили добавляются и удаляются кнопками на вкладке Colab",
      "/profile/add" in page and "/profile/delete/" in page)
check("страница: на вкладке Colab — частей на аккаунт и пауза повтора",
      'name="parts_per_profile"' in page and 'name="retry_hours"' in page and 'value="/colab"' in page)
code, _, _, headers = web.route("POST", "/settings", content_type="application/x-www-form-urlencoded",
                                body=b"back=%2Fcolab&parts_per_profile=3&retry_hours=6")
check("страница: сохранение — обратно на вкладку Colab, остальные настройки не тронуты",
      app.settings.parts_per_profile == 3 and app.settings.retry_hours == 6
      and headers.get("Location", "").startswith("/colab") and app.settings.profiles == 3, str(headers))
app.settings.change(parts_per_profile=2, retry_hours=4)
code, _, _, headers = web.route("POST", f"/resume/{job.meta['session']}")
check("страница: «продолжить» работает", app.jobs.by_id(job.id).status == "queued")
web.route("POST", "/profile/activate/2")
check("страница: «выбрать» профиль", app.profiles.active() == 2)
app.bot.sent = []
app.do_action(CHAT, "per_profile:1")
check("Telegram: «➕» у частей на аккаунт", app.settings.parts_per_profile == 3)
app.do_action(CHAT, "retry_hours:-1")
check("Telegram: «➖» у паузы повтора", app.settings.retry_hours == 3)
check("Telegram: на экране профилей видно правило по кругу",
      said(app.bot.sent, "по 3 част. на аккаунт"), str(app.bot.sent[-1:]))
app.settings.change(parts_per_profile=2, retry_hours=4)

# ── 4. часть книги: упала — сразу следующий профиль ─────────────────────────
app.profiles.activate(1)
parent = app.jobs.add("parts.epub", CHAT, book)
chain = app.chains.create(book, "Части", CHAT, 0.5)
chain.meta["job"] = parent.id
chain.set_parts([{"index": i, "total": 4, "title": f"Части — часть {i} из 4", "epub": str(book),
                  "hours": 0.3} for i in (1, 2, 3, 4)])
part_jobs = []
for part in chain.parts:
    item = app.jobs.add(part["title"], CHAT, book)
    item.meta.update({"chain": chain.id, "part_index": part["index"], "parts_total": 4})
    item.save()
    part["job_id"] = item.id
    part_jobs.append(item)
chain.save()
parent.meta.update({"status": "waiting", "chain": chain.id})
parent.save()
part_jobs[0].meta.update({"status": "failed", "note": "сессия оборвалась"})
part_jobs[0].save()
app.bot.sent = []
app.chains_pass()
chain = app.chains.by_id(chain.id)
check("часть упала один раз — сразу профиль 2, без повтора на том же",
      chain.status != chains_mod.FAILED and app.profiles.active() == 2
      and app.jobs.by_id(part_jobs[0].id).status == "queued" and said(app.bot.sent, "Перехожу на профиль 2"),
      str(app.bot.sent))

# ── 5. по кругу: N частей на аккаунт — следующий ────────────────────────────
def finish(item, profile: int) -> None:
    item = app.jobs.by_id(item.id)
    item.meta.update({"status": "done", "result": str(work / f"{item.id}.zip"), "profile": profile})
    item.save()
    app.chains_pass()


app.state["profile_streak"] = {}
finish(part_jobs[0], 2)
check("часть готова — +1 к частям подряд на её аккаунте",
      app.state["profile_streak"] == {"profile": 2, "parts": 1}, str(app.state.get("profile_streak")))
app.chains_pass()
check("повторный проход не считает ту же часть второй раз", app.state["profile_streak"]["parts"] == 1)
app.rotate_if_due(part_jobs[1])
check("одна часть из двух — аккаунт не меняем", app.profiles.active() == 2)
finish(part_jobs[1], 2)
app.bot.sent = []
app.launch(app.jobs.by_id(part_jobs[2].id))
check("две части на профиле 2 — следующая часть уходит на профиль 3",
      app.profiles.active() == 3 and app.jobs.by_id(part_jobs[2].id).meta.get("profile") == 3
      and said(app.bot.sent, "уже 2 част. подряд"), str(app.bot.sent))
check("и счёт у нового аккаунта начинается с нуля", app.state["profile_streak"] == {"profile": 3, "parts": 0})
app.state["profile_streak"] = {"profile": 3, "parts": 2}
app.rotate_if_due(part_jobs[3])
check("после последнего профиля — снова первый (по кругу)", app.profiles.active() == 1)
app.settings.change(parts_per_profile=0)
app.state["profile_streak"] = {"profile": 1, "parts": 9}
app.rotate_if_due(part_jobs[3])
check("0 частей на аккаунт — по счёту не переключаемся", app.profiles.active() == 1)
app.settings.change(parts_per_profile=2)

# ── 6. часть упала на всех профилях — повтор по расписанию ──────────────────
chain = app.chains.by_id(chain.id)
chain.meta["profiles_tried"] = [2, 3]
chain.save()
part_jobs[3] = app.jobs.by_id(part_jobs[3].id)
part_jobs[3].meta.update({"status": "failed", "note": "машин нет"})
part_jobs[3].save()
app.bot.sent = []
app.chains_pass()
chain = app.chains.by_id(chain.id)
check("часть упала на всех профилях — книга встала, повтор назначен",
      chain.status == chains_mod.FAILED and float(chain.meta.get("retry_at") or 0) > time.time()
      and said(app.bot.sent, "Сама попробую снова"), str(app.bot.sent))
chain.meta["retry_at"] = time.time() - 1
chain.save()
app.bot.sent = []
app.scheduled_retries()
chain = app.chains.by_id(chain.id)
check("время вышло — генерация продолжается сама, круг профилей заново",
      chain.status != chains_mod.FAILED and not chain.meta.get("profiles_tried")
      and app.jobs.by_id(part_jobs[3].id).status == "queued" and said(app.bot.sent, "⏰"), str(app.bot.sent))

failed = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)}, не прошло: {len(failed)})")
sys.exit(1 if failed else 0)
