#!/usr/bin/env python3
"""Отправляет обновление заглушке Telegram (используется в smoke-тесте).

Пишет не в файл, а по HTTP: так единственным писателем остаётся заглушка и гонок нет.
"""
import json, os, time, urllib.parse, urllib.request

url = os.environ.get("TG_STUB_URL", "http://127.0.0.1:8123/_push")
payload = urllib.parse.urlencode({"update": os.environ["PAYLOAD"]}).encode()

for attempt in range(20):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=10) as response:
            json.load(response)
        break
    except Exception:
        if attempt == 19:
            raise
        time.sleep(0.3)
