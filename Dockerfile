FROM python:3.10.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Supervisord will manage both Flask + Extractor bot
CMD ["supervisord", "-c", "/app/supervisord.conf"]
