#!/bin/bash

# Скрипт для быстрого тестирования youtubeRag локально

set -e

API_URL="http://localhost:5055"
TEST_VIDEO="https://www.youtube.com/watch?v=jNQXAC9IVRw"

echo "🚀 youtubeRag Local Testing Script"
echo "=================================="
echo ""

# Проверка что сервер запущен
echo "1️⃣  Проверка health check..."
if curl -s -f $API_URL/health > /dev/null 2>&1; then
    echo "✅ Сервер работает"
else
    echo "❌ Сервер не отвечает. Убедитесь что сервер запущен:"
    echo "   docker-compose up -d"
    echo "   или"
    echo "   python server.py"
    exit 1
fi

echo ""
echo "2️⃣  Тест транскрипции (это займёт 1-3 минуты)..."
TRANSCRIPT_RESPONSE=$(curl -s -X POST $API_URL/transcribe \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\"}")

LANGUAGE=$(echo $TRANSCRIPT_RESPONSE | jq -r '.language')
TEXT_LENGTH=$(echo $TRANSCRIPT_RESPONSE | jq -r '.text | length')

if [ "$LANGUAGE" != "null" ]; then
    echo "✅ Транскрипция успешна"
    echo "   Язык: $LANGUAGE"
    echo "   Длина текста: $TEXT_LENGTH символов"
else
    echo "❌ Ошибка транскрипции"
    echo $TRANSCRIPT_RESPONSE | jq
    exit 1
fi

echo ""
echo "3️⃣  Тест извлечения кадров..."
FRAMES_RESPONSE=$(curl -s -X POST $API_URL/extract-frames \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\", \"interval_sec\": 10}")

FRAMES_COUNT=$(echo $FRAMES_RESPONSE | jq -r '.frames_count')

if [ "$FRAMES_COUNT" != "null" ] && [ "$FRAMES_COUNT" -gt 0 ]; then
    echo "✅ Извлечение кадров успешно"
    echo "   Кадров: $FRAMES_COUNT"
else
    echo "❌ Ошибка извлечения кадров"
    echo $FRAMES_RESPONSE | jq
    exit 1
fi

echo ""
echo "4️⃣  Тест фоновой обработки..."
PROCESS_RESPONSE=$(curl -s -X POST $API_URL/process-video \
  -H "Content-Type: application/json" \
  -d "{\"video_url\": \"$TEST_VIDEO\", \"operations\": [\"transcribe\"]}")

TASK_ID=$(echo $PROCESS_RESPONSE | jq -r '.task_id')

if [ "$TASK_ID" != "null" ]; then
    echo "✅ Задача создана"
    echo "   Task ID: $TASK_ID"

    # Ждём 3 секунды и проверяем статус
    sleep 3
    STATUS_RESPONSE=$(curl -s $API_URL/status/$TASK_ID)
    STATUS=$(echo $STATUS_RESPONSE | jq -r '.status')
    echo "   Статус: $STATUS"
else
    echo "❌ Ошибка создания задачи"
    echo $PROCESS_RESPONSE | jq
    exit 1
fi

echo ""
echo "5️⃣  Тест выполнения Python кода..."
EXEC_RESPONSE=$(curl -s -X POST $API_URL/exec \
  -H "Content-Type: application/json" \
  -d '{"code": "print(\"Hello from youtubeRag!\")"}')

SUCCESS=$(echo $EXEC_RESPONSE | jq -r '.success')
STDOUT=$(echo $EXEC_RESPONSE | jq -r '.stdout')

if [ "$SUCCESS" = "true" ]; then
    echo "✅ Выполнение кода успешно"
    echo "   Output: $STDOUT"
else
    echo "❌ Ошибка выполнения кода"
    echo $EXEC_RESPONSE | jq
    exit 1
fi

echo ""
echo "=================================="
echo "🎉 Все тесты пройдены успешно!"
echo ""
echo "Сервер готов к использованию:"
echo "  - URL: $API_URL"
echo "  - Документация: README.md"
echo "  - Примеры для n8n: n8n_examples.json"
echo "  - Тестовые команды: TEST_COMMANDS.md"
