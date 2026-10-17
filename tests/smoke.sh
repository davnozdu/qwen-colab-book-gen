#!/usr/bin/env bash
# Сквозная проверка образа: бот принимает книгу, собирает её в фоне и присылает итог со ссылками.
#
# В CI (после сборки образа):
#   IMAGE=ghcr.io/davnozdu/qwen-colab-book-gen:<sha> bash tests/smoke.sh
# Локально без Docker (проверяет логику бота и заданий):
#   RUN_LOCAL=1 bash tests/smoke.sh
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/davnozdu/qwen-colab-book-gen:latest}"
# Порты выбираем свои: иначе прогон может попасть на заглушку от прошлого запуска.
PORT="${PORT:-$(( 8000 + RANDOM % 900 ))}"
CHAT="${CHAT:-555}"
pkill -f tg_stub.py 2>/dev/null || true
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
STATE="$WORK/tgstate"
mkdir -p "$STATE" "$WORK/data"
printf '[]\n' > "$STATE/updates.json"
printf 'PK fake epub' > "$STATE/file.bin"
rm -f "$STATE/sent.jsonl"

# Проверяем то же, что будут делать люди: страница внутри слушает 8090 (умолчание образа),
# а наружу публикуем другой номер — как в инструкции «меняйте только левое число».
WEB_PORT="${WEB_PORT:-8090}"      # локальный запуск: без проброса, просто 8090
OUT_PORT="${OUT_PORT:-9090}"      # контейнер: снаружи 9090 → внутри 8090

cleanup() {
    if [ "${RUN_LOCAL:-0}" = "1" ]; then
        # Сабшелл-обёрку kill может не тронуть бота внутри (он её ребёнок, а не она): гасим
        # по имени процесса, иначе следующий локальный прогон упадёт на занятом порту 8090.
        pkill -f "[b]ot.py" 2>/dev/null || true
        pkill -f "tg_stub.py" 2>/dev/null || true
    fi
    if [ -n "${BOT_PID:-}" ]; then kill "$BOT_PID" 2>/dev/null || true; fi
    if [ -n "${STUB_PID:-}" ]; then kill "$STUB_PID" 2>/dev/null || true; fi
    if [ "${RUN_LOCAL:-0}" != "1" ]; then
        # Файлы в томе созданы контейнером: возвращаем их владельцу, иначе тома не удалить.
        docker exec qwen-smoke chown -R "$(id -u):$(id -g)" /data 2>/dev/null || true
        docker rm -f qwen-smoke qwen-mock >/dev/null 2>&1 || true
        docker network rm qwen-smoke-net >/dev/null 2>&1 || true
    fi
    if [ -z "${KEEP_WORK:-}" ]; then rm -rf "$WORK" 2>/dev/null || true; fi
}
trap cleanup EXIT

if [ "${RUN_LOCAL:-0}" = "1" ]; then
    WEB_BASE_PORT="$WEB_PORT"
    TG_STATE="$STATE" TG_HOST=127.0.0.1 python3 "$HERE/tg_stub.py" "$PORT" > "$WORK/stub.log" 2>&1 &
    STUB_PID=$!
    sleep 2
    if ! kill -0 "$STUB_PID" 2>/dev/null; then
        echo "заглушка Telegram не поднялась (порт $PORT занят?):"; cat "$WORK/stub.log"
        exit 1
    fi
else
    WEB_BASE_PORT="$OUT_PORT"        # снаружи страница живёт на опубликованном порту
fi

if [ "${RUN_LOCAL:-0}" = "1" ]; then
    ( cd "$HERE/../bot" && \
      env TELEGRAM_BOT_TOKEN=test TELEGRAM_API_BASE="http://127.0.0.1:$PORT" \
          TELEGRAM_CHAT_ID="$CHAT" POLL_SECONDS=1 DATA_DIR="$WORK/data" \
          PIPELINE_DIR="$HERE/pipeline" STATE_FILE="$WORK/data/state.json" \
          TOKEN_FILE="$WORK/data/token.json" FAKE_SECONDS="${FAKE_SECONDS:-4}" \
          WEB_PORT="$WEB_PORT" \
          python3 bot.py > "$WORK/bot.log" 2>&1 ) &
    BOT_PID=$!
