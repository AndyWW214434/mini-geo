# One image for the API and the Temporal worker; render.yaml picks the command.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 GEO_DATA_DIR=/app/data
WORKDIR /app

COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/geo backend/geo
RUN pip install ./backend

COPY data/brands.csv data/prompts.csv data/history.jsonl.gz data/

RUN useradd --create-home geo
USER geo

# API by default: apply migrations (advisory-locked, idempotent), then serve.
CMD ["sh", "-c", "python -m geo.cli migrate && exec uvicorn geo.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
