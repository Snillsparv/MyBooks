FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data directory (mount a Railway Volume here)
RUN mkdir -p /data

CMD gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --worker-class gevent --timeout 300
