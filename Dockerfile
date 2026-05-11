FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y poppler-utils tesseract-ocr && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# Phase 2 — Run both the FastAPI web service AND the background ingest
# worker in this single container (they share /var/data but have separate
# Python heaps, isolating ingest RAM spikes from request handling). See
# docs/scaling-proposal.md §Phase 2. Set INGEST_WORKER_ENABLED=0 to fall
# back to uvicorn-only (e.g. during cutover rollback).
CMD ["sh", "/app/scripts/start_web_with_worker.sh"]
