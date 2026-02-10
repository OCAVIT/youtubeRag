FROM python:3.11-slim

# Системные зависимости (ffmpeg + dev-библиотеки для сборки PyAV)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    pkg-config \
    gcc \
    g++ \
    python3-dev \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    libavfilter-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p input output temp transcripts

EXPOSE ${PORT:-5055}

# 1 worker чтобы не жрать RAM, preload для быстрого старта
CMD exec gunicorn \
    --bind 0.0.0.0:${PORT:-5055} \
    --workers 1 \
    --threads 4 \
    --timeout 300 \
    --preload \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    server:app
