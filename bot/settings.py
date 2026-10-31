"""Настройки бота: как обрабатывать книги.

Живут в томе (`/data/settings.json`), поэтому переживают и перезапуск контейнера, и обновление
образа. Меняются со страницы (админка), а не командной строкой: режим выбирает пользователь, а не
тот, кто запускал контейнер.

Режимы:
  book  — «одна книга целиком, без нарезки»: как было, все стадии идут на машине Colab, бот
          забирает готовый архив. Порог нарезки не действует.
  parts — «с разбивкой на части»: бот режет книгу, части синтезируются в облаке по одной, а
          склейка, громкость, музыка и сборка книги идут в контейнере, на постоянном томе.

По умолчанию — book. Флажок Google Drive действует только в режиме book: в режиме частей всё
возвращается в том бота, Drive не участвует. Подробно — docs/parts-and-glue.md.
"""
from __future__ import annotations

import json
import pathlib

MODE_BOOK = "book"
MODE_PARTS = "parts"
MODES = {MODE_BOOK: "одна книга целиком, без нарезки",
         MODE_PARTS: "с разбивкой на части"}
VOICES = {"one": "один голос (рассказчик)", "multi": "многоголосая: роли через LLM"}
# Звук готовой книги. Синтез даёт МОНО 24 кГц: настоящего стерео взять неоткуда, поэтому «объём»
# — это расширение моно эффектом Хааса (два канала, правая сторона на 12 мс позже). Длительность
# и громкость при этом не меняются, иначе поехали бы подсветка и уровень книги.
# По умолчанию включён «объём»: так решил владелец 31.10.2026 — слушать приятнее, а вернуть моно
# можно одной настройкой и пересборкой книги без новой озвучки.
STEREO = {"space": "стерео-объём (два канала, мягкий Хаас)",
          "mono": "моно (как есть)"}
# Ударения и ё (стадия stress на машине Colab). Словари — davnozdu/supertonic-dictionaries.
STRESS = {"silero": "Silero (как раньше)",
          "dictionary": "только словарь Supertonic",
          "arbiter": "Silero + словарь, споры решает LLM"}
LLM_PROVIDERS = ("ollama", "deepseek")
SECRETS = ("ollama_key", "deepseek_key")      # ключи LLM: в форме пусто — значит «не менять»
DEFAULTS = {"mode": MODE_BOOK, "parts_minutes": 60, "drive": True, "stall_minutes": 30, "profiles": 1,
            "keep_days": 30, "voices": "one", "llm_provider": "ollama", "llm_model": "",
            "ollama_key": "", "deepseek_key": "", "llm_models": {}, "stress": "silero",
            "parts_per_profile": 1, "retry_hours": 4.0,
            "keep_raw_days": 2, "stereo": "space"}
