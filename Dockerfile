# Etap 1: panel w React (frontend/) → statyczne pliki serwowane przez bota pod /panel/
FROM node:22-alpine AS panel
WORKDIR /panel
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Model mowy i cache Hugging Face na wolumenie danych (reszta systemu plików jest read-only).
    STT_MODELS_DIR=/app/data/models \
    HF_HOME=/app/data/hf \
    XDG_CACHE_HOME=/app/data/cache

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 60 -r requirements.txt

COPY bot ./bot
COPY --from=panel /panel/dist ./bot/static/panel
RUN useradd --create-home --uid 10001 bot && mkdir -p /app/data && chown bot /app/data
USER bot

CMD ["python", "-m", "bot.main"]
