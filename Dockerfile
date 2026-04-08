FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y poppler-utils tesseract-ocr sqlite3 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Verify the recovery code is present
RUN grep -c "malformed" hoaware/db.py && echo "DB recovery code present"

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "echo ENTRYPOINT_START && if [ -f /var/data/hoa_index.db ]; then python3 -c \"import sqlite3; c=sqlite3.connect('/var/data/hoa_index.db'); c.execute('SELECT count(*) FROM sqlite_master')\" 2>/dev/null || (echo DB_CORRUPT_FIXING && mv /var/data/hoa_index.db /var/data/hoa_index.db.corrupt && rm -f /var/data/hoa_index.db-wal /var/data/hoa_index.db-shm && echo DB_FIXED); fi && uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
