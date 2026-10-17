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
from jobs import Jobs, safe_stem  # noqa: E402

results = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + extra) if extra and not ok else ''}")


# ── распознавание кода авторизации ──
check("код из браузера распознаётся", botmod.looks_like_code("4/0AY0e-g7abcdefghijklmnopqrstuvwxyz1234567890"))
check("длинный код без префикса распознаётся", botmod.looks_like_code("A" * 45))
check("случайная фраза кодом не считается", not botmod.looks_like_code("привет, как дела?"))
check("короткий текст кодом не считается", not botmod.looks_like_code("123456"))

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
