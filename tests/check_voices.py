#!/usr/bin/env python3
"""Многоголосая книга в боте — без сети, без Colab и без Telegram.

  1. нет ключа LLM — книга озвучивается одним голосом, с пояснением;
  2. ключ есть — сначала задание «голоса» (первая машина), книга ждёт; ключ — не в командной строке;
  3. пакет готов — состав героев и образцы в Telegram, книга идёт на озвучку с --voice-pack;
  4. повтор той же книги и её части берут готовый пакет, машина на разметку не тратится;
  5. сбой LLM — один голос сразу; сбой машины Colab — повтор задания «голоса»;
  6. Telegram «🎭 Голоса»: вкл/выкл, поставщик, ключ (сообщение удаляется), модели;
  7. страница: поля LLM, «считать модели», образцы голосов слушаются; уборка хранит пять пакетов.
"""
import json
import os
import pathlib
import sys
import tempfile
import time
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bot"))

import bot as botmod             # noqa: E402
import chains as chains_mod      # noqa: E402
import housekeeping              # noqa: E402
import llm as llm_mod            # noqa: E402
import profiles as profiles_mod  # noqa: E402
import settings as settings_mod  # noqa: E402
import tgapi                     # noqa: E402
import voices as voices_mod      # noqa: E402
import web as webmod             # noqa: E402
from jobs import Jobs            # noqa: E402

results = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + extra) if extra and not ok else ''}")


class FakeBot:
    def __init__(self):
        self.sent, self.audio, self.calls = [], [], []

    def send(self, chat_id, text, preview=False, buttons=None, menu=None):
        self.sent.append((text, tgapi.keyboard(buttons) if buttons else ""))
        return True

    def send_audio(self, chat_id, path, caption="", title=""):
        self.audio.append((str(path), caption))
        return True

    def call(self, method, payload=None, timeout=None):
        self.calls.append((method, payload))
        return {}

    def answer(self, query_id, text=""):
        pass


def said(sent, *words) -> bool:
    return any(all(word in text for word in words) for text, _ in sent)


CHAT = "555"
work = pathlib.Path(tempfile.mkdtemp(prefix="voices-"))
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
app.in_background = lambda chat_id, task, title: (lambda answer: answer and app.bot.send(chat_id, answer))(task())
started = []


def fake_start(job, book, gpu="T4", drive_dir="", max_hours=7.0, extra=None, secrets=None):
    started.append({"job": job.id, "extra": list(extra or []), "secrets": dict(secrets or {})})
    job.meta.update({"status": "running", "started": time.time(), "book": str(book)})
    job.save()


app.jobs.start = fake_start
app.jobs.live_running = lambda: []
book = work / "Книга с пробелами.epub"
book.write_bytes(b"PK the book itself")


def make_pack(folder: pathlib.Path) -> pathlib.Path:
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / "Книга с пробелами-voices.zip"
    cast = {"format": "qwen-voice-cast", "book": {"title": "Книга"}, "llm": {"provider": "ollama", "model": "m"},
            "voices": [{"id": "narrator", "name": "Рассказчик", "fixed": True, "lines": 30},
                       {"id": "petya", "name": "Петя", "lines": 12, "sample_text": "Не хочу.",
                        "profile": {"personality": "упрямый мальчик"}}]}
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("cast.json", json.dumps(cast, ensure_ascii=False))
        z.writestr("voices/petya.wav", b"RIFF fake wav")
        z.writestr("voices/petya.json", "{}")
    return zip_path


# ── 1. нет ключа — один голос ───────────────────────────────────────────────
app.settings.change(voices="multi")
plain = app.jobs.add("Книга с пробелами.epub", CHAT, book)
app.launch(plain)
check("многоголосая без ключа LLM: озвучка одним голосом, с пояснением",
      started[-1]["job"] == plain.id and "--voice-pack" not in started[-1]["extra"]
      and said(app.bot.sent, "нет ключа", "одним голосом"), str(app.bot.sent[-1:]))

# ── 2. ключ есть — сначала «голоса» ─────────────────────────────────────────
app.settings.change(ollama_key="ollama-secret-1234")
job = app.jobs.add("Книга с пробелами.epub", CHAT, book)
app.bot.sent = []
app.launch(job)
job = app.jobs.by_id(job.id)
cast = app.jobs.by_id(job.meta.get("cast_job") or "")
check("с ключом книга ждёт голоса, а не озвучивается", job.status == "casting" and cast is not None
      and cast.status == "queued" and started[-1]["job"] != job.id)
check("и в Telegram сказано, что сначала разметка ролей и голоса", said(app.bot.sent, "многоголосая", "голоса героев"))
app.launch(cast)
first = started[-1]
check("задание «голоса» — первая машина с --cast в папку пакета этой книги",
      first["job"] == cast.id and "--cast" in first["extra"]
      and str(app.voice_packs.folder(book)) in first["extra"])
