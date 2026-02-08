# Dockerfile для youtubeRag - оптимизирован под Railway
FROM python:3.11-slim

# Установка системных зависимостей
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    wget \
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

# Создание рабочей директории
WORKDIR /app

# Копирование файла зависимостей
COPY requirements.txt .

# Установка Python зависимостей
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir -r requirements.txt

# Копирование проекта
COPY . .

# Создание необходимых директорий
RUN mkdir -p input output temp transcripts

# Открытие порта (Railway автоматически определит порт из переменной окружения PORT)
EXPOSE 5055

# Healthcheck для Railway
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-5055}/health || exit 1

# Запуск сервера через Gunicorn для production
CMD gunicorn --bind 0.0.0.0:${PORT:-5055} --workers 2 --threads 4 --timeout 300 --access-logfile - --error-logfile - server:app
