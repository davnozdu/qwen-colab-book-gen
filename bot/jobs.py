#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Задания: принять книгу, запустить сборку в фоне, показать состояние и результат.

Фон обеспечивает tmux, если он есть (в образе он есть), иначе обычный отсоединённый процесс —
поэтому модуль одинаково работает и в контейнере, и на маке при отладке.

Состояние лежит в /data/jobs/<id>/meta.json, лог — рядом в log.txt. Всё, что печатает обёртка
run-book.py (ссылка авторизации, ход стадий, ссылки на Drive), попадает в лог и уходит в Telegram
самой обёрткой — бот лишь читает лог для команд /status и /log.

Зависимостей нет: только стандартная библиотека.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import time

DRIVE_LINK_RE = re.compile(r"https://drive\.google\.com/(?:drive/folders|file/d)/[^\s)\]]+")
ZIP_RE = re.compile(r"(/[^\s]*\.zip)")
ERROR_RE = re.compile(r"(?i)(Traceback|ошибка|не удалось|failed|error|не выдана|не поднялась)[^\n]{0,120}")


def safe_stem(name: str) -> str:
    """Имя книги из имени файла: без путей и опасных символов."""
    stem = pathlib.Path(name).stem
    cleaned = re.sub(r"[^\w\-. ()А-Яа-яЁё]+", "_", stem, flags=re.UNICODE).strip(" ._")
    return (cleaned or "book")[:80]


def session_name(job_id: str) -> str:
    """Имя сессии Colab CLI и tmux: коротко и только из допустимых символов."""
    return "b" + re.sub(r"[^0-9a-zA-Z]", "", job_id)[-10:]


class Job:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def status(self) -> str:
        return self.meta["status"]

    @property
    def log(self) -> pathlib.Path:
        return self.path / "log.txt"

    def save(self) -> None:
        (self.path / "meta.json").write_text(json.dumps(self.meta, ensure_ascii=False, indent=2),
                                             encoding="utf-8")

    def tail(self, limit: int = 3500) -> str:
        try:
            text = self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:]

    def drive_links(self) -> list:
        links = []
        for url in DRIVE_LINK_RE.findall(self.tail(20000)):
            if url not in links:
                links.append(url)
        return links

    def archives(self) -> list:
        found = []
        for raw in ZIP_RE.findall(self.tail(20000)):
            if raw not in found:
                found.append(raw)
        return found

    def last_error(self) -> str:
        found = ERROR_RE.findall(self.tail(4000))
        return found[-1].strip() if found else ""


