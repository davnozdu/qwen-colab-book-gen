#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram Bot API на стандартной библиотеке: приём обновлений, сообщения, скачивание книг.

Тот же токен бота используется и для уведомлений из Colab (их шлёт telegram_notify.py внутри
конвейера), поэтому бот и уведомления приходят в один чат.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

# Базовый адрес можно переопределить: нужен для отладки и для локального Bot API сервера.
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
API = API_BASE + "/bot{token}/{method}"
FILE = API_BASE + "/file/bot{token}/{path}"
MAX_DOCUMENT = 20 * 1024 * 1024          # столько Bot API отдаёт файл через getFile


class TelegramError(RuntimeError):
    pass


def keyboard(buttons) -> str:
    """Инлайн-клавиатура для sendMessage: [[("подпись", "данные"), …], …] → reply_markup.

    Кнопки важны для телефона: не нужно помнить команды — «Статус», «Задания», «Остановить».
    """
    rows = [[{"text": label, "callback_data": data} for label, data in row]
            for row in (buttons or []) if row]
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False) if rows else ""


class Bot:
    def __init__(self, token: str, timeout: int = 60):
        self.token = token.strip()
        self.timeout = timeout
        if not self.token:
            raise TelegramError("не задан TELEGRAM_BOT_TOKEN")

    # ────────────────────────────── низкий уровень ──────────────────────────────
    def call(self, method: str, payload: dict | None = None, timeout: int | None = None) -> dict:
        data = urllib.parse.urlencode(payload or {}).encode()
        request = urllib.request.Request(API.format(token=self.token, method=method), data=data)
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    answer = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise TelegramError(f"{method}: {detail}") from exc
            except Exception as exc:
                if attempt == 2:                      # сеть, таймаут — не роняем бота
                    raise TelegramError(f"{method}: {exc}") from exc
                time.sleep(1)                         # короткую сетевую осечку переигрываем
        if not answer.get("ok"):
            raise TelegramError(f"{method}: {answer.get('description')}")
        return answer.get("result") or {}

    def whoami(self) -> str:
        return self.call("getMe").get("username", "")

    # ────────────────────────────── сообщения ──────────────────────────────
    def send(self, chat_id, text: str, preview: bool = False, buttons=None) -> bool:
        payload = {"chat_id": str(chat_id), "text": text[:4000],
                   "disable_web_page_preview": "true" if not preview else "false"}
        markup = keyboard(buttons)
        if markup:
            payload["reply_markup"] = markup
        try:
            self.call("sendMessage", payload)
            return True
        except TelegramError as exc:
            # Молча терять отчёт нельзя: без строки в журнале потом не понять, почему в чате
            # пусто (а проверка, которая ждёт сообщение, падает без улик).
            print(f"[tg] сообщение не ушло в {chat_id}: {exc}", flush=True)
            return False

    def answer(self, query_id: str, text: str = "") -> None:
        """Отклик на нажатие кнопки: без него Telegram держит «часики» на кнопке."""
        try:
            self.call("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:200]})
        except TelegramError:
            pass

    def updates(self, offset: int, wait: int = 50):
        try:
            result = self.call("getUpdates", {"offset": offset, "timeout": wait},
                               timeout=wait +
                               30)
        except TelegramError:
            return []
        return result if isinstance(result, list) else []

    # ────────────────────────────── файлы ──────────────────────────────
    def download(self, file_id: str, target: pathlib.Path) -> int:
        info = self.call("getFile", {"file_id": file_id})
        size = int(info.get("file_size") or 0)
        if size > MAX_DOCUMENT:
            raise TelegramError(f"файл больше {MAX_DOCUMENT // 1024 // 1024} МБ — Bot API такой не отдаёт")
        url = FILE.format(token=self.token, path=info["file_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=300) as response, target.open("wb") as handle:
            total = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                handle.write(chunk)
                total += len(chunk)
        return total
