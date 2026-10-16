#!/usr/bin/env python3
"""Проверки после повтора генерации, остановки всего и отказа Colab (см. smoke.sh)."""
import json, os, pathlib, sys

state = pathlib.Path(os.environ["TG_STATE"])
texts = [json.loads(line)["text"] for line in (state / "sent.jsonl").read_text().splitlines()]
checks = [("повтор генерации запускается", any("Повторяю сборку" in text for text in texts)),
          ("/stop без номера останавливает всё", any("Остановлено заданий" in text for text in texts)),
          ("про занятые машины написано понятно",
           any(("заняты" in text or "подождите" in text) for text in texts))]
for name, ok in checks:
    print(f"  {'✓' if ok else '✗'} {name}")
sys.exit(0 if all(ok for _, ok in checks) else 1)
