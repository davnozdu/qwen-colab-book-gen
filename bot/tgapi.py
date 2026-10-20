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
    Живут они под конкретным сообщением (в истории чата).
    """
    rows = [[{"text": label, "callback_data": data} for label, data in row]
            for row in (buttons or []) if row]
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False) if rows else ""


def reply_menu(rows, placeholder: str = "книга EPUB или кнопка меню") -> str:
    """Постоянная клавиатура ПОД полем ввода (ReplyKeyboardMarkup): menu=[["📊 Статус", …], …].

    Инлайн-кнопки видны только под своим сообщением и уезжают вверх вместе с перепиской, поэтому
    «меню» приходилось вызывать командой. ReplyKeyboardMarkup с is_persistent Telegram держит
    внизу, пока сам пользователь его не свернёт: нажатие приходит обычным ТЕКСТОМ кнопки.
    resize_keyboard — компактные кнопки, а не во всю ширину экрана.
    """
    keyboard = [[{"text": label} for label in row] for row in (rows or []) if row]
    if not keyboard:
        return ""
    return json.dumps({"keyboard": keyboard, "resize_keyboard": True, "is_persistent": True,
                       "input_field_placeholder": placeholder}, ensure_ascii=False)


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
    def send(self, chat_id, text: str, preview: bool = False, buttons=None, menu=None) -> bool:
        """menu — постоянная клавиатура под полем ввода; buttons — инлайн под сообщением.

        Две клавиатуры в одном сообщении Telegram не принимает, поэтому menu имеет приоритет:
        его ставят там, где меню нужно показать (или поставить) само.
        """
        payload = {"chat_id": str(chat_id), "text": text[:4000],
                   "disable_web_page_preview": "true" if not preview else "false"}
        markup = reply_menu(menu) if menu else keyboard(buttons)
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

    def set_commands(self, commands) -> bool:
        """Список команд для кнопки «☰» у поля ввода: команды не помнить, а видеть.

        Список — часть настройки бота на стороне Telegram, поэтому его достаточно поставить
        один раз при запуске. Ошибку не считаем смертельной: без списка бот работает.
        """
        try:
            self.call("setMyCommands", {"commands": json.dumps(
                [{"command": name, "description": text} for name, text in commands], ensure_ascii=False)})
            return True
        except TelegramError as exc:
            print(f"[tg] список команд не установился: {exc}", flush=True)
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
    def send_audio(self, chat_id, path: pathlib.Path, caption: str = "", title: str = "") -> bool:
        """Аудиофайл в чат (образец голоса героя): multipart/form-data, как требует Bot API."""
        path = pathlib.Path(path)
        boundary = "----qwenbook" + os.urandom(8).hex()
        fields = {"chat_id": str(chat_id), "caption": caption[:1000], "title": title[:60] or path.stem}
        body = bytearray()
        for name, value in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     f"{value}\r\n").encode("utf-8")
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; "
                 f"filename=\"{path.name}\"\r\nContent-Type: audio/wav\r\n\r\n").encode("utf-8")
        body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(API.format(token=self.token, method="sendAudio"), data=bytes(body),
                                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return bool(json.load(response).get("ok"))
        except Exception as exc:
            print(f"[tg] аудио {path.name} не ушло в {chat_id}: {exc}", flush=True)
            return False

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
