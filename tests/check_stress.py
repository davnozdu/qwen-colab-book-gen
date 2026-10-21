#!/usr/bin/env python3
"""Ударения и ё в боте — без сети, без Colab и без Telegram.

  1. по умолчанию Silero, как раньше: заданию не передаётся ничего нового;
  2. «только словарь» и «Silero + словарь + LLM» уходят заданию как --stress;
  3. у арбитра ключ и модель — те же, что у озвучки (окружением, не командной строкой);
     без ключа в журнале сказано, что споры решит гибрид;
  4. страница: выбор в настройках, сохранение формой, неизвестный способ не принимается;
  5. части книги получают тот же режим.
"""
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
import voices as voices_mod      # noqa: E402
import web as webmod             # noqa: E402
from jobs import Jobs            # noqa: E402

results = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + extra) if extra and not ok else ''}")


class FakeBot:
    def send(self, chat_id, text, preview=False, buttons=None, menu=None):
        return True


CHAT = "555"
work = pathlib.Path(tempfile.mkdtemp(prefix="stress-"))
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
started = []


def fake_start(job, book, gpu="T4", drive_dir="", max_hours=7.0, extra=None, secrets=None):
    started.append({"job": job.id, "extra": list(extra or []), "secrets": dict(secrets or {})})
    job.meta.update({"status": "running", "started": time.time(), "book": str(book)})
    job.save()


app.jobs.start = fake_start
app.jobs.live_running = lambda: []
book = work / "Книга.epub"
book.write_bytes(b"PK the book itself")


def launch():
    job = app.jobs.add("Книга.epub", CHAT, book)
    app.launch(job)
    return app.jobs.by_id(job.id), started[-1]


def stress_of(run) -> str:
    extra = run["extra"]
    return extra[extra.index("--stress") + 1] if "--stress" in extra else ""


# ── 1. по умолчанию — как было ──────────────────────────────────────────────
check("по умолчанию ударения ставит Silero", app.settings.stress == "silero")
job, run = launch()
check("Silero: заданию не передаётся --stress (путь прежний)", "--stress" not in run["extra"], str(run["extra"]))
check("в журнале задания видно, как ставятся ударения", "ударения и ё: Silero" in job.tail(5000))

# ── 2. только словарь ────────────────────────────────────────────────────────
app.settings.change(stress="dictionary")
job, run = launch()
check("«только словарь» уходит заданию как --stress dictionary", stress_of(run) == "dictionary", str(run["extra"]))

# ── 3. арбитр: тот же ключ и модель, что у озвучки ───────────────────────────
app.settings.change(stress="arbiter")
job, run = launch()
check("арбитр без ключа: всё равно --stress arbiter", stress_of(run) == "arbiter")
check("и в журнале сказано, что без ключа споры решит гибрид", "споры решит гибрид" in job.tail(5000))
app.settings.change(ollama_key="ollama-secret-1234", llm_model="deepseek-v4.1-flash")
job, run = launch()
check("арбитр с ключом: модель и ключ — те же, что для озвучки, окружением",
      run["secrets"].get("LLM_API_KEY") == "ollama-secret-1234"
      and run["secrets"].get("LLM_MODEL") == "deepseek-v4.1-flash"
      and run["secrets"].get("LLM_PROVIDER") == "ollama"
      and not any("ollama-secret" in str(part) for part in run["extra"]))
check("в журнале — поставщик и модель арбитра", "Ollama Cloud, deepseek-v4.1-flash" in job.tail(5000))
check("сводка настроек называет способ и модель",
      "ударения: Silero + словарь, споры решает LLM (ollama, deepseek-v4.1-flash, ключ есть)"
      in app.settings.summary())

# ── 4. страница ─────────────────────────────────────────────────────────────
page = webmod.WebApp(app.jobs, settings=app.settings, chains=app.chains, voice_packs=app.voice_packs)
html_page = page.settings_card()
check("страница: выбор «Ударения и ё» с тремя способами",
      'name="stress"' in html_page and all(title in html_page for title in settings_mod.STRESS.values())
      and '<option value="arbiter" selected>' in html_page)
fields = (b"stress=dictionary&voices=one&llm_provider=ollama&llm_model=deepseek-v4.1-flash&ollama_key=&deepseek_key="
          b"&mode=book&parts_hours=1.5&stall_minutes=30&profiles=1&keep_days=30")
page.route("POST", "/settings", content_type="application/x-www-form-urlencoded", body=fields)
check("форма сохраняет способ, ключ не стирается",
      app.settings.stress == "dictionary" and app.settings.values["ollama_key"] == "ollama-secret-1234")
reloaded = settings_mod.Settings(work / "data" / "settings.json")
check("выбор переживает перезапуск (лежит в томе)", reloaded.stress == "dictionary")
answer = app.settings.change(stress="guess")
check("неизвестный способ не принимается, прежний остаётся",
      "не изменены" in answer and app.settings.stress == "dictionary", answer)
old = settings_mod.Settings(work / "old.json")
(work / "old.json").write_text('{"mode": "book"}', encoding="utf-8")
old.load()
check("старый файл настроек без поля — Silero", old.stress == "silero")

# ── 5. части книги ──────────────────────────────────────────────────────────
app.settings.change(stress="arbiter")
part = app.jobs.add("часть 1", CHAT, book)
chain = chains_mod.pick_for(app.chains, book, part.id, "Книга", CHAT, 1.5)
part.meta.update({"part_index": 1, "chain": chain.id})
part.save()
app.parts_dir = lambda chain: work / "parts"
app.announce_profile = lambda job: None
app.launch(part)
check("часть книги идёт с тем же режимом ударений", stress_of(started[-1]) == "arbiter", str(started[-1]["extra"]))

failed = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)}, не прошло: {len(failed)})")
sys.exit(1 if failed else 0)
