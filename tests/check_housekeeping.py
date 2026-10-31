#!/usr/bin/env python3
"""Уборка томов — без сети: старое удаляется, живое остаётся, журналы не растут бесконечно."""
import json
import os
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bot"))

import chains as chains_mod      # noqa: E402
import housekeeping              # noqa: E402
import settings as settings_mod  # noqa: E402
from jobs import Jobs            # noqa: E402

results = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + extra) if extra and not ok else ''}")


def age(path: pathlib.Path, days: float) -> None:
    moment = time.time() - days * 86400
    os.utime(path, (moment, moment))


work = pathlib.Path(tempfile.mkdtemp(prefix="sweep-"))
data, config = work / "data", work / "home" / ".config"
jobs = Jobs(data, HERE / "pipeline", max_jobs=1, env=dict(os.environ))
chains = chains_mod.Chains(data)
old = time.time() - 40 * 86400
recent = time.time() - 2 * 86400

books = data / "books"
old_book, live_book = books / "old.epub", books / "live.epub"
for book in (old_book, live_book):
    book.write_bytes(b"PK")
    age(book, 40)

done_old = jobs.add("old.epub", "1", old_book)
done_old.meta.update({"status": "done", "finished": old})
done_old.save()
done_new = jobs.add("new.epub", "1", old_book)
done_new.meta.update({"status": "failed", "finished": recent})
done_new.save()
done_new.log.write_text("строка журнала\n" * 400000)        # ≈ 6 МБ
paused = jobs.add("live.epub", "1", live_book)
paused.meta.update({"status": "paused", "finished": old})
paused.save()

chain_old = chains.create(old_book, "Старая", "1", 1.5)
chain_old.meta.update({"status": "done", "finished": old})
chain_old.save()
(data / "parts" / chain_old.id).mkdir(parents=True)
chain_done = chains.create(old_book, "Собрана три дня назад", "1", 1.5)
chain_done.meta.update({"status": "done", "finished": time.time() - 3 * 86400})
chain_done.save()
raw_done = data / "parts" / chain_done.id
raw_done.mkdir(parents=True)
(raw_done / "part1.flac").write_bytes(b"x" * 1000)

# Собранная вчера книга: сырьё — как корзина на два дня, его ещё можно пересобрать без озвучки.
chain_fresh = chains.create(old_book, "Собрана вчера", "1", 1.5)
chain_fresh.meta.update({"status": "done", "finished": time.time() - 86400})
chain_fresh.save()
raw_fresh = data / "parts" / chain_fresh.id
raw_fresh.mkdir(parents=True)
(raw_fresh / "part1.flac").write_bytes(b"x" * 1000)
chain_live = chains.create(live_book, "В работе", "1", 1.5)
chain_live.meta.update({"status": "running", "created": old})
chain_live.save()
raw_live = data / "parts" / chain_live.id
raw_live.mkdir(parents=True)
orphan = data / "parts" / "20200101-000000-dead"
orphan.mkdir(parents=True)
age(orphan, 40)

results_dir = data / "results"
old_zip, new_zip = results_dir / "old.zip", results_dir / "new.zip"
old_zip.write_bytes(b"z")
new_zip.write_bytes(b"z")
age(old_zip, 40)

profile = config / "colab-profiles" / "1"
(profile / "history").mkdir(parents=True)
(config / "colab-cli").symlink_to(pathlib.Path("colab-profiles") / "1")
(profile / "colab.log").write_text("debug\n" * 2000000)       # ≈ 12 МБ
old_history, new_history = profile / "history" / "b1.jsonl", profile / "history" / "b2.jsonl"
old_history.write_text("{}")
new_history.write_text("{}")
age(old_history, 40)
backup = profile / "token.json.backup-20200101"
backup.write_text("{}")
age(backup, 40)
token = profile / "token.json"
token.write_text("{}")
age(token, 40)

check("срок хранения по умолчанию — 30 дней, задаётся в админке",
      settings_mod.DEFAULTS["keep_days"] == 30 and "30 дн" in settings_mod.Settings(work / "s.json").summary())
