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
import threading
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


def locked(method):
    """Блокировка на время работы метода: состояние меняют и бот, и веб-страница."""
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


def diagnose(text: str) -> str:
    """Человеческое объяснение сбоя по журналу: чтобы в Telegram не было загадочных строк."""
    lowered = (text or "").lower()
    if "toomanyassignments" in lowered or "too many" in lowered:
        return ("Все машины Colab заняты: у аккаунта исчерпан лимит бесплатных ускорителей. "
                "Это временно — подождите (обычно помогает пауза) и нажмите «повторить» "
                "(или /repeat).")
    if "service unavailable" in lowered or "не выдана" in lowered or "не поднялась" in lowered:
        return ("Colab не выдаёт машину: свободных T4 сейчас нет или лимит аккаунта. "
                "Пожалуйста, подождите и повторите — книга никуда не денется, "
                "она лежит в папке, кнопка «повторить» (/repeat).")
    if "connection was lost" in lowered:
        return ("Сессия Colab прервалась на середине сборки (машину выключили). "
                "Можно запустить заново: «повторить» на странице или /repeat — "
                "уже сгенерированные куски на машине не сохранились, сборка начнётся с начала.")
    if "cache thrashing" in lowered:
        return ("Синтез упал на кэшах MLX. Обычно помогает повторный запуск — "
                "кэши поднимаются автоматически, но машине не хватило памяти.")
    if "no credentials" in lowered or "не подтверд" in lowered:
        return "Google Drive не подтверждён: зайдите на страницу и нажмите «войти» заново."
    if "архива нет" in lowered or "не забрался" in lowered:
        return ("Сборка закончилась, но архив не забрался с машины. Запустите заново: "
                "«повторить» на странице или /repeat.")
    return ""


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

    def log_payload(self, offset: int = 0) -> dict:
        """Кусок журнала для живой страницы: новые байты с позиции offset плюс состояние."""
        try:
            data = self.log.read_bytes()
        except OSError:
            data = b""
        started = float(self.meta.get("started") or self.meta.get("created") or time.time())
        finished = float(self.meta.get("finished") or time.time())
        seconds = max(0, finished - started)
        elapsed = (f"{int(seconds // 60)} мин" if seconds < 3600
                   else f"{int(seconds // 3600)} ч {int(seconds % 3600 // 60)} мин")
        offset = max(0, min(int(offset), len(data)))
        return {"text": data[offset:].decode("utf-8", "replace"), "offset": len(data),
                "size": len(data), "status": self.status, "elapsed": elapsed}

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
        # Состояние читают и меняют два потока: бот (Telegram) и страница. Одна блокировка
        # на объект — и «залипших» статусов из-за гонки не будет.
        self.lock = threading.RLock()

    # ────────────────────────────── приём книги ──────────────────────────────
    @locked
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
    @locked
    def delete(self, job: Job) -> str:
        """Убрать задание из списка вместе с журналом. Книга и архив остаются на месте."""
        if job.status == "running" and self.alive(job):
            return "Задание ещё идёт — сначала остановите его."
        shutil.rmtree(job.path, ignore_errors=True)
        return f"Задание «{job.meta['name']}» удалено из списка."

    def live_running(self) -> list:
        """Только те, что реально работают сейчас (очередь слот не занимает)."""
        return [job for job in self.load() if job.status == "running" and self.alive(job)]

    def free_slot(self) -> bool:
        return len(self.live_running()) < self.max_jobs

    def fail_stale_queued(self) -> int:
        """На старте бота: задания, которые не успели запуститься, помечаем неудачными."""
        fixed = 0
        for job in self.load():
            if job.status == "queued" and not self.alive(job):
                job.meta.update({"status": "failed", "finished": time.time(),
                                 "note": "Задание не запустилось (бот перезапускали). "
                                         "Нажмите «повторить» или /repeat."})
                job.save()
                fixed += 1
        return fixed

    @locked
    def start(self, job: Job, book: pathlib.Path, gpu: str = "T4", drive_dir: str = "Audiobooks-colab",
              max_hours: float = 7.0, extra: list | None = None) -> None:
        # Одна книга за раз — железно: Telegram и страница проверяют занятость по отдельности,
        # но между проверкой и запуском есть окно. Здесь, под блокировкой, гонка закрыта.
        live = [item for item in self.load() if item.status == "running" and self.alive(item)]
        if len(live) >= self.max_jobs:
            raise RuntimeError("сейчас идёт другая сборка — дождитесь её конца или остановите её")
        command = ([str(self.entry)] if self.entry else [sys_executable(), str(self.runner)]) + \
                  ["--session", job.meta["session"],
                   "--gpu", gpu, "--drive-dir", drive_dir, "--max-hours", str(max_hours),
                   "--out-dir", str(self.results), "--no-caffeinate", "--stop-on-failure", str(book)]
        # Чат владельца: без него обёртка не сможет писать о стадиях и о готовой книге
        # (ключ она берёт из переменной окружения, в командную строку секреты не попадают).
        chat = str(job.meta.get("chat_id") or "").strip()
        if chat:
            command.extend(["--telegram-chat", chat])
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
        try:
            entries = sorted(self.jobs_dir.iterdir(), reverse=True)
        except OSError:                      # каталог могли удалить вместе с томом
            return jobs
        for path in entries:
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
                job.save()                      # код возврата один раз запоминаем в meta
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

    @locked
    def refresh(self) -> dict:
        """Обновляет статусы: завершившиеся задания получают done или failed."""
        changed = {}
        for job in self.load():
            if job.status == "queued":          # ждёт своей очереди — застрявшие после
                continue                        # перезапуска ловит fail_stale_queued() при старте
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
                # Объясняем причину по-человечески: «все машины заняты, подождите» и т.п.
                # Уже записанную причину (например, ошибку tmux) не затираем.
                job.meta["note"] = (job.meta.get("note") or diagnose(job.tail(20000))
                                    or job.last_error() or "архива нет — смотрите лог")
            job.save()
            changed[job.id] = job
        return changed

    @locked
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
