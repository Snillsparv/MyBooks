FROM python:3.11-slim

WORKDIR /app

COPY podcast-summarizer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY podcast-summarizer/ .

CMD gunicorn app:app --bind 0.0.0.0:${PORT:-5000}
