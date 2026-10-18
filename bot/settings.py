"""Настройки бота: как обрабатывать книги.

Живут в томе (`/data/settings.json`), поэтому переживают и перезапуск контейнера, и обновление
образа. Меняются со страницы (админка), а не командной строкой: режим выбирает пользователь, а не
тот, кто запускал контейнер.

Режимы:
  book  — «одна книга целиком, без нарезки»: как было, все стадии идут на машине Colab, бот
          забирает готовый архив. Порог нарезки не действует.
  parts — «с разбивкой на части»: бот режет книгу, части синтезируются в облаке по одной, а
          склейка, громкость, музыка и сборка книги идут в контейнере, на постоянном томе.

Пока режим частей только выбирается и печатается в журнал: сам путь частей добавляется отдельными
шагами, поэтому оба режима пока ведут на существующий путь «одна книга целиком».
"""
from __future__ import annotations

import json
import pathlib

MODE_BOOK = "book"
MODE_PARTS = "parts"
MODES = {MODE_BOOK: "одна книга целиком, без нарезки",
         MODE_PARTS: "с разбивкой на части"}
DEFAULTS = {"mode": MODE_BOOK, "parts_hours": 1.5, "drive": True}
HOURS_MIN, HOURS_MAX = 0.5, 12.0
TRUTHY = {"1", "on", "true", "yes", "да", "вкл"}


def parsed(values: dict) -> tuple[dict, list[str]]:
    """Приводит настройки к допустимому виду: возвращает их и список того, что не принято.

    Все-or-ничего: если хоть что-то не так, настройки не меняются. Половина применённых настроек
    хуже отказа — потом непонятно, какой режим на самом деле работает.
    """
    clean = dict(DEFAULTS)
    problems: list[str] = []
    mode = str(values.get("mode", DEFAULTS["mode"])).strip()
    if mode in MODES:
        clean["mode"] = mode
    elif values.get("mode"):
        problems.append(f"неизвестный режим «{mode}»")
    hours = values.get("parts_hours", DEFAULTS["parts_hours"])
    try:
        number = round(float(str(hours).replace(",", ".")), 2)
        if HOURS_MIN <= number <= HOURS_MAX:
            clean["parts_hours"] = number
        else:
            problems.append(f"порог части должен быть от {HOURS_MIN:g} до {HOURS_MAX:g} ч")
    except (TypeError, ValueError):
        problems.append("порог части должен быть числом (например, 1.5)")
    drive = values.get("drive", DEFAULTS["drive"])
    clean["drive"] = drive if isinstance(drive, bool) else str(drive).strip().lower() in TRUTHY
    return clean, problems


class Settings:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.values = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                      # файла нет или он битый — работаем на умолчаниях
        if isinstance(stored, dict):
            self.values, _ = parsed(stored)     # файл мог остаться от старой версии

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.values, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")

    def update(self, raw: dict) -> str:
        """Применяет настройки со страницы. Возвращает текст: он же уходит владельцу в Telegram."""
        clean, problems = parsed(raw)
        if problems:
            return "Настройки не изменены: " + "; ".join(problems) + "."
        changed = clean != self.values
        self.values = clean
        self.save()
        return ("Настройки сохранены: " if changed else "Настройки не изменились: ") + self.summary()

    # ────────────────────────────── значения ──────────────────────────────
    @property
    def mode(self) -> str:
        return self.values["mode"]

    @property
    def parts_hours(self) -> float:
        return float(self.values["parts_hours"])

    @property
    def drive(self) -> bool:
        return bool(self.values["drive"])

    def title(self) -> str:
        return MODES[self.mode]

    def summary(self) -> str:
        line = (f"режим «{self.title()}», части по {self.parts_hours:g} ч, "
                f"Google Drive: {'да' if self.drive else 'нет'}")
        if self.mode == MODE_BOOK:
            line += " (в этом режиме нарезка не используется)"
        return line

    def job_line(self) -> str:
        """Строка в журнал задания: по журналу должно быть видно, каким путём пошла книга."""
        if self.mode == MODE_PARTS:
            return (f"режим: с разбивкой на части — книги длиннее {self.parts_hours:g} ч будут "
                    f"собираться по частям, склейка в конце")
        return f"режим: одна книга целиком, без нарезки (порог {self.parts_hours:g} ч не действует)"
