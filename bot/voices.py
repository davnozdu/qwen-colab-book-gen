#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пакеты голосов многоголосых книг: /data/voices/<книга>/.

Пакет делает первая машина Colab (разметка ролей LLM + образцы VoiceDesign) и возвращает zip.
Бот хранит его по хешу книги: повтор той же книги (и каждая её часть) берёт готовый пакет, а
машина на разметку больше не тратится. Рядом лежат распакованные образцы — их слушают на
странице и получают в Telegram — и кэш ответов LLM прерванной разметки (для повтора).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import time
import zipfile


def book_key(book) -> str:
    digest = hashlib.sha256()
    with open(book, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


class VoicePacks:
    def __init__(self, data_dir):
        self.root = pathlib.Path(data_dir) / "voices"

    def folder(self, book) -> pathlib.Path:
        return self.root / book_key(book)

    def pack(self, book):
        """Готовый пакет этой книги или None."""
        folder = self.folder(book)
        found = sorted(folder.glob("*-voices.zip"))
        return found[-1] if found and (folder / "view" / "cast.json").is_file() else None

    def accept(self, zip_path) -> dict:
        """Пакет пришёл с машины: распаковываем для страницы и Telegram, возвращаем состав."""
        zip_path = pathlib.Path(zip_path)
        view = zip_path.parent / "view"
        if view.exists():
            shutil.rmtree(view)
        view.mkdir(parents=True)
        with zipfile.ZipFile(zip_path) as archive:
            for name in archive.namelist():
                if name.startswith("/") or ".." in pathlib.PurePosixPath(name).parts:
                    raise ValueError(f"недопустимый путь в пакете голосов: {name}")
            archive.extractall(view)
        (zip_path.parent / "ready.json").write_text(json.dumps({"zip": zip_path.name, "time": time.time()}),
                                                    encoding="utf-8")
        return self.describe(zip_path.parent)

    def describe(self, folder) -> dict:
        """Состав пакета: название книги, LLM и голоса (имя, реплик, характер, файл образца)."""
        folder = pathlib.Path(folder)
        cast = json.loads((folder / "view" / "cast.json").read_text(encoding="utf-8"))
        voices = []
        for voice in cast.get("voices") or []:
            wav = folder / "view" / "voices" / f"{voice['id']}.wav"
            profile = voice.get("profile") or {}
            voices.append({"id": voice["id"], "name": voice.get("name") or voice["id"],
                           "lines": int(voice.get("lines") or 0), "fixed": bool(voice.get("fixed")),
                           "gender": voice.get("gender"),
                           "about": str(profile.get("personality") or voice.get("evidence") or "")[:160],
                           "sample": str(wav) if wav.is_file() else "",
                           "text": str(voice.get("sample_text") or "")})
        return {"key": folder.name, "title": (cast.get("book") or {}).get("title") or "",
                "llm": cast.get("llm") or {}, "voices": voices}

    def recent(self, limit: int = 3) -> list:
        if not self.root.is_dir():
            return []
        ready = [f for f in self.root.iterdir() if (f / "ready.json").is_file() and (f / "view" / "cast.json").is_file()]
        ready.sort(key=lambda f: (f / "ready.json").stat().st_mtime, reverse=True)
        result = []
        for folder in ready[:limit]:
            try:
                result.append(self.describe(folder))
            except (OSError, ValueError, KeyError):
                continue
        return result

    def sample(self, key: str, voice_id: str):
        """Путь к образцу для страницы — только внутри своей папки."""
        if not key.isalnum() or not all(ch.isalnum() or ch in "_-" for ch in voice_id):
            return None
        path = self.root / key / "view" / "voices" / f"{voice_id}.wav"
        return path if path.is_file() else None
