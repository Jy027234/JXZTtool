FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml README.md parsecore.toml parsecore.queue.toml parsecore.remote-http.toml.example parsecore.pgvector.toml.example parsecore.pgvector.fake-embedding.toml.example ./
COPY src ./src

RUN pip install --no-cache-dir -e ".[api,parsers,storage]"

EXPOSE 8090

CMD ["python", "-m", "parsecore.cli", "serve", "--config", "parsecore.toml", "--host", "0.0.0.0", "--port", "8090"]
