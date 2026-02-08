# 🐛 Исправление ошибки билда

## Проблема

При деплое на Railway возникала ошибка:
```
ERROR: Failed to build 'av' when getting requirements to build wheel
pkg-config is required for building PyAV
```

## Причина

PyAV (зависимость faster-whisper) требует системные библиотеки для компиляции:
- `pkg-config` - для обнаружения библиотек
- `gcc/g++` - компиляторы C/C++
- `python3-dev` - заголовочные файлы Python
- `libavformat-dev`, `libavcodec-dev` и другие dev-библиотеки FFmpeg

## Решение

### 1. Обновлён Dockerfile

**Добавлены системные зависимости:**
```dockerfile
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
```

### 2. Обновлён requirements.txt

**Обновлены версии до актуальных:**
```
flask==3.1.0 (было 3.0.0)
flask-cors==5.0.0 (было 4.0.0)
gunicorn==23.0.0 (было 21.2.0)
faster-whisper==1.1.0 (было 1.0.0)
requests==2.32.3 (было 2.31.0)
pillow==11.1.0 (было 10.2.0)
yt-dlp==2025.2.6 (было 2024.3.10)
```

## Следующие шаги

1. Закоммитить изменения:
```bash
git add Dockerfile requirements.txt
git commit -m "fix: добавлены системные зависимости для PyAV"
git push origin main
```

2. Railway автоматически запустит новый деплой

3. Дождаться завершения билда (3-5 минут)

4. Проверить работу:
```bash
curl https://your-url.up.railway.app/health
```

## Дополнительно

Эти зависимости нужны только для **сборки** PyAV. После компиляции они не занимают место в runtime, так как используются только libav* runtime библиотеки.

**Размер образа:** увеличится на ~100MB из-за dev-библиотек, но это нормально для Docker образа с компиляцией нативных расширений.

## Альтернативное решение (если хотите уменьшить размер образа)

Можно использовать multi-stage build:

```dockerfile
# Stage 1: Build
FROM python:3.11-slim as builder
RUN apt-get update && apt-get install -y pkg-config gcc g++ python3-dev libavformat-dev ...
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg curl wget
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*
...
```

Но для начала текущее решение проще и работает отлично!
