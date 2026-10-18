#!/usr/bin/env python3
"""Сквозная проверка режима частей: нарезка в контейнере, очередь частей, кнопка «продолжить».

Что проверяем на живом боте (в контейнере или локально с заглушками):
  1. режим частей включается со страницы;
  2. книга режется САМИМ ботом (машина на это не тратится) — появляется карточка «Книга по частям»;
  3. части идут по одной в общей очереди, кнопка «продолжить генерацию» на месте;
  4. когда все части готовы, бот пишет об этом и цепочка закрывается;
  5. режим возвращается на «одну книгу целиком», чтобы не влиять на остальные проверки.

Запускается из smoke.sh: python3 check_chain.py <адрес страницы> <каталог состояния>.
"""
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip("/")
STATE = pathlib.Path(sys.argv[2])
results = []
started = time.time()


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + extra) if extra and not ok else ''}")


def get(path: str, timeout: int = 30):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        return 0, str(exc).encode()


def post(path: str, fields=None):
    body = urllib.parse.urlencode(fields).encode() if fields is not None else b""
    request = urllib.request.Request(BASE + path, data=body)
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=200) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def jobs():
    code, payload = get("/api/jobs")
    return json.loads(payload) if code == 200 else []


def page() -> str:
    return get("/")[1].decode("utf-8", "replace")


def wait_for(predicate, seconds: int, what: str):
    """Ждём состояние страницы: бот работает в своём потоке, мгновенных переходов тут нет."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        text = page()
        if predicate(text):
            return text
        time.sleep(3)
    print(f"  (не дождался: {what}, {seconds} с)")
    return page()


def wait_jobs(predicate, seconds: int, what: str):
    """Ждём состояние заданий: части появляются в очереди через несколько секунд после нарезки."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        listed = jobs()
        if predicate(listed):
            return listed
        time.sleep(3)
    print(f"  (не дождался: {what}, {seconds} с)")
    return jobs()


books = sorted(pathlib.Path(STATE, "data", "books").glob("*.epub"))
book = next((item for item in books if "limit" not in item.name.lower()), None)
if book is None:
    print("  (нет книги для проверки режима частей — пропускаю)")
    sys.exit(0)

# ── 1. включаем режим частей ────────────────────────────────────────────────
post("/settings", {"mode": "parts", "parts_hours": "1.5"})
check("режим частей включается со страницы",
      "режим «с разбивкой на части" in page(), page()[-300:])

# ── 2. книга режется ботом, а не машиной ────────────────────────────────────
check("книга ставится в работу со страницы", post(f"/run/{urllib.parse.quote(book.name)}") in (200, 303))
card = wait_for(lambda text: "Книга по частям" in text, 90, "карточка частей")
check("бот разрезал книгу сам и показал карточку частей", "Книга по частям" in card,
      page()[:200])
check("кнопка «продолжить генерацию» на месте", "продолжить генерацию" in card)

parts_listed = wait_jobs(lambda listed: any("часть" in (job.get("name") or "").lower()
                                           for job in listed), 120, "части в очереди")
parts_listed = [job for job in parts_listed if "часть" in (job.get("name") or "").lower()]
check("части поставлены в очередь отдельными заданиями", len(parts_listed) >= 1,
      f"найдено: {len(parts_listed)}")
check("части помечены как часть книги, а не как новая книга",
      all("часть" in (job.get("name") or "").lower() for job in parts_listed))

# ── 3. все части доходят до конца, и книга собирается в контейнере ──────────
done_card = wait_for(lambda text: "книга собрана" in text or "сборка книги не удалась" in text,
                     300, "сборка книги")
check("бот сам запустил сборку книги, когда все части готовы",
      "Собираю книгу здесь" in done_card or "книга собрана" in done_card, page()[-400:])
check("книга собрана и записана в результаты", "книга собрана" in done_card, page()[-400:])
finished = [job for job in jobs() if "часть" in (job.get("name") or "").lower()]
check("у каждой части свой архив", bool(finished) and all(job.get("result") for job in finished),
      str([(job["name"], bool(job.get("result"))) for job in finished]))
gluers = [job for job in jobs() if "сборка книги" in (job.get("name") or "").lower()]
glue_log = ""
for job in gluers:
    log = pathlib.Path(STATE, "data", "jobs") / job["id"] / "log.txt"
    glue_log += log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
# Сборка идёт в контейнере: в журнале нет ни подъёма машины, ни сессий Colab — только склейка.
check("сборка книги — отдельное задание и идёт в контейнере, а не в облаке",
      bool(gluers) and all(job["status"] == "done" for job in gluers)
      and "сборка книги: режим final" in glue_log and "поднимаю сессию" not in glue_log,
      str([(job["name"], job["status"]) for job in gluers]) + " | " + glue_log[-300:])
check("сборке отдали каталог с частями в томе и их количество",
      f"частей найдено {len(finished)}, ждём {len(finished)}" in glue_log
      and all((pathlib.Path(STATE, "data", "parts") / chain_parts / f"part-{index}").is_dir()
              for chain_parts in [item.name for item in pathlib.Path(STATE, "data", "parts").iterdir()]
              for index in (1, 2)),
      glue_log[-300:])
check("архив собранной книги лежит в результатах и бот на него ссылается",
      bool(gluers) and all(job.get("result") and pathlib.Path(job["result"]).is_file() for job in gluers),
      str([(job.get("result"), job.get("note")) for job in gluers]))
check("в карточке части показаны как готовые",
      "готово" in done_card.split("Готовые архивы")[0]
      and "в очереди" not in done_card.split("Готовые архивы")[0])
check("части идут по одной: одновременно не больше одного задания",
      len([job for job in jobs() if job["status"] == "running"]) <= 1)

sent = STATE / "tgstate" / "sent.jsonl"
sent_text = sent.read_text(encoding="utf-8") if sent.exists() else ""
check("о нарезке, частях и сборке написано в Telegram",
      "режу на части" in sent_text and ("Собираю книгу здесь" in sent_text or "книга собрана" in sent_text))

# ── 4. возвращаем режим, чтобы не мешать остальным проверкам ────────────────
post("/settings", {"mode": "book", "parts_hours": "1.5", "drive": "on"})
check("режим возвращается на «одну книгу целиком»",
      "режим «одна книга целиком" in page())

bad = [name for name, ok in results if not ok]
print(f"  (проверок: {len(results)}, время {time.time() - started:.0f} с)")
sys.exit(1 if bad else 0)
