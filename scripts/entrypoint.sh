#!/bin/sh
# Точка входа контейнера: готовим тома и запускаем бота.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/books" "$DATA_DIR/results" "$DATA_DIR/jobs"
mkdir -p "$HOME/.config/colab-cli" "$HOME/.config/qwen-book"

if [ ! -f "$HOME/.config/colab-cli/settings.json" ]; then
    printf '{"enable_update_check": false}\n' > "$HOME/.config/colab-cli/settings.json"
fi

echo "[entrypoint] данные: $DATA_DIR | конвейер: ${PIPELINE_DIR:-/opt/qwen-epub-audiobook}"
echo "[entrypoint] tmux: $(command -v tmux || echo 'нет — задания пойдут отдельными процессами')"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "[entrypoint] ОШИБКА: не задан TELEGRAM_BOT_TOKEN (см. .env.example и compose.yaml)" >&2
    exit 2
fi

exec python3 "${BOT_DIR:-/opt/bot}/bot.py"
