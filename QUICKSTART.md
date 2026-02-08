# ⚡ Быстрый старт youtubeRag

Минимальная инструкция для запуска за 5 минут.

## 🚀 Деплой на Railway (рекомендуется)

```bash
# 1. Создайте репозиторий
git init
git add .
git commit -m "Initial commit"
git remote add origin <your-repo-url>
git push -u origin main

# 2. Перейдите на railway.app
# 3. New Project → Deploy from GitHub repo
# 4. Выберите ваш репозиторий
# 5. Скопируйте URL после деплоя
```

**Готово!** Ваш сервер работает на Railway.

## 🐳 Локальный запуск (Docker)

```bash
# Запуск
docker-compose up -d --build

# Проверка
curl http://localhost:5055/health

# Тест
./test_local.sh

# Остановка
docker-compose down
```

## 💻 Локальный запуск (без Docker)

```bash
# Установка зависимостей
pip install -r requirements.txt

# Установка ffmpeg
# Ubuntu: sudo apt-get install ffmpeg
# Mac: brew install ffmpeg
# Windows: скачайте с ffmpeg.org

# Запуск
python server.py
```

## 🧪 Быстрый тест

```bash
# Замените на ваш URL
export API_URL="https://your-url.up.railway.app"
# или для локального
export API_URL="http://localhost:5055"

# Health check
curl $API_URL/health

# Транскрипция (займёт 1-3 минуты)
curl -X POST $API_URL/transcribe \
  -H "Content-Type: application/json" \
  -d '{"video_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}'
```

## 🔗 Интеграция с n8n

1. Создайте HTTP Request ноду в n8n
2. Настройте:
   - **Method**: POST
   - **URL**: `https://your-url.up.railway.app/transcribe`
   - **Body**: `{"video_url": "{{ $json.url }}"}`
3. Запустите workflow

**Готово!** Транскрипция работает в n8n.

## 📚 Больше информации

- **Полная документация**: [README.md](README.md)
- **Инструкция по деплою**: [DEPLOY.md](DEPLOY.md)
- **Примеры команд**: [TEST_COMMANDS.md](TEST_COMMANDS.md)
- **Примеры для n8n**: [n8n_examples.json](n8n_examples.json)
- **Checklist**: [CHECKLIST.md](CHECKLIST.md)

## 🆘 Проблемы?

### Сервер не запускается

```bash
# Проверьте логи
docker-compose logs -f
```

### Railway деплой падает

1. Проверьте логи в Railway Dashboard
2. Убедитесь что все файлы закоммичены
3. Проверьте что Dockerfile корректный

### Ошибка при транскрипции

- Убедитесь что URL видео корректный
- Попробуйте другое видео
- Проверьте логи

## 💡 Полезные команды

```bash
# Локально
docker-compose up -d         # Запуск
docker-compose logs -f       # Логи
docker-compose down          # Остановка

# Railway
git push origin main         # Автоматический деплой
# Логи: Railway Dashboard → Deployments → Logs

# Тестирование
./test_local.sh              # Автотест всех эндпоинтов
curl $API_URL/health         # Проверка работы
```

## 🎉 Готово!

Ваш youtubeRag сервер работает и готов к использованию с n8n!
