FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y poppler-utils tesseract-ocr sqlite3 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Verify the recovery code is present
RUN grep -c "malformed" hoaware/db.py && echo "DB recovery code present"

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
