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
import urllib.parse
import time

# Настоящий идентификатор объекта в Drive — длинная строка из букв, цифр, «-» и «_».
# Свежесозданная папка на смонтированном диске отдаёт вместо него пометку вроде «local-105»,
# и ссылка по ней не открывается: такие ссылки в отчёт не берём, вместо них — поиск по имени.
DRIVE_ID = r"[A-Za-z0-9_-]{20,}"
DRIVE_LINK_RE = re.compile(
    rf"https://drive\.google\.com/(?:drive/folders/{DRIVE_ID}|file/d/{DRIVE_ID}"
    rf"|drive/search\?q=[^\s)\]]+)[^\s)\]]*")
DRIVE_LINE_RE = re.compile(r"(?i)^.*drive.*$")
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


# Стадии конвейера в том порядке, в каком их объявляет colab_run_all.py, и их «вес» в общей
# длительности книги (~2 ч звука). Нужны для двух вещей: назвать стадию человеческим словом и
# оценить, сколько осталось. Точное время одной стадии предсказать нельзя, а вот проценты по
# сегментам считаются точно: книга нарезана, и синтез печатает «сегмент k/N».
STAGE_NAMES = ["env", "deps", "repo", "model", "manifest", "prepare", "stress", "voices",
               "synth", "normalize", "music", "package"]
STAGE_TITLES = {"env": "проверка окружения", "deps": "установка зависимостей", "repo": "правки репозитория",
                "model": "веса модели (≈4,5 ГБ)", "manifest": "EPUB → манифест", "prepare": "нарезка на сегменты",
                "stress": "ударения", "voices": "образец чтеца", "synth": "синтез речи",
                "normalize": "громкость и обработка", "music": "музыка", "package": "сборка m4b и epub"}