check("ключ LLM уходит заданию окружением, а не командной строкой",
      first["secrets"].get("LLM_API_KEY") == "ollama-secret-1234"
      and first["secrets"].get("LLM_MODEL") == llm_mod.DEFAULT_MODELS["ollama"]
      and not any("ollama-secret" in str(part) for part in first["extra"]))

# ── 3. пакет готов — образцы и озвучка с пакетом ─────────────────────────────
pack_zip = make_pack(app.voice_packs.folder(book))
cast = app.jobs.by_id(cast.id)
cast.meta.update({"status": "done", "result": str(pack_zip), "finished": time.time()})
cast.save()
app.bot.sent = []
app.casting_pass()
job = app.jobs.by_id(job.id)
check("пакет пришёл — книга снова в очереди с пакетом", job.status == "queued" and job.meta.get("voice_pack") == str(pack_zip))
check("в Telegram — состав героев и образцы аудио",
      said(app.bot.sent, "голоса готовы", "Петя", "реплик 12") and app.bot.audio
      and app.bot.audio[-1][0].endswith("petya.wav"), str(app.bot.sent))
app.launch(job)
check("озвучка идёт с --voice-pack", started[-1]["job"] == job.id and started[-1]["extra"][-2:] == ["--voice-pack", str(pack_zip)])

# ── 4. повтор и части — готовый пакет ───────────────────────────────────────
again = app.jobs.add("Книга с пробелами.epub", CHAT, book)
count = len(app.jobs.load())
app.launch(again)
check("повтор той же книги берёт готовый пакет: «голоса» не запускаются",
      started[-1]["job"] == again.id and "--voice-pack" in started[-1]["extra"] and len(app.jobs.load()) == count)
parent = app.jobs.add("Книга с пробелами.epub", CHAT, book)
parent.meta.update({"voice_pack": str(pack_zip)})
parent.save()
chain = app.chains.create(book, "Части", CHAT, 0.5)
botmod.chains.pick_for = lambda store, b, job_id, name="", chat_id="", parts_hours=1.5: chain
app.split_chain = lambda chain_obj, job_obj: None
app.start_chain(parent, book)
chain = app.chains.by_id(chain.id)
part = app.jobs.add("Части — часть 1", CHAT, book)
part.meta.update({"chain": chain.id, "part_index": 1, "parts_total": 2})
part.save()
app.launch(part)
check("части книги озвучиваются тем же пакетом голосов",
      chain.meta.get("voice_pack") == str(pack_zip) and started[-1]["job"] == part.id
      and started[-1]["extra"][-2:] == ["--voice-pack", str(pack_zip)])

# ── 5. сбои «голосов» ───────────────────────────────────────────────────────
other_book = work / "other.epub"
other_book.write_bytes(b"PK another book")
broken = app.jobs.add("other.epub", CHAT, other_book)
app.launch(broken)
failed_cast = app.jobs.by_id(app.jobs.by_id(broken.id).meta["cast_job"])
app.launch(failed_cast)
failed_cast.meta.update({"status": "failed", "note": "архива нет", "finished": time.time()})
failed_cast.save()
failed_cast.log_line("RuntimeError: Ollama отклонил ключ (HTTP 401). Проверьте ключ в настройках")
app.bot.sent = []
app.casting_pass()
check("ключ отклонён — книга сразу идёт одним голосом, причина в Telegram",
      app.jobs.by_id(broken.id).status == "queued" and app.jobs.by_id(broken.id).meta.get("single_voice")
      and said(app.bot.sent, "отклонил ключ", "одним голосом"), str(app.bot.sent))
app.launch(app.jobs.by_id(broken.id))
check("и запускается без пакета", started[-1]["job"] == broken.id and "--voice-pack" not in started[-1]["extra"])

third_book = work / "third.epub"
third_book.write_bytes(b"PK third book")
waiting = app.jobs.add("third.epub", CHAT, third_book)
app.launch(waiting)
machine_cast = app.jobs.by_id(app.jobs.by_id(waiting.id).meta["cast_job"])
app.launch(machine_cast)
machine_cast = app.jobs.by_id(machine_cast.id)
machine_cast.meta.update({"status": "failed", "note": "Все машины Colab заняты", "finished": time.time()})
machine_cast.save()
machine_cast.log_line("TooManyAssignmentsError: too many assignments")
app.casting_pass()
check("машина Colab не выдана — «голоса» повторяются, книга ждёт",
      app.jobs.by_id(machine_cast.id).status == "queued" and app.jobs.by_id(waiting.id).status == "casting")

# ── 6. Telegram «🎭 Голоса» ─────────────────────────────────────────────────
app.bot.sent = []
app.do_action(CHAT, "voices")
markup = app.bot.sent[-1][1]
check("меню голосов: вкл/выкл, поставщик, ключ, модели",
      all(item in markup for item in ("voices_toggle", "llm_provider:deepseek", "llm_key", "llm_models"))
      and said(app.bot.sent, "••••1234") and "ollama-secret" not in app.bot.sent[-1][0])
app.do_action(CHAT, "llm_provider:deepseek")
check("поставщик переключён, модель — его по умолчанию",
      app.settings.llm_provider == "deepseek" and app.settings.llm_model == "deepseek-flash")
