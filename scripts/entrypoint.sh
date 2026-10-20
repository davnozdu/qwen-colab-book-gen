#!/bin/sh
# Точка входа контейнера: готовим тома и запускаем бота.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/books" "$DATA_DIR/results" "$DATA_DIR/jobs"
mkdir -p "$HOME/.config/qwen-book"
# colab-cli — ссылка на папку активного профиля Colab (bot/profiles.py). Ссылку на пропавшую папку
# mkdir -p не переживает («File exists») — тогда создаём саму папку профиля, ссылку бот проверит.
if [ -L "$HOME/.config/colab-cli" ]; then
    target="$(readlink "$HOME/.config/colab-cli")"
    case "$target" in /*) ;; *) target="$HOME/.config/$target" ;; esac
    mkdir -p "$target"
else
    mkdir -p "$HOME/.config/colab-cli"
fi

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
