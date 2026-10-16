#!/bin/sh
# Основной (Linux) запуск сборки внутри контейнера: он же используется ботом, он же доступен
# руками — docker compose exec bot run-book "книга.epub"
# Всё, что нужно, уже установлено в образе; ничего докачивать не надо.
set -eu
exec python3 "${PIPELINE_DIR:-/opt/qwen-epub-audiobook}/colab/cli/run-book.py" "$@"
