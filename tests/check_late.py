#!/usr/bin/env python3
"""Проверки после повтора генерации, остановки всего и отказа Colab (см. smoke.sh)."""
import json, os, pathlib, sys, time, urllib.request

state = pathlib.Path(os.environ["TG_STATE"])
texts = [json.loads(line)["text"] for line in (state / "sent.jsonl").read_text().splitlines()]
jobs = json.loads(urllib.request.urlopen(os.environ["WEB_BASE"] + "/api/jobs", timeout=30).read())
# Ждём итог по книге, запущенной СО СТРАНИЦЫ: именно о ней раньше не приходило уведомление.
for _ in range(60):
    texts = [json.loads(line)["text"] for line in (state / "sent.jsonl").read_text().splitlines()]
    if any("готова" in text for text in texts):
        break
    time.sleep(2)

# Ждём и отказа Colab на limit.epub: отчёт о нём приходит лишь после ближайшего опроса бота,
# поэтому здесь не полагаемся на «уже успел» — дожидаемся статуса задания и перечитываем сообщения.
# Отказ повторяется: сначала тот же профиль ещё раз, затем круг профилей (здесь профиль один) —
# и книга встаёт на паузу. Ждём именно паузы.
for _ in range(90):
    jobs = json.loads(urllib.request.urlopen(os.environ["WEB_BASE"] + "/api/jobs", timeout=30).read())
    if any(item["name"] == "limit.epub" and item["status"] == "paused" for item in jobs):
        break
    time.sleep(2)

# Статус в meta.json и сообщение в Telegram пишутся в одном такте бота, но читаются из разных
# файлов: между «увидели paused» и «долетело sent.jsonl» может быть мгновение. Дожидаемся самого
# сообщения, а не полагаемся на то, что оно уже здесь.
for _ in range(10):
    texts = [json.loads(line)["text"] for line in (state / "sent.jsonl").read_text().splitlines()]
    if any("пауза" in text for text in texts):
        break
    time.sleep(1)

sent = [json.loads(line) for line in (state / "sent.jsonl").read_text().splitlines()]
owner = os.environ.get("CHECK_CHAT", "")
checks = [("по книге со страницы пришёл итог в Telegram (была жалоба: уведомления не было)",
           any("готова" in text for text in texts)),
          # Итог и ссылки на Drive должны адресоваться ЧАТУ владельца, а не пометке «web»:
          # Telegram на такую пометку отвечает «chat not found», и сообщение пропадает.
          ("итог по книге со страницы адресован чату владельца",
           bool(owner) and any(item.get("chat_id") == owner and "готова" in item.get("text", "")
                               for item in sent)),
          ("обёртка тоже писала о старте и о готовности (чат ей передан)",
           any("Поднимаю машину Colab" in text for text in texts)
           and any("Книга собрана" in text for text in texts)),
          # Чужому чату — полное молчание: ни отказа, ни статуса, ни подсказок.
          ("чужому чату бот не отвечает вовсе",
           not any(item.get("chat_id") == "999" for item in sent)
           and not any(item["name"] == "chuzhaya.epub" for item in jobs)),
          ("повтор генерации запускается", any("Повторяю сборку «smoke.epub»" in text for text in texts)),
          ("/stop без номера останавливает всё", any("Остановлено заданий" in text for text in texts)),
          # Если вход в Colab уже есть, бот честно скажет это и ждать кода не станет — оба ответа верны.
          ("случайный текст не считается кодом",
           any(("не похоже на код" in text.lower() or "уже есть" in text.lower()) for text in texts)),
          ("про занятые машины написано понятно",
           any(("заняты" in text or "подождите" in text) for text in texts)),
          ("после первого отказа бот сам повторяет на том же профиле",
           any("limit.epub" in text and "Повторяю на том же профиле" in text for text in texts)),
          ("после второго — пауза с причиной и кнопкой «продолжить»",
           any("limit.epub" in item.get("text", "") and "пауза" in item.get("text", "")
               and "resume:" in json.dumps(item, ensure_ascii=False) for item in sent)),
          ("книга на паузе видна на странице с кнопкой «продолжить»",
           "/resume/" in urllib.request.urlopen(os.environ["WEB_BASE"] + "/", timeout=30).read().decode())]
for name, ok in checks:
    print(f"  {'✓' if ok else '✗'} {name}")
sys.exit(0 if all(ok for _, ok in checks) else 1)
