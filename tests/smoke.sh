#!/usr/bin/env bash
# Сквозная проверка образа: бот принимает книгу, собирает её в фоне и присылает итог со ссылками.
#
# В CI (после сборки образа):
#   IMAGE=ghcr.io/davnozdu/qwen-colab-book-gen:<sha> bash tests/smoke.sh
# Локально без Docker (проверяет логику бота и заданий):
#   RUN_LOCAL=1 bash tests/smoke.sh
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/davnozdu/qwen-colab-book-gen:latest}"
PORT="${PORT:-8123}"
CHAT="${CHAT:-555}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
STATE="$WORK/tgstate"
mkdir -p "$STATE" "$WORK/data"
printf '[]\n' > "$STATE/updates.json"
printf 'PK fake epub' > "$STATE/file.bin"
rm -f "$STATE/sent.jsonl"

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

TG_STATE="$STATE" python3 "$HERE/tg_stub.py" "$PORT" &
STUB_PID=$!
sleep 1

if [ "${RUN_LOCAL:-0}" = "1" ]; then
    ( cd "$HERE/../bot" && \
      env TELEGRAM_BOT_TOKEN=test TELEGRAM_API_BASE="http://127.0.0.1:$PORT" \
          TELEGRAM_CHAT_ID="$CHAT" POLL_SECONDS=1 DATA_DIR="$WORK/data" \
          PIPELINE_DIR="$HERE/pipeline" STATE_FILE="$WORK/data/state.json" \
          TOKEN_FILE="$WORK/data/token.json" FAKE_SECONDS="${FAKE_SECONDS:-4}" \
          python3 bot.py > "$WORK/bot.log" 2>&1 ) &
    BOT_PID=$!
else
    docker run -d --name qwen-smoke --network host \
        -e TELEGRAM_BOT_TOKEN=test -e TELEGRAM_API_BASE="http://127.0.0.1:$PORT" \
        -e TELEGRAM_CHAT_ID="$CHAT" -e POLL_SECONDS=1 -e PIPELINE_DIR=/stub/pipeline \
        -e DATA_DIR=/data -e FAKE_SECONDS="${FAKE_SECONDS:-4}" -e HOME=/root \
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
feed "{\"update_id\":2,\"message\":{\"message_id\":2,\"chat\":{\"id\":$CHAT},\"text\":\"/jobs\"}}"
sleep 4

echo "--- что прислал бот ---"
cat "$STATE/sent.jsonl" 2>/dev/null || true

echo "--- проверки ---"
fail=0
TG_STATE="$STATE" python3 "$HERE/check_sent.py" || fail=1
[ -f "$WORK/data/results/smoke.zip" ] || { echo "  ✗ архива нет в томе результатов"; fail=1; }

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
