#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вход в Google Colab CLI прямо из Telegram.

CLI входит так: печатает ссылку, вы подтверждаете доступ в браузере, Google показывает код,
код нужно вставить в терминал. В контейнере терминала у вас нет, поэтому терминалом управляет
бот: он вылавливает ссылку и отдаёт вам, а присланный код кладёт в тот же терминал.

Ничего секретного бот не хранит: код авторизации сразу уходит в CLI, а тот сохраняет обычный
token.json в смонтированный том (/root/.config/colab-cli/token.json).
"""

from __future__ import annotations

import os
import pathlib
import re
import select
import shutil
import signal
import time

URL_RE = re.compile(r"https://accounts\.google\.com/o/oauth2/[^\s]+")
CODE_HINT = "Enter the authorization code"


class Login:
    """Одна попытка входа: держит терминал CLI, пока не придёт код."""

    def __init__(self, token_file: pathlib.Path, command: list | None = None):
        self.token_file = pathlib.Path(token_file)
        self.command = command or ["colab", "usage"]

    def has_token(self) -> bool:
        return self.token_file.is_file() and self.token_file.stat().st_size > 0

    def terminal_logged_in(self, timeout: float = 60) -> bool:
        """Живёт ли вход: короткий вызов CLI без запроса кода."""
        import subprocess
        binary = shutil.which("colab")
        if not binary:
            return False
        try:
            done = subprocess.run([binary, "usage"], capture_output=True, text=True,
                                  timeout=timeout, env=dict(os.environ), input="")
        except (subprocess.TimeoutExpired, OSError):
            return False
        return done.returncode == 0 and CODE_HINT not in (done.stdout + done.stderr)

    def start(self):
        """Запускает CLI в своём терминале и возвращает объект с ссылкой и ожиданием кода."""
        import pty
        binary = shutil.which(str(self.command[0]))
        if not binary:
            raise RuntimeError(f"нет команды {self.command[0]} — CLI не установлен")
        pid, master = pty.fork()
        if pid == 0:                                   # ребёнок: подменяем себя на CLI
            try:
                os.execvpe(binary, [binary, *self.command[1:]], dict(os.environ))
            except Exception:
                os._exit(127)
        return Session(master, pid)


class Session:
    """Терминал CLI: вычитываем ссылку, затем передаём код и собираем результат."""

    SESSION_TTL = 15 * 60      # попытка входа живёт четверть часа: дальше код всё равно просрочен

    def __init__(self, master: int, pid: int):
        self.master = master
        self.pid = pid
        self.buffer = ""
        self.started = time.monotonic()
        self.reaped = False        # процесс уже забрали: повторно waitpid нельзя

    def read(self, seconds: float = 3.0) -> str:
        ready, _, _ = select.select([self.master], [], [], seconds)
        if not ready:
            return ""
        try:
            chunk = os.read(self.master, 65536)
        except OSError:
            return ""
        text = chunk.decode("utf-8", "replace")
        self.buffer += text
        return text

    def wait_for_url(self, seconds: float = 90) -> str:
        """Ждёт ссылку авторизации (CLI печатает её сразу после запуска)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            text = self.read(2.0)
            found = URL_RE.search(text)
            if found:
                return found.group(0)
            if not text and not self.alive():
                break
        return ""

    def wait_for_prompt(self, seconds: float = 90) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            text = self.read(2.0)
            if CODE_HINT in self.buffer:
                return True
            if not text and not self.alive():
                break
        return CODE_HINT in self.buffer

    def submit_code(self, code: str) -> None:
        os.write(self.master, (code.strip() + "\n").encode())

    def expired(self) -> bool:
        """Пора ли закрыть попытку входа: код из браузера за это время успевает устареть."""
        return time.monotonic() - self.started > self.SESSION_TTL

    def alive(self) -> bool:
        """Живой ли ещё CLI. Процесс забираем только один раз: повторный waitpid — ошибка."""
        if self.reaped:
            return False
        try:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:            # уже забран (например, при повторной проверке)
            self.reaped = True
            return False
        if done == self.pid:
            self.reaped = True
            return False
        return True

    def wait_finish(self, seconds: float = 90) -> str:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            chunk = self.read(2.0)
            if not chunk and not self.alive():
                break
        return self.buffer

    def close(self) -> None:
        try:
            if self.alive():
                os.killpg(self.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.close(self.master)
        except OSError:
            pass

    def summary(self) -> str:
        """Короткая выжимка: без ссылок и служебного шума."""
        lines = [line.strip() for line in self.buffer.splitlines() if line.strip()]
        keep = [line for line in lines
                if "http" not in line and not line.startswith(("Enter the", "After approving",
                                                               "To authorize"))]
        return " | ".join(keep[-4:])[:300]
