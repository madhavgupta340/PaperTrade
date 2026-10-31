# PaperTrade container. Standard library only; tzdata supplies time zones on slim images.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATABASE_PATH=/data/paper_trading.db

WORKDIR /app
RUN pip install --no-cache-dir tzdata==2025.2
COPY server.py ./
COPY paper ./paper
COPY static ./static

# /data is a persistent volume on the host (see fly.toml); the database lives there.
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "server.py"]
