#!/usr/bin/env python3
"""Проверки после повтора генерации, остановки всего и отказа Colab (см. smoke.sh)."""
import json, os, pathlib, sys, urllib.request

state = pathlib.Path(os.environ["TG_STATE"])
texts = [json.loads(line)["text"] for line in (state / "sent.jsonl").read_text().splitlines()]
jobs = json.loads(urllib.request.urlopen(os.environ["WEB_BASE"] + "/api/jobs", timeout=30).read())
checks = [("чужому чату бот ничего не даёт",
           any("Это бот владельца" in text for text in texts)
           and not any(item["name"] == "chuzhaya.epub" for item in jobs)),
          ("повтор генерации запускается", any("Повторяю сборку «smoke.epub»" in text for text in texts)),
          ("/stop без номера останавливает всё", any("Остановлено заданий" in text for text in texts)),
          # Если вход в Colab уже есть, бот честно скажет это и ждать кода не станет — оба ответа верны.
          ("случайный текст не считается кодом",
           any(("не похоже на код" in text.lower() or "уже есть" in text.lower()) for text in texts)),
          ("про занятые машины написано понятно",
           any(("заняты" in text or "подождите" in text) for text in texts))]
for name, ok in checks:
    print(f"  {'✓' if ok else '✗'} {name}")
sys.exit(0 if all(ok for _, ok in checks) else 1)
