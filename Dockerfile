FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md parsecore.toml parsecore.queue.toml ./
COPY src ./src

RUN pip install --no-cache-dir -e ".[api,parsers]"

EXPOSE 8090

CMD ["python", "-m", "parsecore.cli", "serve", "--config", "parsecore.toml", "--host", "0.0.0.0", "--port", "8090"]
