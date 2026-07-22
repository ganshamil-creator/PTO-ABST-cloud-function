# Cloud Run образ: Python (для нашего кода) + Java (для OpenDataLoader).
# Обычные Cloud Functions это не поддерживают — там нет Java, поэтому именно
# Cloud Run с этим Dockerfile, не голый Python-деплой, как для остальных
# файлов проекта.

FROM python:3.11-slim

# Java нужна ТОЛЬКО из-за OpenDataLoader (он спавнит JVM-процесс изнутри
# Python-обёртки). Ставим headless JRE — GUI не нужен, размер поменьше.
# openjdk-17 в текущей репе Debian (trixie) больше недоступен — берём 21-ю,
# она тоже подходит (OpenDataLoader требует Java 17+).
RUN apt-get update && \
    apt-get install -y --no-install-recommends openjdk-21-jre-headless && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py verify_table.py ./

ENV PORT=8080
EXPOSE 8080

CMD exec functions-framework --target=extract_and_verify_tables --port=$PORT