else
    # Порт публикуем так же, как в compose (левое число : правое число), и проверяем снаружи
    # именно его — тогда опечатка вроде «8090:9988» сразу валит тест.
    NET=qwen-smoke-net
    docker network create "$NET" >/dev/null 2>&1 || true
    # Заглушка Telegram — отдельным контейнером в той же сети; наружу отдаём только её порт,
    # чтобы тест мог подкидывать обновления.
    docker run -d --name qwen-mock --network "$NET" -p "${PORT}:${PORT}" \
        -e TG_STATE=/state -e TG_HOST=0.0.0.0 \
        -v "$HERE:/tests:ro" -v "$STATE:/state" \
        python:3.13-slim python /tests/tg_stub.py "$PORT" >/dev/null
    # Бот: порт страницы публикуем как в compose — снаружи OUT_PORT, внутри 8090.
    docker run -d --name qwen-smoke --network "$NET" -p "${OUT_PORT}:8090" \
        -e TELEGRAM_BOT_TOKEN=test -e TELEGRAM_API_BASE="http://qwen-mock:$PORT" \
        -e TELEGRAM_CHAT_ID="$CHAT" -e POLL_SECONDS=1 -e PIPELINE_DIR=/stub/pipeline \
        -e DATA_DIR=/data -e FAKE_SECONDS="${FAKE_SECONDS:-4}" -e HOME=/root \
        -v "$HERE/pipeline:/stub/pipeline:ro" \
        -v "$WORK/data:/data" "$IMAGE" >/dev/null
    sleep 3
    # WEB_PORT не задаём: страница должна слушать 8090 — умолчание образа, как в README.
fi
sleep 3

# Обновления отправляем заглушке по HTTP: единственный писатель — она сама.
feed() {
    TG_STUB_URL="http://127.0.0.1:$PORT/_push" PAYLOAD="$1" python3 "$HERE/feed.py"
}

echo "--- проверяю логику бота (без сети) ---"
if ! python3 "$HERE/check_logic.py"; then fail=1; fi

echo "книга отправлена, жду сборку…"
feed "{\"update_id\":1,\"message\":{\"message_id\":1,\"chat\":{\"id\":$CHAT},\"document\":{\"file_id\":\"F1\",\"file_name\":\"smoke.epub\",\"file_size\":2048}}}"

# Пока книга собирается, вторую бот брать не должен: собираем по одной.
# Вторую отправляем сразу, как первая начала сборку (обёртка пишет об этом в чат), а не по
# фиксированной паузе: заглушка быстрая, и на паузе первая успевала закончиться — бот честно
# принимал вторую, а проверка «одна книга за раз» падала на ровном месте.
for _ in $(seq 1 40); do
    if grep -q "Поднимаю машину Colab" "$STATE/sent.jsonl" 2>/dev/null; then break; fi
    sleep 0.5
done
feed "{\"update_id\":9,\"message\":{\"message_id\":9,\"chat\":{\"id\":$CHAT},\"document\":{\"file_id\":\"F9\",\"file_name\":\"vtoraya.epub\",\"file_size\":2048}}}"
sleep 4

for _ in $(seq 1 40); do
    sleep 2
    if grep -q "готов" "$STATE/sent.jsonl" 2>/dev/null; then break; fi
done

sleep 2
feed "{\"update_id\":10,\"message\":{\"message_id\":10,\"chat\":{\"id\":$CHAT},\"text\":\"/jobs\"}}"
sleep 4

echo "--- что прислал бот ---"
cat "$STATE/sent.jsonl" 2>/dev/null || true

echo "--- проверки ---"
fail=0
TG_STATE="$STATE" python3 "$HERE/check_sent.py" || fail=1
[ -f "$WORK/data/results/smoke.zip" ] || { echo "  ✗ архива нет в томе результатов"; fail=1; }

echo "--- чужой чат: бот не обслуживает ---"
feed "{\"update_id\":20,\"message\":{\"message_id\":20,\"chat\":{\"id\":999},\"text\":\"/help\"}}"
feed "{\"update_id\":21,\"message\":{\"message_id\":21,\"chat\":{\"id\":999},\"document\":{\"file_id\":\"F9\",\"file_name\":\"chuzhaya.epub\",\"file_size\":2048}}}"
sleep 6

echo "--- вход в Colab: фраза вместо кода не должна ломать вход ---"
feed "{\"update_id\":30,\"message\":{\"message_id\":30,\"chat\":{\"id\":$CHAT},\"text\":\"/login\"}}"
sleep 8
feed "{\"update_id\":31,\"message\":{\"message_id\":31,\"chat\":{\"id\":$CHAT},\"text\":\"привет, это не код\"}}"
sleep 5

