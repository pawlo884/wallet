FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Model mowy i cache Hugging Face na wolumenie danych (reszta systemu plików jest read-only).
    STT_MODELS_DIR=/app/data/models \
    HF_HOME=/app/data/hf \
    XDG_CACHE_HOME=/app/data/cache

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
RUN useradd --create-home --uid 10001 bot && mkdir -p /app/data && chown bot /app/data
USER bot

CMD ["python", "-m", "bot.main"]