class Jobs:
    """Пул заданий: одно за другим (у бесплатного тарифа нет двух GPU одновременно)."""

    def __init__(self, data_dir: pathlib.Path, pipeline_dir: pathlib.Path, runner_name: str = "run-book.py",
                 max_jobs: int = 1, env: dict | None = None):
        self.data = pathlib.Path(data_dir)
        self.pipeline = pathlib.Path(pipeline_dir)
        # В контейнере основной запуск — Linux-скрипт run-book (ставится в образ как /usr/local/bin).
        entry = shutil.which("run-book") or os.environ.get("RUN_BOOK")
        self.entry = pathlib.Path(entry) if entry else None
        self.runner = self.pipeline / "colab" / "cli" / runner_name
        self.max_jobs = max(1, int(max_jobs))
        self.books = self.data / "books"
        self.results = self.data / "results"
        self.jobs_dir = self.data / "jobs"
        for path in (self.books, self.results, self.jobs_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ if env is None else env)
        self.has_tmux = shutil.which("tmux") is not None

    # ────────────────────────────── приём книги ──────────────────────────────
    def add(self, name: str, chat_id: str) -> Job:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        job_id = f"{stamp}-{os.urandom(2).hex()}"
        path = self.jobs_dir / job_id
        path.mkdir(parents=True, exist_ok=True)
        meta = {"id": job_id, "session": session_name(job_id), "book": "", "name": name,
                "chat_id": str(chat_id), "created": time.time(), "status": "queued", "started": None,
                "finished": None, "pid": None, "note": ""}
        (path / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (path / "log.txt").write_text("", encoding="utf-8")
        return Job(path)

    # ────────────────────────────── запуск ──────────────────────────────
    def live_running(self) -> list:
        """Только те, что реально работают сейчас (очередь слот не занимает)."""
        return [job for job in self.load() if job.status == "running" and self.alive(job)]

    def free_slot(self) -> bool:
        return len(self.live_running()) < self.max_jobs

    def start(self, job: Job, book: pathlib.Path, gpu: str = "T4", drive_dir: str = "Audiobooks-colab",
              max_hours: float = 7.0, extra: list | None = None) -> None:
        command = ([str(self.entry)] if self.entry else [sys_executable(), str(self.runner)]) + \
                  ["--session", job.meta["session"],
                   "--gpu", gpu, "--drive-dir", drive_dir, "--max-hours", str(max_hours),
                   "--out-dir", str(self.results), "--no-caffeinate", "--stop-on-failure", str(book)]
        command.extend(extra or [])
        log = job.log
        if self.has_tmux:
            shell = " ".join(_quote(part) for part in command)
            done = subprocess.run(["tmux", "new-session", "-d", "-s", job.meta["session"], "-c",
                                   str(self.pipeline), f"{shell} >> {_quote(str(log))} 2>&1"],
                                  capture_output=True, text=True, env=self.env)
            if done.returncode != 0:
                job.meta["note"] = f"tmux: {done.stderr.strip()[:200]}"
        else:
            with log.open("ab") as handle:
                process = subprocess.Popen(command, stdout=handle, stderr=handle,
                                           stdin=subprocess.DEVNULL, start_new_session=True,
                                           env=self.env, cwd=str(self.pipeline))
            job.meta["pid"] = process.pid
        job.meta.update({"book": str(book), "status": "running", "started": time.time()})
        job.save()

    # ────────────────────────────── состояние ──────────────────────────────
    def load(self) -> list:
        jobs = []
        for path in sorted(self.jobs_dir.iterdir(), reverse=True):
            if (path / "meta.json").exists():
                try:
                    jobs.append(Job(path))
                except (OSError, ValueError):
                    continue
        return jobs

    def by_id(self, job_id: str):
        for job in self.load():
            if job.id == job_id or job.id.startswith(job_id) or job.meta["session"] == job_id:
                return job
        return None

    def alive(self, job: Job) -> bool:
        if self.has_tmux:
            done = subprocess.run(["tmux", "has-session", "-t", job.meta["session"]],
                                  capture_output=True, env=self.env)
            return done.returncode == 0
        pid = job.meta.get("pid")
        if not pid:
            return False
        pid = int(pid)
        try:
            # Завершившийся ребёнок остаётся зомби, и os.kill(pid, 0) по нему «успешен» —
            # поэтому сначала пробуем его дождаться: так мы и узнаём код возврата.
            done, status = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                job.meta["exit_code"] = os.waitstatus_to_exitcode(status)
                return False
            return True
        except ChildProcessError:
            pass                                   # процесс не наш (бот перезапускали)
        except (OSError, ValueError):
            return False
        if _is_zombie(pid):
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def running(self) -> list:
        return [job for job in self.load() if job.status == "queued" or
                (job.status == "running" and self.alive(job))]

    def refresh(self) -> dict:
        """Обновляет статусы: завершившиеся задания получают done или failed."""
        changed = {}
        for job in self.load():
            if job.status == "queued":          # ещё не запускалось: пусть решает очередь
                continue
            if job.status != "running":
                continue
            if self.alive(job):
                continue
            if self.has_tmux:
                # Команда закончилась — окно больше не нужно: ничего не держим в памяти.
                subprocess.run(["tmux", "kill-session", "-t", job.meta["session"]],
                               capture_output=True, env=self.env)
            archives = [path for path in job.archives() if pathlib.Path(path).exists()]
            job.meta["finished"] = time.time()
            if archives:
                job.meta.update({"status": "done", "result": archives[-1]})
            else:
                job.meta["status"] = "failed"
                job.meta["note"] = job.last_error() or "архива нет — смотрите лог"
            job.save()
            changed[job.id] = job
        return changed

    def stop(self, job: Job) -> None:
        if self.has_tmux:
            subprocess.run(["tmux", "kill-session", "-t", job.meta["session"]], capture_output=True,
                           env=self.env)
        pid = job.meta.get("pid")
        if pid:
            try:
                os.killpg(int(pid), 15)
            except (OSError, ValueError):
                pass
        # Обёртку прервали, значит сама она машину не освободит — делаем это за неё,
        # иначе ускоритель останется занятым и следующий запуск не получит GPU.
        binary = shutil.which("colab")
        if binary:
            try:
                subprocess.run([binary, "stop", "-s", job.meta["session"]], capture_output=True,
                               text=True, timeout=180, env=self.env, input="")
            except Exception:
                pass
        job.save()


def _is_zombie(pid: int) -> bool:
    """В Linux состояние «зомби» видно в /proc: процесс уже кончился, но не убран родителем."""
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    tail = stat.rsplit(")", 1)[-1].strip()
    return tail.split(" ")[:1] == ["Z"]


def sys_executable() -> str:
    import sys
    return sys.executable or "python3"


def _quote(text: str) -> str:
    import shlex
    return shlex.quote(str(text))
