#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Уборка томов: старое удаляется само, журналы не растут бесконечно.

Что копится и что с этим делаем (срок — настройка «хранить, дней» в админке):
  /data/jobs/<id>         завершённые задания старше срока — удаляются; журнал завершённого
                          задания длиннее JOB_LOG_LIMIT обрезается до хвоста (там итог и ошибки);
  /data/chains/<id>       книги по частям (готовые, упавшие, заменённые) старше срока — удаляются
                          вместе с сырьём частей /data/parts/<id>;
  /data/parts/<id>        сырьё частей ГОТОВОЙ книги — через RAW_DONE_DAYS: книга уже собрана,
                          а сырьё — гигабайты;
  /data/voices/<книга>    пакеты голосов многоголосых книг — последние VOICE_PACKS_KEEP (5) книг;
  /data/results, books    архивы и книги старше срока — удаляются; книги, которые сейчас в работе, —
                          никогда;
  ~/.config/colab-*       журнал CLI colab.log (пишет отладку HTTP при каждом вызове) обрезается до
                          хвоста; history/*.jsonl, отложенные копии входа (*.backup-*) и корзина
                          удалённых профилей — старше срока удаляются.

Живое не трогаем: задания в работе, в очереди, на паузе, ждущие ответа; цепочки в работе.
"""

from __future__ import annotations

import pathlib
import shutil
import time

JOB_LOG_LIMIT = 2 * 1024 * 1024        # журнал завершённого задания больше 2 МБ — оставляем последний 1 МБ
CLI_LOG_LIMIT = 5 * 1024 * 1024        # colab.log больше 5 МБ — оставляем последний 1 МБ
CLI_LOG_KEEP = 1024 * 1024
RAW_DONE_DAYS = 1                      # сырьё частей собранной книги
DAY = 24 * 3600

LIVE_JOBS = {"running", "queued", "choice", "splitting", "waiting", "gluing", "held", "paused", "casting"}
VOICE_PACKS_KEEP = 5                   # пакеты голосов: последние пять книг (повтор и части берут готовые)
LIVE_CHAINS = {"split", "running", "gluing"}


def _age(path: pathlib.Path, now: float) -> float:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return 0.0


def _remove(path: pathlib.Path) -> bool:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError:
        return False


def keep_tail(path: pathlib.Path, limit: int, keep: int) -> bool:
    """Файл длиннее limit — оставить последние keep байт (с целой строки)."""
    try:
        size = path.stat().st_size
        if size <= limit:
            return False
        with path.open("rb") as handle:
            handle.seek(size - keep)
            tail = handle.read()
        cut = tail.find(b"\n")
        tail = tail[cut + 1:] if 0 <= cut < len(tail) - 1 else tail
        path.write_bytes("[...] начало журнала удалено при уборке\n".encode() + tail)
        return True
    except OSError:
        return False


def sweep(data: pathlib.Path, config: pathlib.Path, jobs, chains, keep_days: float,
          now: float | None = None) -> list:
    """Одна уборка. Возвращает строки «что убрано» (пусто — убирать было нечего)."""
    now = time.time() if now is None else now
    limit = max(1.0, float(keep_days)) * DAY
    data, config = pathlib.Path(data), pathlib.Path(config)
    done = {"заданий": 0, "журналов обрезано": 0, "книг по частям": 0, "сырья частей": 0,
            "архивов": 0, "книг": 0, "файлов CLI": 0, "пакетов голосов": 0}

    # ── задания ──
    in_use = set()
    for job in jobs.load():
        status = job.status
        moment = float(job.meta.get("finished") or job.meta.get("created") or 0)
        if status in LIVE_JOBS:
            in_use.add(pathlib.Path(job.meta.get("book") or "").name)
            continue
        if moment and now - moment > limit:
            if _remove(job.path):
                done["заданий"] += 1
            continue
        if keep_tail(job.log, JOB_LOG_LIMIT, JOB_LOG_LIMIT // 2):
            done["журналов обрезано"] += 1

    # ── книги по частям и их сырьё ──
    live_chains = set()
    for chain in chains.load():
        raw = data / "parts" / str(chain.id)
        if chain.status in LIVE_CHAINS or (chain.status == "failed" and _chain_job_live(jobs, chain)):
            live_chains.add(str(chain.id))
            in_use.add(pathlib.Path(chain.meta.get("book") or "").name)
            continue
        moment = float(chain.meta.get("finished") or chain.meta.get("created") or 0) or _age(chain.folder, now)
        if moment and now - moment > limit:
            removed = _remove(chain.folder)
            if raw.exists():
                _remove(raw)
            if removed:
                done["книг по частям"] += 1
            continue
        if chain.status == "failed":
            in_use.add(pathlib.Path(chain.meta.get("book") or "").name)   # её ещё могут продолжить
        if chain.status == "done" and raw.exists() and now - moment > RAW_DONE_DAYS * DAY:
            if _remove(raw):
                done["сырья частей"] += 1
    parts_root = data / "parts"
    if parts_root.is_dir():
        known = {str(chain.id) for chain in chains.load()}
        for folder in parts_root.iterdir():               # сырьё без цепочки — сирота
            if folder.name not in known and folder.name not in live_chains and _age(folder, now) > limit:
                if _remove(folder):
                    done["сырья частей"] += 1

    # ── пакеты голосов: последние VOICE_PACKS_KEEP книг; книги в работе не трогаем никогда ──
    voices_root = data / "voices"
    if voices_root.is_dir():
        busy = set()
        for job in jobs.load():
            if job.status in LIVE_JOBS:
                if job.meta.get("pack_dir"):
                    busy.add(pathlib.Path(job.meta["pack_dir"]).name)
                if job.meta.get("voice_pack"):
                    busy.add(pathlib.Path(job.meta["voice_pack"]).parent.name)
        for chain in chains.load():
            if chain.status in LIVE_CHAINS | {"failed"} and chain.meta.get("voice_pack"):
                busy.add(pathlib.Path(chain.meta["voice_pack"]).parent.name)
        folders = [f for f in voices_root.iterdir() if f.is_dir()]
        folders.sort(key=lambda f: max([item.stat().st_mtime for item in f.rglob("*")] or [f.stat().st_mtime]),
                     reverse=True)
        for folder in folders[VOICE_PACKS_KEEP:]:
            if folder.name not in busy and _remove(folder):
                done["пакетов голосов"] += 1

    # ── архивы и книги ──
    for folder, key in ((data / "results", "архивов"), (data / "books", "книг")):
        if not folder.is_dir():
            continue
        for item in folder.iterdir():
            if not item.is_file() or item.name in in_use:
                continue
            if _age(item, now) > limit and _remove(item):
                done[key] += 1

    # ── Colab CLI: журналы, история, отложенные копии входа, корзина профилей ──
    folders = []
    profiles = config / "colab-profiles"
    if profiles.is_dir():
        folders.extend(item for item in profiles.iterdir() if item.is_dir() and not item.name.startswith("."))
        trash = profiles / ".deleted"
        if trash.is_dir():
            for item in trash.iterdir():
                if _age(item, now) > limit and _remove(item):
                    done["файлов CLI"] += 1
    cli = config / "colab-cli"
    if cli.is_dir() and not cli.is_symlink():
        folders.append(cli)
    for folder in folders:
        for name in ("colab.log",):
            if keep_tail(folder / name, CLI_LOG_LIMIT, CLI_LOG_KEEP):
                done["журналов обрезано"] += 1
        history = folder / "history"
        if history.is_dir():
            for item in history.iterdir():
                if _age(item, now) > limit and _remove(item):
                    done["файлов CLI"] += 1
        for item in folder.glob("*.backup-*"):
            if _age(item, now) > limit and _remove(item):
                done["файлов CLI"] += 1
    for item in config.glob("colab-cli.backup-*"):
        if _age(item, now) > limit and _remove(item):
            done["файлов CLI"] += 1

    return [f"{key}: {count}" for key, count in done.items() if count]


def _chain_job_live(jobs, chain) -> bool:
    """Остановленная цепочка, книга которой ещё ждёт решения («продолжить генерацию»)."""
    job = jobs.by_id(str(chain.meta.get("job") or "")) if chain.meta.get("job") else None
    return job is not None and job.status in LIVE_JOBS