done = housekeeping.sweep(data, config, jobs, chains, 30)
print("    убрано:", ", ".join(done))

check("завершённое задание старше срока удалено", jobs.by_id(done_old.id) is None)
check("свежее задание осталось, а его журнал обрезан до хвоста",
      jobs.by_id(done_new.id) is not None and done_new.log.stat().st_size <= housekeeping.JOB_LOG_LIMIT + 200
      and done_new.log.read_text().startswith("[...]"))
check("задание на паузе не тронуто, даже старое", jobs.by_id(paused.id) is not None)
check("старая книга по частям удалена вместе с сырьём",
      chains.by_id(chain_old.id) is None and not (data / "parts" / chain_old.id).exists())
check("сырьё собранной книги хранится 2 дня по умолчанию, срок — настройка",
      settings_mod.DEFAULTS["keep_raw_days"] == 2
      and "сырьё собранной книги 2 дн" in settings_mod.Settings(work / "s.json").summary())
check("у собранной три дня назад книги сырьё убрано, сама книга в списке осталась",
      chains.by_id(chain_done.id) is not None and not raw_done.exists())
check("у собранной вчера книги сырьё на месте: книгу ещё можно пересобрать без озвучки",
      chains.by_id(chain_fresh.id) is not None and raw_fresh.exists())
check("книга по частям в работе и её сырьё не тронуты", chains.by_id(chain_live.id) is not None and raw_live.exists())
check("сырьё без книги (сирота) удалено", not orphan.exists())
check("старый архив удалён, свежий остался", not old_zip.exists() and new_zip.exists())
check("старая книга удалена, книга в работе осталась", not old_book.exists() and live_book.exists())
check("журнал Colab CLI обрезан", (profile / "colab.log").stat().st_size <= housekeeping.CLI_LOG_KEEP + 200)
check("старая история CLI и отложенный вход удалены, свежая история осталась",
      not old_history.exists() and new_history.exists() and not backup.exists())
check("сам вход профиля не трогается, даже старый", token.exists())
check("повторная уборка ничего лишнего не находит", housekeeping.sweep(data, config, jobs, chains, 30) == [])
check("срок 0 дней — сырьё собранной книги убирается сразу",
      housekeeping.sweep(data, config, jobs, chains, 30, time.time(), 0) is not None
      and not raw_fresh.exists())

# ── брошенное: «живой» статус, но месяц без движения; остатки нарезки; битые папки ──
def age_tree(folder, days):
    for item in [folder, *folder.rglob("*")]:
        age(item, days)


abandoned = jobs.add("hung.epub", "1", live_book)
abandoned.meta.update({"status": "waiting", "chain": "x"})
abandoned.save()
age_tree(abandoned.path, 40)
fresh_wait = jobs.add("wait.epub", "1", live_book)
fresh_wait.meta.update({"status": "choice"})
fresh_wait.save()
orphan_chain = chains.create(old_book, "Брошенная", "1", 1.5)
orphan_chain.meta.update({"status": "running", "job": abandoned.id})
orphan_chain.save()
orphan_raw = data / "parts" / orphan_chain.id
(orphan_raw / "part-1").mkdir(parents=True)
(orphan_raw / "part-1" / "a.flac").write_bytes(b"x")
age_tree(orphan_chain.folder, 40)
age_tree(orphan_raw, 40)
broken = data / "jobs" / "20200101-000000-dead"
broken.mkdir(parents=True)
(broken / "meta.json").write_text("{оборвано")
(broken / "log.txt").write_text("x")
age_tree(broken, 40)
housekeeping.sweep(data, config, jobs, chains, 30)
check("брошенное задание (месяц без движения в «живом» статусе) удалено", jobs.by_id(abandoned.id) is None)
check("свежее ждущее ответа задание осталось", jobs.by_id(fresh_wait.id) is not None)
check("нарезка брошенной книги удалена вместе с сырьём частей",
      chains.by_id(orphan_chain.id) is None and not orphan_raw.exists())
check("папка задания с повреждённым состоянием удалена по сроку", not broken.exists())

failed = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)}, не прошло: {len(failed)})")
sys.exit(1 if failed else 0)
