#!/usr/bin/env python3
"""Кладёт обновление в очередь заглушки Telegram (используется в smoke-тесте).

Пишем атомарно и с повтором: заглушка и бот читают тот же файл параллельно, поэтому
промежуточное состояние наружу отдавать нельзя.
"""
import json, os, pathlib, time

state = pathlib.Path(os.environ["TG_STATE"])
path = state / "updates.json"

pending = []
for _ in range(40):
    try:
        pending = json.loads(path.read_text(encoding="utf-8") or "[]")
        break
    except (ValueError, FileNotFoundError):
        time.sleep(0.05)

pending.append(json.loads(os.environ["PAYLOAD"]))
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")   # имя уникальное: пишут двое
tmp.write_text(json.dumps(pending), encoding="utf-8")
os.replace(tmp, path)