echo "--- готовлю книги для проверки каталога ---"
mkdir -p "$WORK/data/books"
if [ "${RUN_LOCAL:-0}" = "1" ]; then
    for index in $(seq -w 0 24); do printf 'probe %s' "$index" > "$WORK/data/books/probe-$index.epub"; done
else
    # В том пишет контейнер (от root), поэтому и файлы создаём его руками.
    docker exec qwen-smoke sh -c 'for i in $(seq -w 0 24); do printf "probe %s" "$i" > /data/books/probe-$i.epub; done'
fi

echo "--- повтор генерации и остановка всего ---"
SESSION=$(TG_STATE="$STATE" python3 -c "
import json, os, pathlib
texts = [json.loads(line) for line in (pathlib.Path(os.environ['TG_STATE']) / 'sent.jsonl').read_text().splitlines()]
for line in texts:
    if '/log ' in line['text']:
        print(line['text'].split('/log ')[-1].strip())
        break
")
feed "{\"update_id\":40,\"message\":{\"message_id\":40,\"chat\":{\"id\":$CHAT},\"text\":\"/repeat $SESSION\"}}"
sleep 3
# Останавливаем, пока повтор ещё идёт, — иначе останавливать будет нечего.
feed "{\"update_id\":41,\"message\":{\"message_id\":41,\"chat\":{\"id\":$CHAT},\"text\":\"/stop\"}}"
sleep 6

echo "--- книга, на которой Colab отказывает (проверяем понятное объяснение) ---"
cp "$WORK/data/books/smoke.epub" "$WORK/data/books/limit.epub" 2>/dev/null || true
if [ ! -f "$WORK/data/books/limit.epub" ]; then
    if [ "${RUN_LOCAL:-0}" = "1" ]; then
        printf 'PK fake' > "$WORK/data/books/limit.epub"
    else
        docker exec qwen-smoke sh -c 'printf "PK fake" > /data/books/limit.epub'
    fi
fi
WEB_BASE="http://127.0.0.1:${WEB_BASE_PORT:-$WEB_PORT}" python3 "$HERE/run_via_web.py" limit.epub
sleep 14

echo "--- проверки после повтора и остановки ---"
if ! TG_STATE="$STATE" WEB_BASE="http://127.0.0.1:${WEB_BASE_PORT:-$WEB_PORT}" python3 "$HERE/check_late.py"; then fail=1; fi

echo "--- кнопки: статус, задания, остановка ---"
# Нажатие кнопки приходит как callback_query — тот же путь, что у живого Telegram.
BEFORE_BUTTONS=$(wc -l < "$STATE/sent.jsonl" | tr -d ' ')
button() {
    feed "{\"update_id\":$1,\"callback_query\":{\"id\":\"btn$1\",\"from\":{\"id\":$CHAT},\"message\":{\"message_id\":$1,\"chat\":{\"id\":$CHAT},\"text\":\"кнопка\"},\"data\":\"$2\"}}"
}
button 50 status
sleep 4
button 51 jobs
sleep 4
button 52 stop_ask
sleep 4
if ! TG_STATE="$STATE" BUTTONS_FROM="$BEFORE_BUTTONS" python3 "$HERE/check_buttons.py"; then fail=1; fi

echo "--- проверяю веб-страницу ---"
if ! WEB_BASE="http://127.0.0.1:${WEB_BASE_PORT:-$WEB_PORT}" WORK="$WORK" python3 "$HERE/check_web.py"; then fail=1; fi

# Ничего не должно остаться работать: ни фонового окна, ни процесса сборки.
# Даём пару секунд на завершение и не считаем зомби (они уже не работают).
if [ "${RUN_LOCAL:-0}" != "1" ]; then
    sleep 3
    if docker exec qwen-smoke sh -c 'tmux ls 2>/dev/null | grep -q .'; then
        echo "  ✗ осталась работающая сессия tmux"; fail=1
    else
        echo "  ✓ фоновых окон не осталось"
    fi
    if docker exec qwen-smoke sh -c 'ps -eo stat=,args= | grep "run-book" | grep -v grep | grep -qv "^Z"'; then
        echo "  ✗ остался процесс сборки"; fail=1
    else
        echo "  ✓ процессов сборки не осталось"
    fi
fi

if [ "$fail" = "0" ]; then echo "ПРОВЕРКА ПРОЙДЕНА"; exit 0; fi
echo "ПРОВЕРКА НЕ ПРОЙДЕНА"; exit 1
