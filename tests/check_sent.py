#!/usr/bin/env python3
"""Проверяет, что бот ответил как задумано (читает текст, а не сырой JSON)."""
import json, os, pathlib, sys

state = pathlib.Path(os.environ["TG_STATE"])
texts = [json.loads(line)["text"] for line in (state / "sent.jsonl").read_text().splitlines()]
checks = [("подтверждение приёма книги", any("Принял" in text for text in texts)),
          ("отчёт о готовности", any("готова" in text for text in texts)),
          ("ссылки на Google Drive", any("drive.google.com" in text for text in texts)),
          ("журнал по команде /jobs", any("smoke.epub · done" in text for text in texts)),
          ("одна книга за раз: вторая отклонена", any("идёт сборка" in text for text in texts))]
for name, ok in checks:
    print(f"  {'✓' if ok else '✗'} {name}")
sys.exit(0 if all(ok for _, ok in checks) else 1)