STALL_MIN, STALL_MAX = 5, 600       # сторож зависаний: 0 — не следить
MINUTES_MIN, MINUTES_MAX = 10, 720     # длина части в минутах звука
KEEP_MIN, KEEP_MAX = 1, 365          # уборка: сколько дней хранить завершённое
RAW_MIN, RAW_MAX = 0, 60             # сырьё собранной книги — «корзина»: 0 — удалять сразу
PROFILES_MAX = 10                   # аккаунтов Google Colab, между которыми бот переключается
PER_PROFILE_MAX = 50                # частей подряд на одном аккаунте; 0 — переключаться только при сбое
RETRY_MAX = 48.0                    # все аккаунты упали — через столько часов круг заново; 0 — ждать человека
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
    # Длина части — в минутах. Прежние настройки (и прежние формы) хранили часы: parts_hours,
    # если он есть, переводим в минуты — 1.5 ч станут 90 мин, а не сбросятся на умолчание.
    try:
        if values.get("parts_hours") not in (None, ""):
            minutes = round(float(str(values["parts_hours"]).replace(",", ".")) * 60)
        else:
            minutes = round(float(str(values.get("parts_minutes", DEFAULTS["parts_minutes"])).replace(",", ".")))
        if MINUTES_MIN <= minutes <= MINUTES_MAX:
            clean["parts_minutes"] = minutes
        else:
            problems.append(f"длина части — от {MINUTES_MIN} до {MINUTES_MAX} минут")
    except (TypeError, ValueError):
        problems.append("длина части — число минут (например, 60)")
    stall = values.get("stall_minutes", DEFAULTS["stall_minutes"])
    try:
        minutes = int(float(str(stall).replace(",", ".")))
        if minutes == 0 or STALL_MIN <= minutes <= STALL_MAX:
            clean["stall_minutes"] = minutes
        else:
            problems.append(f"порог зависания — 0 (не следить) или от {STALL_MIN} до {STALL_MAX} мин")
    except (TypeError, ValueError):
        problems.append("порог зависания должен быть числом минут (например, 30)")
    profiles = values.get("profiles", DEFAULTS["profiles"])
    try:
        count = int(float(str(profiles).replace(",", ".")))
        if 1 <= count <= PROFILES_MAX:
            clean["profiles"] = count
        else:
            problems.append(f"профилей Colab — от 1 до {PROFILES_MAX}")
    except (TypeError, ValueError):
        problems.append("число профилей Colab должно быть целым (например, 2)")
    per_profile = values.get("parts_per_profile", DEFAULTS["parts_per_profile"])
    try:
        count = int(float(str(per_profile).replace(",", ".")))
        if 0 <= count <= PER_PROFILE_MAX:
            clean["parts_per_profile"] = count
        else:
            problems.append(f"частей на один аккаунт — от 0 (не переключать) до {PER_PROFILE_MAX}")
    except (TypeError, ValueError):
        problems.append("частей на один аккаунт — целое число (например, 2)")
    retry = values.get("retry_hours", DEFAULTS["retry_hours"])
    try:
        hours = round(float(str(retry).replace(",", ".")), 2)
        if 0 <= hours <= RETRY_MAX:
            clean["retry_hours"] = hours
        else:
            problems.append(f"пауза, когда упали все аккаунты, — от 0 (ждать вас) до {RETRY_MAX:g} ч")
    except (TypeError, ValueError):
        problems.append("пауза, когда упали все аккаунты, — число часов (например, 4)")
    raw = values.get("keep_raw_days", DEFAULTS["keep_raw_days"])
    try:
        raw_days = int(float(str(raw).replace(",", ".")))
        if RAW_MIN <= raw_days <= RAW_MAX:
            clean["keep_raw_days"] = raw_days
        else:
            problems.append(f"сырьё собранной книги — от {RAW_MIN} до {RAW_MAX} дней")
    except (TypeError, ValueError):
        problems.append("сколько хранить сырьё собранной книги — число дней (например, 2)")
    keep = values.get("keep_days", DEFAULTS["keep_days"])
    try:
        days = int(float(str(keep).replace(",", ".")))
        if KEEP_MIN <= days <= KEEP_MAX:
            clean["keep_days"] = days
        else:
            problems.append(f"срок хранения — от {KEEP_MIN} до {KEEP_MAX} дней")
    except (TypeError, ValueError):
        problems.append("срок хранения должен быть числом дней (например, 30)")
    voices = str(values.get("voices", DEFAULTS["voices"])).strip()
    if voices in VOICES:
        clean["voices"] = voices
    elif values.get("voices"):
        problems.append(f"неизвестный режим голосов «{voices}»")
    stress = str(values.get("stress", DEFAULTS["stress"])).strip()
    if stress in STRESS:
        clean["stress"] = stress
    elif values.get("stress"):
        problems.append(f"неизвестный способ расстановки ударений «{stress}»")
    stereo = str(values.get("stereo", DEFAULTS["stereo"])).strip()
    if stereo in STEREO:
        clean["stereo"] = stereo
    elif values.get("stereo"):
        problems.append(f"неизвестный режим звука «{stereo}»")
    provider = str(values.get("llm_provider", DEFAULTS["llm_provider"])).strip()
    if provider in LLM_PROVIDERS:
        clean["llm_provider"] = provider
    elif values.get("llm_provider"):
        problems.append(f"неизвестный поставщик LLM «{provider}»")
    model = str(values.get("llm_model") or "").strip()
    if len(model) > 120 or any(ch.isspace() for ch in model):
        problems.append("название модели LLM — одно слово до 120 знаков")
    else:
        clean["llm_model"] = model
    for name in SECRETS:
        clean[name] = str(values.get(name) or "").strip()
    models = values.get("llm_models")
    clean["llm_models"] = {k: [str(m) for m in v][:200] for k, v in models.items()
                           if k in LLM_PROVIDERS and isinstance(v, list)} if isinstance(models, dict) else {}
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
        raw = dict(raw)
        for name in SECRETS:                     # пустое поле ключа — «оставить как есть»
            if not str(raw.get(name) or "").strip():
                raw[name] = self.values.get(name, "")
        raw.setdefault("llm_models", self.values.get("llm_models") or {})
        if raw.get("llm_provider", self.llm_provider) != self.llm_provider \
                and str(raw.get("llm_model") or "") == self.values.get("llm_model", ""):
            raw["llm_model"] = ""                # у другого поставщика другие названия моделей
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
    def parts_minutes(self) -> int:
        return int(self.values.get("parts_minutes", DEFAULTS["parts_minutes"]))

    @property
    def parts_hours(self) -> float:
        """Для нарезки (shrink_book --max-hours) — те же минуты, в часах."""
        return self.parts_minutes / 60

    @property
    def drive(self) -> bool:
        return bool(self.values["drive"])

    @property
    def stall_minutes(self) -> int:
        return int(self.values.get("stall_minutes", DEFAULTS["stall_minutes"]))

    @property
    def profiles(self) -> int:
        return int(self.values.get("profiles", DEFAULTS["profiles"]))

    @property
    def parts_per_profile(self) -> int:
        """Сколько частей подряд идут на одном аккаунте Colab, прежде чем перейти на следующий."""
        return int(self.values.get("parts_per_profile", DEFAULTS["parts_per_profile"]))

    @property
    def retry_hours(self) -> float:
        """Все аккаунты Colab упали — через столько часов бот сам начинает круг заново (0 — не сам)."""
        return float(self.values.get("retry_hours", DEFAULTS["retry_hours"]))

    def rotation_line(self) -> str:
        line = (f"по {self.parts_per_profile} част. на аккаунт Colab, дальше следующий по кругу"
                if self.parts_per_profile else "аккаунт Colab меняется только при сбое")
        return line + (f"; упали все — повтор через {self.retry_hours:g} ч" if self.retry_hours
                       else "; упали все — ждём вас")

    @property
    def keep_days(self) -> int:
        return int(self.values.get("keep_days", DEFAULTS["keep_days"]))

    @property
    def keep_raw_days(self) -> int:
        """Сколько дней после сборки лежит сырьё книги — чтобы пересобрать звук без новой озвучки."""
        return int(self.values.get("keep_raw_days", DEFAULTS["keep_raw_days"]))

    @property
    def voices(self) -> str:
        return self.values.get("voices", "one")

    @property
    def stress(self) -> str:
        return self.values.get("stress", "silero")

    @property
    def stereo(self) -> str:
        """Звук готовой книги: «mono» как есть, «space» — стерео-объём при сборке."""
        return self.values.get("stereo", "mono")

    @property
    def llm_provider(self) -> str:
        return self.values.get("llm_provider", "ollama")

    @property
    def llm_key(self) -> str:
        return self.values.get(f"{self.llm_provider}_key", "")

    @property
    def llm_model(self) -> str:
        import llm
        return self.values.get("llm_model") or llm.default_model(self.llm_provider)

    def models(self, provider: str = "") -> list:
        return list((self.values.get("llm_models") or {}).get(provider or self.llm_provider) or [])

    def change(self, **fields) -> str:
        """Одна-две настройки из кнопок Telegram или страницы — остальные не трогаем."""
        return self.update(dict(self.values, **fields))

    def set_profiles(self, count: int) -> str:
        """Число профилей из кнопок «добавить/удалить профиль» — остальные настройки не трогаем."""
        return self.update(dict(self.values, profiles=count))

    def title(self) -> str:
        return MODES[self.mode]

    def summary(self) -> str:
        line = (f"режим «{self.title()}», части по {self.parts_minutes} мин, "
                f"Google Drive: {'да' if self.drive else 'нет'}, "
                f"зависание: {f'{self.stall_minutes} мин без движения' if self.stall_minutes else 'не следим'}, "
                f"профилей Colab: {self.profiles} ({self.rotation_line()}), "
                f"хранить завершённое {self.keep_days} дн., "
                f"сырьё собранной книги {self.keep_raw_days} дн., "
                f"голоса: {VOICES[self.voices]}"
                + (f" ({self.llm_provider}, {self.llm_model}, ключ {'есть' if self.llm_key else 'НЕТ'})"
                   if self.voices == "multi" else "")
                + f", ударения: {STRESS[self.stress]}"
                + (f" ({self.llm_provider}, {self.llm_model}, ключ {'есть' if self.llm_key else 'НЕТ'})"
                   if self.stress == "arbiter" else "")
                + f", звук: {STEREO[self.stereo]}")
        if self.mode == MODE_BOOK:
            line += " (в этом режиме нарезка не используется)"
        else:
            line += " (в этом режиме Google Drive не используется)"
        return line

    def job_line(self) -> str:
        """Строка в журнал задания: по журналу должно быть видно, каким путём пошла книга."""
        if self.mode == MODE_PARTS:
            return (f"режим: с разбивкой на части — книги длиннее {self.parts_minutes} мин будут "
                    f"собираться по частям, склейка в конце")
        return f"режим: одна книга целиком, без нарезки (порог {self.parts_minutes} мин не действует)"
