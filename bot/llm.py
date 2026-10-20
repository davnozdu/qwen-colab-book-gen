#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM для многоголосой книги: поставщики, модели по умолчанию и список моделей по ключу.

Саму разметку ролей делает конвейер на машине Colab (cast_book.py); бот только хранит ключ,
даёт выбрать поставщика и модель и передаёт их заданию. Ключи никуда, кроме поставщика, не уходят.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

PROVIDERS = {"ollama": "Ollama Cloud", "deepseek": "DeepSeek API"}
# Модель «DeepSeek 4.1 flash» у поставщиков называется по-разному.
DEFAULT_MODELS = {"ollama": "deepseek-v4.1-flash", "deepseek": "deepseek-flash"}
URLS = {"ollama": "https://ollama.com/api/tags", "deepseek": "https://api.deepseek.com/models"}


def default_model(provider: str) -> str:
    return DEFAULT_MODELS.get(provider, DEFAULT_MODELS["ollama"])


def list_models(provider: str, key: str, timeout: float = 30, opener=None) -> list:
    """Модели поставщика по ключу: Ollama Cloud /api/tags, DeepSeek /models. Ошибка — исключение
    с понятным текстом (ключ отклонён, нет сети)."""
    if provider not in PROVIDERS:
        raise ValueError(f"неизвестный поставщик LLM «{provider}»")
    if not (key or "").strip():
        raise ValueError(f"сначала введите ключ {PROVIDERS[provider]}")
    request = urllib.request.Request(URLS[provider], headers={"Authorization": "Bearer " + key.strip()})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError(f"{PROVIDERS[provider]} отклонил ключ (HTTP {exc.code})") from None
        raise ValueError(f"{PROVIDERS[provider]}: ошибка HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"нет связи с {PROVIDERS[provider]}: {exc}") from None
    if provider == "deepseek":
        names = [item.get("id") for item in data.get("data") or [] if isinstance(item, dict)]
    else:
        names = [item.get("name") or item.get("model") for item in data.get("models") or [] if isinstance(item, dict)]
    return sorted({str(name) for name in names if name})


def masked(key: str) -> str:
    """Ключ для показа на странице и в Telegram: только последние четыре знака."""
    key = (key or "").strip()
    return "" if not key else "••••" + key[-4:]