app.do_action(CHAT, "llm_key")
app.handle({"message": {"message_id": 77, "chat": {"id": int(CHAT)}, "text": "sk-deepseek-9876"}})
check("ключ принят следующим сообщением, сообщение с ключом удалено",
      app.settings.values["deepseek_key"] == "sk-deepseek-9876"
      and ("deleteMessage", {"chat_id": CHAT, "message_id": "77"}) in app.bot.calls
      and said(app.bot.sent, "••••9876"))
real_list = llm_mod.list_models
llm_mod.list_models = lambda provider, key, timeout=30, opener=None: ["deepseek-flash", "deepseek-v4-pro"]
app.bot.sent = []
app.do_action(CHAT, "llm_models")
check("«считать модели» показывает модели кнопками", "llm_model:deepseek-v4-pro" in app.bot.sent[-1][1])
app.do_action(CHAT, "llm_model:deepseek-v4-pro")
check("модель выбрана кнопкой", app.settings.llm_model == "deepseek-v4-pro")
app.do_action(CHAT, "voices_toggle")
check("многоголосие выключается кнопкой", app.settings.voices == "one")
app.settings.change(voices="multi")

# ── 7. страница ─────────────────────────────────────────────────────────────
page = webmod.WebApp(app.jobs, settings=app.settings, chains=app.chains, voice_packs=app.voice_packs,
                     refresh_models=app.refresh_models)
html_page = page.jobs_page(1)
check("страница: режим голосов, поставщик, модель, ключи и «считать модели»",
      all(item in html_page for item in ('name="voices"', 'name="llm_provider"', 'name="llm_model"',
                                          'name="ollama_key"', 'name="deepseek_key"', "/llm/models"))
      and "sk-deepseek" not in html_page and "••••9876" in html_page)
check("страница: голоса книги со слушаемым образцом",
      "Голоса: Книга" in html_page and f"/voice/{app.voice_packs.folder(book).name}/petya.wav" in html_page)
code, kind, body, _ = page.route("GET", f"/voice/{app.voice_packs.folder(book).name}/petya.wav")
check("образец отдаётся как audio/wav", code == 200 and kind == "audio/wav")
code, _, _, _ = page.route("GET", f"/voice/{app.voice_packs.folder(book).name}/..%2Fsecret.wav")
check("чужие пути не отдаются", code == 404)
page.route("POST", "/llm/models")
check("«считать модели» со страницы сохраняет список", app.settings.models("deepseek") == ["deepseek-flash", "deepseek-v4-pro"])
llm_mod.list_models = real_list
fields = b"voices=multi&llm_provider=deepseek&llm_model=deepseek-flash&deepseek_key=&ollama_key=&mode=book&parts_hours=1.5&stall_minutes=30&profiles=1&keep_days=30"
page.route("POST", "/settings", content_type="application/x-www-form-urlencoded", body=fields)
check("сохранение настроек с пустым полем ключа ключ не стирает",
      app.settings.values["deepseek_key"] == "sk-deepseek-9876" and app.settings.llm_model == "deepseek-flash")

# ── секреты задания и уборка ────────────────────────────────────────────────
real = Jobs(work / "real", HERE / "pipeline", max_jobs=1, env=dict(os.environ))
real.has_tmux = True
commands = []
botmod_subprocess = __import__("jobs").subprocess
original_run = botmod_subprocess.run
botmod_subprocess.run = lambda command, **kw: commands.append(command) or type("R", (), {"returncode": 0, "stderr": ""})()
secret_job = real.add("x.epub", CHAT, book)
real.start(secret_job, book, secrets={"LLM_API_KEY": "very-secret"})
botmod_subprocess.run = original_run
shell = commands[-1][-1] if commands else ""
secret_file = secret_job.path / "secrets.env"
check("в tmux ключ идёт файлом 0600, который задание читает и удаляет",
      "very-secret" not in shell and "secrets.env" in shell and "rm -f" in shell
      and secret_file.is_file() and oct(secret_file.stat().st_mode & 0o777) == "0o600"
      and "very-secret" in secret_file.read_text())

for number in range(7):
    folder = work / "data" / "voices" / f"{number:016x}"
    (folder / "view").mkdir(parents=True, exist_ok=True)
    (folder / "x-voices.zip").write_bytes(b"z")
    moment = time.time() - (10 - number) * 3600
    for item in [folder / "x-voices.zip", folder / "view", folder]:
        os.utime(item, (moment, moment))
housekeeping.sweep(work / "data", work / "nohome", app.jobs, app.chains, 30)
left = sorted(p.name for p in (work / "data" / "voices").iterdir())
check("уборка хранит пакеты голосов последних пяти книг, книги в работе — всегда",
      len(left) >= 5 and app.voice_packs.folder(book).name in left
      and f"{0:016x}" not in left and f"{6:016x}" in left, str(left))

failed = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)}, не прошло: {len(failed)})")
sys.exit(1 if failed else 0)
