#!/usr/bin/env python3
"""Кладёт обновление в очередь заглушки Telegram (используется в smoke-тесте)."""
import json, os, pathlib

state = pathlib.Path(os.environ["TG_STATE"])
pending = json.loads((state / "updates.json").read_text())
pending.append(json.loads(os.environ["PAYLOAD"]))
(state / "updates.json").write_text(json.dumps(pending))
