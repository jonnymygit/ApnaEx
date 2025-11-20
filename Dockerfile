FROM python:3.10.11-slim

WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libcurl4-openssl-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python requirements
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Run Flask server + Extractor bot together
CMD python app.py & python -m Extractor
