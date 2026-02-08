# 🧪 Тестовые команды для youtubeRag

Набор curl команд для тестирования всех эндпоинтов API.

## Настройка

```bash
# Установите URL вашего сервера
# Для Railway:
export API_URL="https://youtuberag-production.up.railway.app"

# Для локальной разработки:
export API_URL="http://localhost:5055"

# Тестовое видео (короткое)
export TEST_VIDEO="https://www.youtube.com/watch?v=jNQXAC9IVRw"
```

## 1. Health Check

```bash
# Проверка работоспособности сервера
curl -X GET $API_URL/health

# Ожидаемый ответ:
# {"status":"ok","timestamp":"2024-01-15T12:00:00"}
```

## 2. Транскрипция видео (синхронно)

### С YouTube URL

```bash
curl -X POST $API_URL/transcribe \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\"}"
```

### С локальным файлом

```bash
curl -X POST $API_URL/transcribe \
  -H "Content-Type: application/json" \
  -d '{"file_path": "/app/input/video.mp4"}'
```

### Ожидаемый ответ

```json
{
  "text": "Полный текст транскрипции...",
  "segments": [
    {
      "start": 0.0,
      "end": 2.5,
      "text": "Привет, это тестовое видео"
    },
    {
      "start": 2.5,
      "end": 5.0,
      "text": "Следующий сегмент текста"
    }
  ],
  "language": "ru"
}
```

## 3. Извлечение кадров

### Базовое использование (интервал 5 секунд)

```bash
curl -X POST $API_URL/extract-frames \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\", \"interval_sec\": 5}"
```

### Кастомный интервал (10 секунд)

```bash
curl -X POST $API_URL/extract-frames \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\", \"interval_sec\": 10}"
```

### Ожидаемый ответ

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "frames_count": 120,
  "frames_dir": "/app/output/frames/550e8400-e29b-41d4-a716-446655440000",
  "interval_sec": 5.0,
  "video_duration": 600.5
}
```

## 4. Фоновая обработка видео

### Только транскрипция

```bash
curl -X POST $API_URL/process-video \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\", \"operations\": [\"transcribe\"]}"
```

### Только извлечение кадров

```bash
curl -X POST $API_URL/process-video \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\", \"operations\": [\"extract_frames\"]}"
```

### Обе операции

```bash
curl -X POST $API_URL/process-video \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\", \"operations\": [\"transcribe\", \"extract_frames\"]}"
```

### Ожидаемый ответ

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing"
}
```

## 5. Проверка статуса задачи

```bash
# Замените TASK_ID на реальный ID из предыдущего запроса
export TASK_ID="550e8400-e29b-41d4-a716-446655440000"

curl -X GET $API_URL/status/$TASK_ID
```

### Ответ (в процессе)

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "video_url": "https://...",
  "operations": ["transcribe"],
  "created_at": "2024-01-15T12:00:00"
}
```

### Ответ (завершено)

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "video_url": "https://...",
  "operations": ["transcribe"],
  "created_at": "2024-01-15T12:00:00",
  "completed_at": "2024-01-15T12:05:00",
  "result": {
    "transcription": {
      "text": "...",
      "segments": [...],
      "language": "ru"
    }
  }
}
```

### Ответ (ошибка)

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "error",
  "error": "Video download failed",
  "traceback": "..."
}
```

## 6. Выполнение Python кода

### Простой print

```bash
curl -X POST $API_URL/exec \
  -H "Content-Type: application/json" \
  -d '{"code": "print(1 + 1)"}'
```

### Импорт модулей

```bash
curl -X POST $API_URL/exec \
  -H "Content-Type: application/json" \
  -d '{"code": "import sys\nprint(sys.version)"}'
```

### Математические операции

```bash
curl -X POST $API_URL/exec \
  -H "Content-Type: application/json" \
  -d '{"code": "result = sum([1, 2, 3, 4, 5])\nprint(f\"Sum: {result}\")"}'
```

### Ожидаемый ответ (успех)

```json
{
  "success": true,
  "stdout": "2\n",
  "stderr": "",
  "error": null
}
```

### Ожидаемый ответ (ошибка)

```json
{
  "success": false,
  "stdout": "",
  "stderr": "Traceback...",
  "error": "ZeroDivisionError: division by zero"
}
```

## 7. Скрипты для автоматизации

### Полный workflow: Транскрибировать и подождать результата

```bash
#!/bin/bash

# 1. Запустить фоновую обработку
RESPONSE=$(curl -s -X POST $API_URL/process-video \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\", \"operations\": [\"transcribe\"]}")

# 2. Извлечь task_id
TASK_ID=$(echo $RESPONSE | jq -r '.task_id')
echo "Task ID: $TASK_ID"

# 3. Ждать завершения
while true; do
  STATUS=$(curl -s $API_URL/status/$TASK_ID | jq -r '.status')
  echo "Status: $STATUS"

  if [ "$STATUS" = "completed" ]; then
    echo "Done!"
    curl -s $API_URL/status/$TASK_ID | jq '.result'
    break
  elif [ "$STATUS" = "error" ]; then
    echo "Error occurred!"
    curl -s $API_URL/status/$TASK_ID | jq '.error'
    break
  fi

  sleep 5
done
```

### Сохранить результат транскрипции в файл

```bash
curl -s -X POST $API_URL/transcribe \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\"}" \
  | jq '.text' > transcript.txt

echo "Transcript saved to transcript.txt"
```

### Проверить все эндпоинты

```bash
#!/bin/bash

echo "Testing all endpoints..."

echo "\n1. Health check"
curl -s $API_URL/health | jq

echo "\n2. Transcribe (this will take a while...)"
curl -s -X POST $API_URL/transcribe \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\"}" | jq '.language'

echo "\n3. Extract frames"
curl -s -X POST $API_URL/extract-frames \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\", \"interval_sec\": 10}" | jq '.frames_count'

echo "\n4. Process video (background)"
RESPONSE=$(curl -s -X POST $API_URL/process-video \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\", \"operations\": [\"transcribe\"]}")
TASK_ID=$(echo $RESPONSE | jq -r '.task_id')

echo "\n5. Check status"
curl -s $API_URL/status/$TASK_ID | jq '.status'

echo "\n6. Execute Python code"
curl -s -X POST $API_URL/exec \
  -H "Content-Type: application/json" \
  -d '{"code": "print(\"Hello from youtubeRag!\")"}' | jq '.stdout'

echo "\nAll tests completed!"
```

## Примечания

- Транскрипция может занять 1-5 минут в зависимости от длины видео
- Используйте короткие тестовые видео для быстрой проверки
- Для production используйте фоновую обработку (`/process-video`)
- Логи всегда доступны через `docker-compose logs -f` или в Railway Dashboard

## Рекомендуемые тестовые видео

```bash
# Короткое видео (30 сек)
export TEST_SHORT="https://www.youtube.com/watch?v=jNQXAC9IVRw"

# Среднее видео (5 мин)
export TEST_MEDIUM="https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Русский язык
export TEST_RU="https://www.youtube.com/watch?v=..."
```

## Troubleshooting

### Ошибка: Connection refused

```bash
# Проверьте что сервер запущен
docker-compose ps
# или
curl $API_URL/health
```

### Ошибка: Video download failed

```bash
# Попробуйте другой URL
# Убедитесь что видео доступно и не ограничено по региону
```

### Ошибка: Timeout

```bash
# Используйте более длинный timeout
curl --max-time 600 ...
# или используйте фоновую обработку /process-video
```
