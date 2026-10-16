# Образ: Telegram-бот, который принимает книгу и запускает сборку аудиокниги через Google Colab CLI.
#
# Внутри: свежий Google Colab CLI (ставится из PyPI), tmux для фоновых заданий,
# копия конвейера qwen-epub-audiobook (её кладёт CI перед сборкой, см. .github/workflows/build.yml).
# Google Colab CLI требует Python >= 3.12 (проверено на 3.13 — как на рабочей машине).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    HOME=/root \
    BOT_DIR=/opt/bot \
    PIPELINE_DIR=/opt/qwen-epub-audiobook \
    DATA_DIR=/data

# tmux — фоновые задания; attr — xattr для ссылок на Google Drive; tini — аккуратное завершение.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git tmux attr procps tini \
 && rm -rf /var/lib/apt/lists/*

RUN pip install "google-colab-cli"

# Конвейер сборки аудиокниги (копию кладёт workflow из davnozdu/qwen-epub-audiobook).
COPY vendor/qwen-epub-audiobook/ /opt/qwen-epub-audiobook/

# Сам бот.
COPY bot/ /opt/bot/

# В контейнере (Linux) основной запуск — один: scripts/run-book.sh. Windows-обёртку не берём.
COPY scripts/run-book.sh /usr/local/bin/run-book
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/run-book /usr/local/bin/entrypoint.sh \
 && rm -f /opt/qwen-epub-audiobook/colab/cli/run-book.cmd

# Настройки CLI: без автообновления и без лишних проверок при каждом запуске.
RUN mkdir -p /root/.config/colab-cli \
 && printf '{"enable_update_check": false}\n' > /root/.config/colab-cli/settings.json \
 && colab version \
 && python3 --version \
 && tmux -V \
 && /usr/local/bin/run-book --help >/dev/null \
 && echo "образ собран: CLI, tmux, конвейер и Linux-запуск на месте"

VOLUME ["/root/.config", "/data"]
EXPOSE 8080

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s \
  CMD pgrep -f bot.py >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
