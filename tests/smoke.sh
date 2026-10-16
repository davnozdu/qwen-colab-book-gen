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

# Порт страницы: в контейнере 8080 (совпадает с EXPOSE в образе), локально 8090, чтобы не мешать.
if [ "${RUN_LOCAL:-0}" = "1" ]; then WEB_PORT="${WEB_PORT:-8090}"; else WEB_PORT="${WEB_PORT:-8080}"; fi

cleanup() {
    if [ -n "${BOT_PID:-}" ]; then kill "$BOT_PID" 2>/dev/null || true; fi
    if [ -n "${STUB_PID:-}" ]; then kill "$STUB_PID" 2>/dev/null || true; fi
    if [ "${RUN_LOCAL:-0}" != "1" ]; then
        # Файлы в томе созданы контейнером: возвращаем их владельцу, иначе тома не удалить.
        docker exec qwen-smoke chown -R "$(id -u):$(id -g)" /data 2>/dev/null || true
        docker rm -f qwen-smoke >/dev/null 2>&1 || true
    fi
    if [ -z "${KEEP_WORK:-}" ]; then rm -rf "$WORK" 2>/dev/null || true; fi
}
trap cleanup EXIT

TG_STATE="$STATE" python3 "$HERE/tg_stub.py" "$PORT" > "$WORK/stub.log" 2>&1 &
STUB_PID=$!
sleep 2
if ! kill -0 "$STUB_PID" 2>/dev/null; then
    echo "заглушка Telegram не поднялась (порт $PORT занят?):"; cat "$WORK/stub.log"
    exit 1
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
    docker run -d --name qwen-smoke --network host \
        -e TELEGRAM_BOT_TOKEN=test -e TELEGRAM_API_BASE="http://127.0.0.1:$PORT" \
        -e TELEGRAM_CHAT_ID="$CHAT" -e POLL_SECONDS=1 -e PIPELINE_DIR=/stub/pipeline \
        -e DATA_DIR=/data -e FAKE_SECONDS="${FAKE_SECONDS:-4}" -e HOME=/root -e WEB_PORT="$WEB_PORT" \
        -v "$HERE/pipeline:/stub/pipeline:ro" \
        -v "$WORK/data:/data" "$IMAGE" >/dev/null
fi
sleep 3

# Обновления отправляем заглушке по HTTP: единственный писатель — она сама.
feed() {
    TG_STUB_URL="http://127.0.0.1:$PORT/_push" PAYLOAD="$1" python3 "$HERE/feed.py"
}

echo "книга отправлена, жду сборку…"
feed "{\"update_id\":1,\"message\":{\"message_id\":1,\"chat\":{\"id\":$CHAT},\"document\":{\"file_id\":\"F1\",\"file_name\":\"smoke.epub\",\"file_size\":2048}}}"
sleep 4

# Пока книга собирается, вторую бот брать не должен: собираем по одной.
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
feed "{\"update_id\":20,\"message\":{\"message_id\":20,\"chat\":{\"id\":$CHAT},\"text\":\"/repeat $SESSION\"}}"
sleep 3
# Останавливаем, пока повтор ещё идёт, — иначе останавливать будет нечего.
feed "{\"update_id\":21,\"message\":{\"message_id\":21,\"chat\":{\"id\":$CHAT},\"text\":\"/stop\"}}"
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
WEB_BASE="http://127.0.0.1:$WEB_PORT" python3 "$HERE/run_via_web.py" limit.epub
sleep 14

echo "--- проверки после повтора и остановки ---"
if ! TG_STATE="$STATE" python3 "$HERE/check_late.py"; then fail=1; fi

echo "--- проверяю веб-страницу ---"
if ! WEB_BASE="http://127.0.0.1:$WEB_PORT" WORK="$WORK" python3 "$HERE/check_web.py"; then fail=1; fi

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
