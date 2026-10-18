"""Части книги: нарезка в контейнере, очередь частей и состояние в томе.

Режим частей устроен так: книга режется ЗДЕСЬ, в контейнере (машину Colab на арифметику с epub не
тратим), части собираются по очереди обычными заданиями, а состояние — «что готово, что упало,
сколько раз повторяли» — лежит в томе. Поэтому перезапуск бота и обновление образа ничего не
теряют: продолжать всегда можно с первой неготовой части.

Часть — обычное задание бота: у него свой журнал, свой архив и свои кнопки. Цепочка лишь знает,
в каком порядке их запускать и когда останавливаться.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time

SPLIT = "split"              # книга режется на части
RUNNING = "running"          # части собираются
DONE = "done"                # все части готовы
FAILED = "failed"            # цепочка встала и ждёт человека

PART_QUEUED = "queued"
PART_RUNNING = "running"
PART_DONE = "done"
PART_FAILED = "failed"
PART_HELD = "held"           # не запускаем: одна из частей упала дважды, нужен человек
PART_STOPPED = "stopped"     # остановлено человеком
RETRIES = 1                  # сколько раз повторяем упавшую часть сами, прежде чем звать человека


def split_arguments(python: str, pipeline: pathlib.Path, book: pathlib.Path, out: pathlib.Path,
                    hours: float) -> list[str]:
    """Команда нарезки. Drive не задействуем: части нужны боту в томе, а не в облаке."""
    return [python, str(pathlib.Path(pipeline) / "shrink_book.py"), str(book),
            "--out", str(out), "--max-hours", f"{hours:g}", "--no-drive"]


def shrink_step(python: str, pipeline: pathlib.Path, book: pathlib.Path, out: pathlib.Path,
                hours: float, log: pathlib.Path, env: dict | None = None) -> tuple[int, dict]:
    """Режет книгу на части, складывая ход нарезки в журнал. Возвращает код и план."""
    out.mkdir(parents=True, exist_ok=True)
    arguments = split_arguments(python, pipeline, book, out, hours)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(arguments) + "\n")
        handle.flush()
        done = subprocess.run(arguments, cwd=str(pipeline), env=env, stdout=handle,
                              stderr=subprocess.STDOUT, text=True)
    plan_path = out / "plan.json"
    plan = {}
    if plan_path.is_file():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except ValueError:
            plan = {}
    return done.returncode, plan


def parts_from_plan(plan: dict, book: pathlib.Path) -> list[dict]:
    """Части по плану нарезки. План без частей (parts <= 1) — книга влезает целиком, часть одна."""
    files = [item for item in (plan.get("files") or []) if item.get("epub")]
    if len(files) <= 1 and int(plan.get("parts") or 1) <= 1:
        return [{"index": 1, "total": 1, "title": pathlib.Path(book).stem,
                 "epub": str(book), "hours": float(plan.get("estimated_hours") or 0.0)}]
    return [{"index": number, "total": len(files), "title": item.get("part") or f"часть {number}",
             "epub": item["epub"], "hours": float(item.get("estimated_hours") or 0.0)}
            for number, item in enumerate(files, 1)]


class Chain:
    """Одна книга и её части: план нарезки, части и их состояние (всё на диске)."""

    def __init__(self, folder: pathlib.Path, meta: dict):
        self.folder = pathlib.Path(folder)
        self.meta = meta

    # ────────────────────────────── хранилище ──────────────────────────────
    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def status(self) -> str:
        return self.meta.get("status") or SPLIT

    @property
    def log(self) -> pathlib.Path:
        return self.folder / "log.txt"

    @property
    def parts_hours(self) -> float:
        return float(self.meta.get("parts_hours") or 1.5)

    @property
    def out(self) -> pathlib.Path:
        return pathlib.Path(self.meta.get("out") or (self.folder / "parts"))

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "meta.json").write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def log_line(self, text: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(str(text).rstrip() + "\n")

    def tail(self, limit: int = 4000) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    # ────────────────────────────── части ──────────────────────────────
    @property
    def parts(self) -> list:
        return self.meta.setdefault("parts", [])

    def set_parts(self, parts: list) -> None:
        """Раскладывает части по номеру: у каждой свой epub, название и состояние."""
        self.meta["parts"] = [dict(part, status=PART_QUEUED, job_id="", retries=0,
                                   result="", note="") for part in parts]
        self.meta["total"] = len(self.meta["parts"])
        self.meta["status"] = RUNNING
        self.save()

    def part_by_job(self, job_id: str):
        return next((part for part in self.parts if part.get("job_id") == job_id), None)

    def unfinished(self) -> list:
        return [part for part in self.parts if part.get("status") != PART_DONE]

    def first_unfinished(self):
        pending = self.unfinished()
        return pending[0] if pending else None

    def failed(self) -> list:
        return [part for part in self.parts if part.get("status") == PART_FAILED]

    def held(self) -> list:
        return [part for part in self.parts if part.get("status") == PART_HELD]

    def all_done(self) -> bool:
        return bool(self.parts) and not self.unfinished()

    def summary(self) -> str:
        """Строка для страницы и Telegram: где сейчас цепочка."""
        total = int(self.meta.get("total") or len(self.parts) or 1)
        done = len([part for part in self.parts if part.get("status") == PART_DONE])
        if self.status == SPLIT:
            return "режется на части"
        if self.status == DONE:
            return f"все части готовы ({done} из {total})"
        if self.status == FAILED:
            part = self.first_unfinished()
            where = f"часть {part['index']} из {total}" if part else f"{done} из {total}"
            return f"остановлено на «{where}» — нужно ваше решение"
        current = next((part for part in self.parts if part.get("status") == PART_RUNNING), None)
        if current:
            return f"часть {current['index']} из {total} — идёт"
        following = self.first_unfinished()
        if following:
            return f"часть {following['index']} из {total} — следующая"
        return f"{done} из {total}"

    def title_of(self, part) -> str:
        return part.get("title") or f"{self.meta.get('name')} — часть {part.get('index')}"

    # ────────────────────────────── решения ──────────────────────────────
    def hold(self, reason: str) -> None:
        """Останавливает цепочку: часть упала дважды, дальше идти нельзя (книга с дырой не нужна)."""
        self.meta["status"] = FAILED
        self.meta["note"] = reason
        for part in self.parts:
            if part.get("status") in (PART_QUEUED, PART_RUNNING):
                part["status"] = PART_HELD
                part["note"] = reason
        self.save()

    def release(self) -> list:
        """Что запускать при «продолжить генерацию»: упавшие и придержанные части."""
        again = [part for part in self.parts
                 if part.get("status") in (PART_FAILED, PART_HELD, PART_STOPPED)]
        for part in again:
            part["status"] = PART_QUEUED
            part["note"] = ""
        if self.held() or again:
            self.meta["status"] = RUNNING
            self.meta["note"] = ""
        self.save()
        return again

    def finish(self) -> None:
        self.meta["status"] = DONE
        self.meta["finished"] = time.time()
        self.meta["note"] = ""
        self.save()


def new_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"


def pick_for(store: "Chains", book, job_id: str, name: str = "", chat_id: str = "",
             parts_hours: float = 1.5) -> "Chain":
    """Цепочка для книги: незаконченная нарезка той же книги или новая.

    Метку «job» ставим ВСЕГДА: цепочку двигает chains_pass, а находит он её по этой метке.
    Забыть её — значит получить части, которые собрались, но книгу никто не закрыл.
    """
    unfinished = [item for item in store.of_book(book) if item.status != DONE and item.parts]
    if unfinished:
        chain = unfinished[0]                     # уже резали и не доделали: продолжаем, не режем заново
    else:
        chain = store.create(book, name, chat_id, parts_hours)
    chain.meta["job"] = job_id
    chain.save()
    return chain


class Chains:
    """Все цепочки частей: папка в томе, по одной на книгу."""

    def __init__(self, data_dir):
        self.dir = pathlib.Path(data_dir) / "chains"

    def folder(self, chain_id: str) -> pathlib.Path:
        return self.dir / chain_id

    def create(self, book, name: str, chat_id: str, parts_hours: float, out=None) -> Chain:
        chain_id = new_id()
        folder = self.folder(chain_id)
        folder.mkdir(parents=True, exist_ok=True)
        meta = {"id": chain_id, "book": str(book), "name": name, "chat_id": str(chat_id),
                "parts_hours": float(parts_hours), "created": time.time(), "status": SPLIT,
                "total": 0, "parts": [], "out": str(out or (folder / "parts")),
                "finished": None, "note": ""}
        chain = Chain(folder, meta)
        chain.save()
        return chain

    def load(self) -> list:
        if not self.dir.is_dir():
            return []
        chains = []
        for folder in sorted(self.dir.iterdir(), reverse=True):
            meta_path = folder / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(meta, dict) and meta.get("id"):
                chains.append(Chain(folder, meta))
        return chains

    def by_id(self, chain_id: str):
        folder = self.folder(str(chain_id))
        meta_path = folder / "meta.json"
        if not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        return Chain(folder, meta) if meta.get("id") else None

    def of_book(self, book) -> list:
        """Цепочки этой книги: повторная загрузка не должна резать заново."""
        target = str(pathlib.Path(book).resolve())
        return [chain for chain in self.load()
                if str(pathlib.Path(chain.meta.get("book") or "").resolve()) == target]

    def active(self) -> list:
        return [chain for chain in self.load() if chain.status in (SPLIT, RUNNING, FAILED)]
