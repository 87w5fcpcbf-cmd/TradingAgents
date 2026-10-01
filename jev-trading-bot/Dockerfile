# Multi-arch: builds natively on Raspberry Pi 5 (arm64) and on x86.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .
# Non-root; the SQLite db and strategy.md are mounted from the host.
RUN useradd -m bot && mkdir /data && chown bot /data
USER bot
ENV JEVBOT_DB=/data/jevbot.db
ENTRYPOINT ["jevbot"]
