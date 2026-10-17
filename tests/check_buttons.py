#!/usr/bin/env python3
"""Проверяет кнопки: нажатие даёт ответ, а меню приходит вместе с клавиатурой.

Смотреть на сырой JSON тут не нужно — проверяем то, что увидит человек в чате:
меню после нажатия, отчёт по статусу и подтверждение остановки.
"""
import json
import os
import pathlib
import sys

state = pathlib.Path(os.environ["TG_STATE"])
lines = (state / "sent.jsonl").read_text().splitlines()
# Смотрим только на то, что пришло ПОСЛЕ нажатий: иначе проверка «пришёл список»
# ловит старые сообщения с тем же словом и проходит впустую.
start = int(os.environ.get("BUTTONS_FROM", "0") or 0)
sent = [json.loads(line) for line in lines[start:]]
texts = [item["text"] for item in sent]
menus = [item.get("buttons") or "" for item in sent]
menu_with_buttons = [markup for markup in menus if "inline_keyboard" in markup]

checks = [
    ("на нажатие «Статус» пришёл отчёт",
     any(("Свободно:" in text or "Сейчас:" in text) for text in texts)),
    ("на нажатие «Задания» пришёл список",
     any(("/log " in text or "· done" in text or "Заданий" in text) for text in texts)),
    ("остановка просит подтверждение",
     any("Остановить всё" in text for text in texts)),
    ("сброс входа есть в Telegram и просит подтверждение",
     any("Сбросить вход в Google Colab" in text for text in texts)
     and any("reset_do" in markup for markup in menus)),
    ("кнопки приходят как клавиатура, а не текстом",
     any("Остановить всё" in markup for markup in menu_with_buttons)
     and not any("inline_keyboard" in text for text in texts)),
    ("в клавиатуре есть статус, задания, вход и остаток",
     any(all(word in markup for word in ("Статус", "Задания", "Войти в Colab", "Остаток"))
         for markup in menu_with_buttons)),
]
for name, ok in checks:
    print(f"  {'✓' if ok else '✗'} {name}")
sys.exit(0 if all(ok for _, ok in checks) else 1)