# Сколько минут занимает стадия (кроме синтеза — он считается по сегментам). Оценки по факту
# прошлых прогонов: конвейер не сообщает заранее, и лучше честное «примерно», чем выдумка.
STAGE_MINUTES = {"env": 3, "deps": 4, "repo": 1, "model": 6, "manifest": 2, "prepare": 3,
                 "stress": 5, "voices": 1, "normalize": 6, "music": 4, "package": 5}


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

    def last_activity(self) -> float:
        """Когда задание последний раз подавало признаки жизни: последняя запись в журнал.

        Синтез пишет строку на каждый сегмент, стадии — свои строки: у живой сборки журнал растёт.
        """
        moments = [float(self.meta.get("started") or self.meta.get("created") or 0)]
        try:
            moments.append(self.log.stat().st_mtime)
        except OSError:
            pass
        return max(moments)

    def tail(self, limit: int = 3500) -> str:
        try:
            text = self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:]

    def log_line(self, text: str) -> None:
        """Дописать строку в журнал задания. Так в журнале остаётся след о том, каким путём
        пошла книга (режим, порог нарезки) — иначе через месяц этого не восстановить."""
        self.path.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(str(text).rstrip() + "\n")

    def progress(self) -> dict:
        """Проценты и оценка остатка по журналу.

        Проценты берём по СЕГМЕНТАМ, а не по времени: книга нарезана заранее, синтез печатает
        «сегмент k/N», и это единственная честно измеримая величина. Время остатка: синтез идёт
        примерно за столько же часов, сколько длится звук книги (конвейер сам это печатает), плюс
        фиксированные минуты на оставшиеся стадии из STAGE_MINUTES. Если человек уже спрашивал
        прогресс раньше, скорость берём по факту между двумя замерами — это точнее оценки.
        """
        try:
            text = self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        found = re.findall(r"\[(\d+)/(\d+)\]\s*(.+)", text)
        if found:
            index, stages, title = int(found[-1][0]), int(found[-1][1]), found[-1][2].strip()
        else:
            index, stages, title = 0, len(STAGE_NAMES), "подготовка"
        name = STAGE_NAMES[index - 1] if 1 <= index <= len(STAGE_NAMES) else ""
        counted = re.findall(r"сегмент\s+(\d+)/(\d+)", text)
        done, segments = (int(counted[-1][0]), int(counted[-1][1])) if counted else (None, None)
        if segments is None:
            listed = re.search(r"сегментов на озвучку:\s*(\d+)", text)
            segments = int(listed[1]) if listed else None
        # Конвейер печатает ожидаемое время звука в двух видах («≈2.4 ч» в сообщении Telegram и
        # «142 мин (2.4 ч)» в журнале), поэтому принимаем оба: иначе оценка остатка пропадёт.
        hours = re.search(r"ожидаемое время звука[^(\n]*?(\d+(?:[.,]\d+)?)\s*ч", text)
        minutes = re.search(r"ожидаемое время звука[^\n]*?(\d+(?:[.,]\d+)?)\s*мин", text)
        if hours:
            expected_hours = float(hours[1].replace(",", "."))
        elif minutes:
            expected_hours = float(minutes[1].replace(",", ".")) / 60
        else:
            expected_hours = None

        # Проценты — доля ОЗВУЧЕННОЙ книги: сегментов готово из общего числа. Это то, что человек
        # имеет в виду под «сколько книги обработано»; до синтеза честный ноль, а вес стадий не
        # подмешиваем, иначе «40 %» звучало бы при пустой ещё книге. Оценка времени — отдельно.
        percent = 0
        if done and segments:
            percent = round(100 * done / segments)
        if self.status == "done":
            percent = 100

        rate = None                                            # сегментов в минуту по двум замерам
        samples = [pair for pair in (self.meta.get("progress_samples") or []) if len(pair) == 2]
        if len(samples) >= 2 and samples[-1][1] > samples[0][1]:
            minutes = (samples[-1][0] - samples[0][0]) / 60
            if minutes >= 2:
                rate = (samples[-1][1] - samples[0][1]) / minutes

        remaining = 0.0
        if self.status in ("running", "queued"):
            if name == "synth" and segments:
                left = segments - (done or 0)
                if rate:                                       # по факту замеров — точнее
                    remaining += left / rate
                elif expected_hours:
                    remaining += 60 * expected_hours * left / segments
            for step in STAGE_NAMES[index:]:                   # стадии после текущей
                remaining += STAGE_MINUTES.get(step, 3)

        # Части книги: у части есть номер и общее число, а у книги целиком — сколько частей уже
        # готово. Без этой метки по строке прогресса не понять, первая это часть или вторая.
        total_parts = int(self.meta.get("parts_total") or 0)
        part = None
        if total_parts:
            index_part = int(self.meta.get("part_index") or 0)
            finished_parts = int(self.meta.get("parts_done") or 0)
            if index_part:
                part = {"index": index_part, "total": total_parts, "done": finished_parts}
                # Проценты книги целиком: готовые части плюс доля текущей.
                whole = (finished_parts + round(percent) / 100) / total_parts * 100
                percent = min(round(whole), 99)
            else:
                part = {"index": 0, "total": total_parts, "done": finished_parts}
                percent = round(100 * finished_parts / total_parts)
            if self.status == "done":
                percent = 100

        def human(minutes: float) -> str:
            return (f"{int(minutes)} мин" if minutes < 60
                    else f"{int(minutes // 60)} ч {int(minutes % 60)} мин")

        return {"stage": name, "stage_title": STAGE_TITLES.get(name, title), "stage_index": index,
                "stages": stages, "segments": segments, "done": done, "percent": round(percent),
                "expected_hours": expected_hours, "measured_rate": round(rate, 1) if rate else None,
                "remaining_minutes": round(remaining), "remaining": human(remaining),
                "eta": time.strftime("%H:%M", time.localtime(time.time() + remaining * 60)) if remaining else "",
                "part": part, "running": self.status == "running"}

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
                "size": len(data), "status": self.status, "elapsed": elapsed,
                "progress": self.progress()}

    def drive_links(self) -> list:
        links = []
        for url in DRIVE_LINK_RE.findall(self.tail(20000)):
            if url not in links:
                links.append(url)
        return links

    def drive_search_link(self) -> str:
        """Ссылка на поиск в Drive по имени книги: работает и без идентификатора от drivefs."""
        stem = pathlib.Path(self.meta.get("name") or self.meta.get("book") or "").stem
        if not stem:
            return ""
        return "https://drive.google.com/drive/search?q=" + urllib.parse.quote(stem)

    def archives(self) -> list:
        found = []
        for raw in ZIP_RE.findall(self.tail(20000)):
            if raw not in found:
                found.append(raw)
        return found

    def last_error(self) -> str:
        found = ERROR_RE.findall(self.tail(4000))
        return found[-1].strip() if found else ""

    def drive_note(self) -> str:
        """Что журнал в последний раз сказал про Google Drive.

        Нужно, когда ссылок нет: по этой строке видно причину («не подключился», «не скопировалось»),
        и в Telegram приходит не догадка, а слова самой обёртки.
        """
        for line in reversed(self.tail(20000).splitlines()):
            cleaned = line.strip()
            if cleaned and DRIVE_LINE_RE.match(cleaned) and "accounts.google.com" not in cleaned:
                return cleaned[:200]
        return ""


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
    def add(self, name: str, chat_id: str, book=None) -> Job:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        job_id = f"{stamp}-{os.urandom(2).hex()}"
        path = self.jobs_dir / job_id
        path.mkdir(parents=True, exist_ok=True)
        # Книгу записываем сразу при создании: пока задание «в очереди», его уже может
        # подхватить очередь бота — и без готового пути к книге оно падало бы «файл пропал».
        meta = {"id": job_id, "session": session_name(job_id), "book": str(book or ""), "name": name,
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
        """Можно ли брать следующее из очереди: есть место и на Colab сейчас не идёт машина
        (очередь — это задания для машины; вторую одновременно не поднимаем)."""
        live = self.live_running()
        return len(live) < self.max_jobs and not any(not job.meta.get("local") for job in live)

    def interrupted(self) -> list:
        """На старте бота: задания, которые оборвал перезапуск контейнера (обновление образа).

        Статус «идёт», а живого окна нет и итога тоже нет: процесс умер вместе с прежним
        контейнером, а не упал сам. Готовое (архив уже лежит) сюда не попадает — его закроет refresh.
        """
        found = []
        for job in self.load():
            if job.status != "running" or self.alive(job):
                continue
            if any(pathlib.Path(path).exists() for path in job.archives()):
                continue
            if job.meta.get("pack_dir"):                 # «голоса»: итог — пакет в своей папке
                started = float(job.meta.get("started") or 0) - 5
                if any(path.stat().st_mtime >= started
                       for path in pathlib.Path(job.meta["pack_dir"]).glob("*-voices.zip")):
                    continue
            found.append(job)
        return found

    def release_machine(self, job: Job, timeout: float = 60) -> None:
        """Освободить машину Colab, брошенную прерванным заданием. Делается ДО повторного запуска:
        у повтора то же имя сессии, и поздний «stop» погасил бы уже новую машину."""
        binary = shutil.which("colab")
        if not binary:
            return
        try:
            subprocess.run([binary, "stop", "-s", job.meta["session"]], capture_output=True, text=True,
                           timeout=timeout, env=self.env, input="")
        except Exception:
            pass

    @locked
    def start(self, job: Job, book: pathlib.Path, gpu: str = "T4", drive_dir: str = "Audiobooks-colab",
              max_hours: float = 7.0, extra: list | None = None, secrets: dict | None = None) -> None:
        # Статус берём с диска, а не из снимка в памяти: одно задание запускают двое —
        # очередь бота (по своему списку) и страница, — а снимок успевает устареть.
        # Второй запуск доходил до tmux и перебивал человеческое объяснение ошибки
        # («tmux: duplicate session»), из-за чего в Telegram уходил отчёт без причины:
        # так случайно падала сквозная проверка «про занятые машины написано понятно».
        fresh = self.by_id(job.id)
        if fresh is not None and fresh.status != "queued":
            return                       # задание уже запущено или уже завершилось
        if job.status == "running" and self.alive(job):
            return                       # это задание уже идёт: повторный запуск ничего не делает
        live = [item for item in self.load() if item.id != job.id
                and item.status == "running" and self.alive(item)]
        if len(live) >= self.max_jobs:
            raise RuntimeError("сейчас идёт другая сборка — дождитесь её конца или остановите её")
        if any(not item.meta.get("local") for item in live):
            # Машина Colab — всегда одна, что бы ни стояло в MAX_JOBS: за две сразу блокируют аккаунты.
            raise RuntimeError("на Colab уже идёт машина — вторую одновременно не поднимаю")
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
        env = dict(self.env)
        prelude = ""
        if secrets:
            # Ключи (LLM) — не в командной строке и не в журнале: файл 0600, который оболочка
            # задания читает и сразу удаляет. Без tmux — просто окружение процесса.
            env.update({k: str(v) for k, v in secrets.items()})
            if self.has_tmux:
                secret_file = job.path / "secrets.env"
                with open(os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                          encoding="utf-8") as handle:
                    handle.write("".join(f"export {k}={_quote(str(v))}\n" for k, v in secrets.items()))
                prelude = f". {_quote(str(secret_file))}; rm -f {_quote(str(secret_file))}; "
        if self.has_tmux:
            shell = prelude + " ".join(_quote(part) for part in command)
            done = subprocess.run(["tmux", "new-session", "-d", "-s", job.meta["session"], "-c",
                                   str(self.pipeline), f"{shell} >> {_quote(str(log))} 2>&1"],
                                  capture_output=True, text=True, env=self.env)
            if done.returncode != 0:
                # Ошибку tmux не ставим поверх уже записанной причины: объяснение для человека
                # важнее служебной строки (иначе отчёт в Telegram уходит без причины).
                job.meta["note"] = job.meta.get("note") or f"tmux: {done.stderr.strip()[:200]}"
        else:
            with log.open("ab") as handle:
                process = subprocess.Popen(command, stdout=handle, stderr=handle,
                                           stdin=subprocess.DEVNULL, start_new_session=True,
                                           env=env, cwd=str(self.pipeline))
            job.meta["pid"] = process.pid
        job.meta.pop("local", None)          # прежняя местная пересборка части: теперь это машина
        job.meta.update({"book": str(book), "status": "running", "started": time.time()})
        job.save()

    @locked
    def start_local(self, job: Job, script: pathlib.Path, arguments: list) -> None:
        """Местный шаг в контейнере (сборка книги из частей): машину не занимает, облако не трогает.

        Это то же, что делает машина на стадиях громкости, музыки и сборки, только на склеенном
        сырье и без GPU. Процесс живёт независимо от бота (start_new_session), поэтому перезапуск
        контейнера его не обрывает на середине, а журнал пишется туда же, где у остальных заданий.
        """
        script = pathlib.Path(script)
        if not script.is_file():
            raise RuntimeError(f"нет скрипта {script}")
        command = [sys_executable(), str(script)] + [str(item) for item in arguments]
        log = job.log
        with log.open("ab") as handle:
            process = subprocess.Popen(command, stdout=handle, stderr=handle,
                                       stdin=subprocess.DEVNULL, start_new_session=True,
                                       env=self.env, cwd=str(script.parent))
        job.meta.update({"status": "running", "started": time.time(), "pid": process.pid,
                         "local": True})
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
        # Пустой номер — это «задания нет», а не «первое попавшееся»: startswith("") истинно для
        # любого задания, и проход по цепочкам однажды выдал части статус родителя.
        if not job_id:
            return None
        for job in self.load():
            if job.id == job_id or job.id.startswith(job_id) or job.meta["session"] == job_id:
                return job
        return None

    def alive(self, job: Job) -> bool:
        # Сборка книги из частей — местный процесс (start_local), не tmux-сессия. В контейнере tmux
        # есть, и проверка по сессии считала такую сборку мёртвой сразу после запуска: задание
        # падало с «архива нет», хотя сборка шла и архив появлялся через минуту.
        if self.has_tmux and not job.meta.get("local"):
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
                continue                        # перезапуска разбирает recover_after_restart() при старте
            if job.status != "running":
                continue
            if self.alive(job):
                continue
            if self.has_tmux:
                # Команда закончилась — окно больше не нужно: ничего не держим в памяти.
                subprocess.run(["tmux", "kill-session", "-t", job.meta["session"]],
                               capture_output=True, env=self.env)
            archives = [path for path in job.archives() if pathlib.Path(path).exists()]
            if job.meta.get("pack_dir"):
                # Задание «голоса» (первая машина многоголосой книги): итог — пакет голосов в своей
                # папке, свежее начала задания. По журналу его не ищем: в имени книги бывают пробелы.
                started = float(job.meta.get("started") or 0) - 5
                packs = [path for path in pathlib.Path(job.meta["pack_dir"]).glob("*-voices.zip")
                         if path.stat().st_mtime >= started]
                archives = [str(max(packs, key=lambda path: path.stat().st_mtime))] if packs else []
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
