#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Профили Google Colab: несколько аккаунтов, и бот сам переходит на следующий при сбоях.

CLI хранит вход и свои сессии в одной папке — ~/.config/colab-cli (token.json, sessions.json,
settings.json, history). Профиль — это отдельная такая папка на каждый аккаунт:

    ~/.config/colab-profiles/1, ~/.config/colab-profiles/2, …

а ~/.config/colab-cli становится символической ссылкой на папку активного профиля. Поэтому CLI,
вход по коду, обёртка run-book и «сбросить вход» работают как раньше — с тем аккаунтом, на который
сейчас указывает ссылка. Ничего не копируем: переключение — это замена одной ссылки.

Сколько профилей использовать — настройка «profiles» в админке (по умолчанию один). Папки
профилей сверх этого числа не удаляются: вход в них сохраняется на случай, если число вернут.
"""

from __future__ import annotations

import json
import os
import pathlib
import time

LINK_NAME = "colab-cli"
ROOT_NAME = "colab-profiles"
CLI_SETTINGS = {"enable_update_check": False}


class Profiles:
    def __init__(self, link, count=lambda: 1):
        # link — папка настроек CLI (~/.config/colab-cli); count — сколько профилей задано в админке.
        self.link = pathlib.Path(link)
        self.root = self.link.parent / ROOT_NAME
        self.count_source = count
        # Профили включаются только для настоящей папки CLI: в тестах вход лежит где попало,
        # и переносить чужую папку в профиль было бы опасно.
        self.enabled = self.link.name == LINK_NAME
        self.note = ""

    # ────────────────────────────── подготовка ──────────────────────────────
    def prepare(self) -> str:
        """Первый запуск: прежняя папка CLI становится профилем 1, на её месте — ссылка.

        Вход, сделанный до появления профилей, так не теряется. Возвращает строку для журнала.
        """
        if not self.enabled:
            return "профили выключены: вход Colab лежит не в ~/.config/colab-cli"
        self.root.mkdir(parents=True, exist_ok=True)
        first = self.path(1)
        if self.link.is_symlink():
            if not self.link.exists():                    # ссылка на пропавшую папку — чиним
                self.ensure(self.active_number() or 1)
            return f"профили: активен {self.active()}"
        if self.link.is_dir():
            if first.exists():
                # И папка CLI, и профиль 1 уже есть (руками скопировали?) — ничего не теряем.
                backup = self.link.with_name(f"{LINK_NAME}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
                self.link.rename(backup)
                message = f"прежняя папка CLI отложена в {backup.name}"
            else:
                self.link.rename(first)
                message = "прежний вход перенесён в профиль 1"
        else:
            message = "создан профиль 1"
        self.activate(1)
        return f"профили: {message}"

    def path(self, number: int) -> pathlib.Path:
        return self.root / str(int(number))

    def ensure(self, number: int) -> pathlib.Path:
        folder = self.path(number)
        folder.mkdir(parents=True, exist_ok=True)
        settings = folder / "settings.json"
        if not settings.exists():
            settings.write_text(json.dumps(CLI_SETTINGS) + "\n", encoding="utf-8")
        return folder

    # ────────────────────────────── состояние ──────────────────────────────
    def count(self) -> int:
        if not self.enabled:
            return 1
        try:
            return max(1, int(self.count_source()))
        except (TypeError, ValueError):
            return 1

    def active_number(self) -> int:
        try:
            return int(pathlib.Path(os.readlink(self.link)).name)
        except (OSError, ValueError):
            return 0

    def active(self) -> int:
        return self.active_number() or 1

    def signed_in(self, number: int) -> bool:
        token = self.folder(number) / "token.json"
        try:
            return token.stat().st_size > 0
        except OSError:
            return False

    def folder(self, number: int) -> pathlib.Path:
        return self.path(number) if self.enabled else self.link

    def home(self, number: int) -> pathlib.Path:
        """HOME, в котором ~/.config/colab-cli — папка именно этого профиля.

        С ним CLI смотрит и гасит машины любого аккаунта, не переключая активный: перед подъёмом
        машины бот обходит все аккаунты — у Colab за две машины сразу блокируют.
        """
        shim = self.root.parent / "colab-homes" / str(int(number))
        cli = shim / ".config" / LINK_NAME
        target = self.folder(number).resolve()
        if not cli.is_symlink() or pathlib.Path(os.readlink(cli)) != target:
            cli.parent.mkdir(parents=True, exist_ok=True)
            temp = cli.with_name(f".{LINK_NAME}.{os.getpid()}.tmp")
            try:
                temp.unlink()
            except OSError:
                pass
            temp.symlink_to(target)
            os.replace(temp, cli)
        return shim

    def account(self, number: int) -> str:
        """Адрес Google-аккаунта профиля из кэша (account.json), если он относится к текущему входу."""
        folder = self.folder(number)
        try:
            cached = json.loads((folder / "account.json").read_text(encoding="utf-8"))
            if cached.get("token_mtime") == (folder / "token.json").stat().st_mtime:
                return str(cached.get("email") or "")
        except (OSError, ValueError, AttributeError):
            pass
        return ""

    def lookup(self, number: int, timeout: float = 20) -> str:
        """Узнать адрес аккаунта у Google по входу CLI и запомнить его в папке профиля.

        У CLI есть право userinfo.email: обмениваем refresh_token на свежий токен доступа и
        спрашиваем userinfo. Ни токен, ни адрес никуда, кроме Google и папки профиля, не уходят.
        Не вышло (нет сети, вход отозван) — пустая строка: имя аккаунта — подсказка, а не условие.
        """
        import urllib.parse
        import urllib.request
        known = self.account(number)
        if known:
            return known
        folder = self.folder(number)
        token_file = folder / "token.json"
        try:
            token = json.loads(token_file.read_text(encoding="utf-8"))
            form = urllib.parse.urlencode({
                "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                "client_id": token["client_id"], "client_secret": token.get("client_secret", "")}).encode()
            uri = token.get("token_uri") or "https://oauth2.googleapis.com/token"
            with urllib.request.urlopen(urllib.request.Request(uri, data=form), timeout=timeout) as answer:
                access = json.loads(answer.read().decode())["access_token"]
            request = urllib.request.Request("https://openidconnect.googleapis.com/v1/userinfo",
                                             headers={"Authorization": f"Bearer {access}"})
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                email = str(json.loads(answer.read().decode()).get("email") or "")
        except Exception:
            return ""
        if email:
            try:
                (folder / "account.json").write_text(json.dumps(
                    {"email": email, "token_mtime": token_file.stat().st_mtime}) + "\n", encoding="utf-8")
            except OSError:
                pass
        return email

    def label(self, number: int) -> str:
        """«профиль 2 (name@gmail.com)» — для журнала и сообщений в Telegram."""
        email = self.account(number)
        return f"профиль {number}" + (f" ({email})" if email else "")

    def numbers(self) -> list:
        return list(range(1, self.count() + 1))

    # ────────────────────────────── переключение ──────────────────────────────
    def activate(self, number: int) -> str:
        """Сделать профиль активным: ссылка папки CLI начинает указывать на него."""
        number = int(number)
        if not self.enabled:
            return "Профили недоступны: вход Colab хранится не в ~/.config/colab-cli."
        if not 1 <= number <= max(self.count(), 1):
            return f"Профиля {number} нет: в настройках их {self.count()}."
        self.ensure(number)
        temp = self.link.with_name(f".{LINK_NAME}.{os.getpid()}.tmp")
        try:
            temp.unlink()
        except OSError:
            pass
        # Ссылка относительная: том с настройками монтируется в разные места (/home/bot, ./config).
        temp.symlink_to(pathlib.Path(ROOT_NAME) / str(number))
        os.replace(temp, self.link)
        return f"Активен профиль {number}" + (" — вход есть." if self.signed_in(number)
                                              else " — входа нет: войдите в Colab под этим аккаунтом.")

    def next_after(self, tried) -> int:
        """Следующий профиль по кругу после активного, ещё не пробованный. 0 — круг пройден."""
        tried = {int(number) for number in tried or []}
        count = self.count()
        current = self.active()
        for step in range(1, count + 1):
            candidate = (current - 1 + step) % count + 1
            if candidate not in tried:
                return candidate
        return 0

    def fit_count(self) -> None:
        """Число профилей уменьшили — активный за его пределами переключаем на первый."""
        if self.enabled and self.active() > self.count():
            self.activate(1)

    def logout(self, number: int) -> str:
        """Выйти из профиля: вход и состояние CLI откладываются рядом с меткой времени.

        Так же, как «сбросить вход»: файлы не стираются, а переименовываются — по ошибке нажатую
        кнопку можно отменить руками. Папка профиля остаётся, войти можно снова.
        """
        folder = self.folder(number)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        moved = []
        for name in ("token.json", "sessions.json", "history", "account.json"):
            item = folder / name
            if item.exists():
                item.rename(folder / f"{name}.backup-{stamp}")
                moved.append(name)
        return (f"Профиль {number}: вход сброшен." if moved
                else f"Профиль {number}: входа и не было.")

    def delete(self, number: int) -> tuple:
        """Удалить профиль: папка уходит в корзину профилей, профили после него сдвигаются.

        Возвращает (сообщение, новое число профилей). Последний профиль не удаляется — без него
        CLI некуда положить вход; вместо этого из него можно выйти.
        """
        number, count = int(number), self.count()
        if not self.enabled:
            return "Профили недоступны: вход Colab хранится не в ~/.config/colab-cli.", count
        if count <= 1:
            return "Последний профиль не удаляется — из него можно только выйти.", count
        if not 1 <= number <= count:
            return f"Профиля {number} нет.", count
        active = self.active()
        trash = self.root / ".deleted"
        trash.mkdir(parents=True, exist_ok=True)
        folder = self.path(number)
        if folder.exists():
            folder.rename(trash / f"{number}-{time.strftime('%Y%m%d-%H%M%S')}")
        for later in range(number + 1, count + 1):          # 3 → 2 и т.д.: номера без дыр
            if self.path(later).exists():
                self.path(later).rename(self.path(later - 1))
        count -= 1
        if active == number:
            active = 1
        elif active > number:
            active -= 1
        self.ensure(active)
        temp = self.link.with_name(f".{LINK_NAME}.{os.getpid()}.tmp")
        try:
            temp.unlink()
        except OSError:
            pass
        temp.symlink_to(pathlib.Path(ROOT_NAME) / str(active))
        os.replace(temp, self.link)
        return f"Профиль {number} удалён. Профилей: {count}, активен {active}.", count

    def lines(self) -> list:
        """Строки для Telegram и страницы: номер, вход, активный ли."""
        result = []
        for number in self.numbers():
            mark = "▶️" if number == self.active() else "•"
            email = self.account(number)
            signed = (f"вход есть{f' ({email})' if email else ''}" if self.signed_in(number)
                      else "входа нет")
            result.append(f"{mark} Профиль {number} — {signed}")
        return result
