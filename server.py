"""
youtubeRag - HTTP сервер для обработки видео
Поддерживает транскрипцию, извлечение кадров и выполнение Python кода
"""

import os
import sys
import time
import uuid
import json
import logging
import threading
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path
from io import StringIO
import signal

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from faster_whisper import WhisperModel
import ffmpeg
from PIL import Image
import yt_dlp

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Инициализация Flask приложения
app = Flask(__name__)
CORS(app)

# Директории
BASE_DIR = Path("/app")
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR = BASE_DIR / "temp"
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"

# Создание директорий если не существуют
for directory in [INPUT_DIR, OUTPUT_DIR, TEMP_DIR, TRANSCRIPTS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# Хранилище задач
tasks: Dict[str, Dict[str, Any]] = {}
tasks_lock = threading.Lock()

# Whisper модель (загружается lazy)
whisper_model: Optional[WhisperModel] = None
whisper_lock = threading.Lock()


def get_whisper_model() -> WhisperModel:
    """Получение Whisper модели (lazy loading)."""
    global whisper_model
    with whisper_lock:
        if whisper_model is None:
            logger.info("Загрузка Whisper модели...")
            whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
            logger.info("Whisper модель загружена")
        return whisper_model


def download_video(url: str, output_path: Optional[Path] = None) -> Path:
    """
    Скачивание видео по URL через yt-dlp.

    Args:
        url: URL видео
        output_path: Путь для сохранения (если None, генерируется автоматически)

    Returns:
        Path: Путь к скачанному файлу
    """
    if output_path is None:
        task_id = str(uuid.uuid4())
        output_path = TEMP_DIR / f"{task_id}.mp4"

    logger.info(f"Скачивание видео: {url}")

    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'outtmpl': str(output_path),
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        logger.info(f"Видео скачано: {output_path}")
        return output_path
    except Exception as e:
        logger.error(f"Ошибка при скачивании видео: {e}")
        raise


def cleanup_old_temp_files(max_age_hours: int = 1) -> None:
    """
    Очистка временных файлов старше указанного времени.

    Args:
        max_age_hours: Максимальный возраст файлов в часах
    """
    try:
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
        removed_count = 0

        for file_path in TEMP_DIR.iterdir():
            if file_path.is_file():
                file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
                if file_time < cutoff_time:
                    file_path.unlink()
                    removed_count += 1
                    logger.debug(f"Удалён старый файл: {file_path}")

        if removed_count > 0:
            logger.info(f"Очищено {removed_count} старых временных файлов")
    except Exception as e:
        logger.error(f"Ошибка при очистке временных файлов: {e}")


def transcribe_video(video_path: Path) -> Dict[str, Any]:
    """
    Транскрипция видео с помощью faster-whisper.

    Args:
        video_path: Путь к видео файлу

    Returns:
        Dict с текстом, сегментами и языком
    """
    logger.info(f"Начало транскрипции: {video_path}")

    model = get_whisper_model()
    segments, info = model.transcribe(str(video_path), beam_size=5)

    full_text = []
    segment_list = []

    for segment in segments:
        full_text.append(segment.text)
        segment_list.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip()
        })

    result = {
        "text": " ".join(full_text).strip(),
        "segments": segment_list,
        "language": info.language
    }

    logger.info(f"Транскрипция завершена. Язык: {info.language}, сегментов: {len(segment_list)}")
    return result


def extract_frames(video_path: Path, interval_sec: float = 5.0) -> Dict[str, Any]:
    """
    Извлечение кадров из видео.

    Args:
        video_path: Путь к видео файлу
        interval_sec: Интервал между кадрами в секундах

    Returns:
        Dict с информацией о кадрах
    """
    task_id = str(uuid.uuid4())
    frames_dir = OUTPUT_DIR / "frames" / task_id
    frames_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Извлечение кадров: {video_path}, интервал: {interval_sec}s")

    try:
        # Получение информации о видео
        probe = ffmpeg.probe(str(video_path))
        video_info = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        duration = float(probe['format']['duration'])

        # Извлечение кадров
        frame_count = 0
        for timestamp in range(0, int(duration), int(interval_sec)):
            output_file = frames_dir / f"frame_{timestamp:06d}.jpg"

            (
                ffmpeg
                .input(str(video_path), ss=timestamp)
                .filter('scale', 1280, -1)
                .output(str(output_file), vframes=1, format='image2', vcodec='mjpeg')
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True, quiet=True)
            )

            frame_count += 1

        result = {
            "task_id": task_id,
            "frames_count": frame_count,
            "frames_dir": str(frames_dir),
            "interval_sec": interval_sec,
            "video_duration": duration
        }

        logger.info(f"Извлечено кадров: {frame_count}")
        return result

    except Exception as e:
        logger.error(f"Ошибка при извлечении кадров: {e}")
        raise


def process_video_background(task_id: str, video_url: str, operations: List[str]) -> None:
    """
    Фоновая обработка видео.

    Args:
        task_id: ID задачи
        video_url: URL видео
        operations: Список операций для выполнения
    """
    try:
        with tasks_lock:
            tasks[task_id]["status"] = "processing"

        # Скачивание видео
        video_path = download_video(video_url)

        result = {}

        # Выполнение операций
        if "transcribe" in operations:
            logger.info(f"Задача {task_id}: транскрипция")
            transcript = transcribe_video(video_path)
            result["transcription"] = transcript

            # Сохранение транскрипции
            transcript_file = TRANSCRIPTS_DIR / f"{task_id}.json"
            with open(transcript_file, 'w', encoding='utf-8') as f:
                json.dump(transcript, f, ensure_ascii=False, indent=2)

        if "extract_frames" in operations:
            logger.info(f"Задача {task_id}: извлечение кадров")
            frames_info = extract_frames(video_path)
            result["frames"] = frames_info

        # Очистка временного файла
        if video_path.exists():
            video_path.unlink()

        with tasks_lock:
            tasks[task_id]["status"] = "completed"
            tasks[task_id]["result"] = result
            tasks[task_id]["completed_at"] = datetime.now().isoformat()

        logger.info(f"Задача {task_id} завершена успешно")

    except Exception as e:
        logger.error(f"Ошибка в задаче {task_id}: {e}")
        logger.error(traceback.format_exc())

        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"] = str(e)
            tasks[task_id]["traceback"] = traceback.format_exc()


@app.before_request
def log_request():
    """Логирование каждого запроса."""
    logger.info(f"{request.method} {request.path} - {request.remote_addr}")


@app.errorhandler(Exception)
def handle_error(error):
    """Обработчик всех ошибок."""
    logger.error(f"Ошибка: {error}")
    logger.error(traceback.format_exc())

    return jsonify({
        "error": str(error),
        "type": type(error).__name__
    }), 500


@app.route('/health', methods=['GET'])
def health():
    """Проверка здоровья сервера."""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


@app.route('/transcribe', methods=['POST'])
def transcribe():
    """
    Транскрипция видео.

    Body JSON:
        {
            "video_url": "https://...", // опционально
            "file_path": "/app/input/video.mp4" // опционально
        }
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({"error": "JSON body required"}), 400

        # Определение источника видео
        if "video_url" in data:
            video_path = download_video(data["video_url"])
            cleanup_after = True
        elif "file_path" in data:
            video_path = Path(data["file_path"])
            if not video_path.exists():
                return jsonify({"error": f"File not found: {video_path}"}), 404
            cleanup_after = False
        else:
            return jsonify({"error": "Either 'video_url' or 'file_path' required"}), 400

        # Транскрипция
        result = transcribe_video(video_path)

        # Очистка временного файла
        if cleanup_after and video_path.exists():
            video_path.unlink()

        # Очистка старых файлов
        cleanup_old_temp_files()

        return jsonify(result)

    except Exception as e:
        logger.error(f"Ошибка в /transcribe: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/extract-frames', methods=['POST'])
def extract_frames_endpoint():
    """
    Извлечение кадров из видео.

    Body JSON:
        {
            "video_url": "https://...",
            "interval_sec": 5 // опционально, по умолчанию 5
        }
    """
    try:
        data = request.get_json()

        if not data or "video_url" not in data:
            return jsonify({"error": "'video_url' required"}), 400

        interval_sec = data.get("interval_sec", 5.0)

        # Скачивание видео
        video_path = download_video(data["video_url"])

        # Извлечение кадров
        result = extract_frames(video_path, interval_sec)

        # Очистка временного файла
        if video_path.exists():
            video_path.unlink()

        # Очистка старых файлов
        cleanup_old_temp_files()

        return jsonify(result)

    except Exception as e:
        logger.error(f"Ошибка в /extract-frames: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/process-video', methods=['POST'])
def process_video():
    """
    Фоновая обработка видео.

    Body JSON:
        {
            "video_url": "https://...",
            "operations": ["transcribe", "extract_frames"]
        }
    """
    try:
        data = request.get_json()

        if not data or "video_url" not in data or "operations" not in data:
            return jsonify({"error": "'video_url' and 'operations' required"}), 400

        task_id = str(uuid.uuid4())

        # Создание задачи
        with tasks_lock:
            tasks[task_id] = {
                "task_id": task_id,
                "status": "pending",
                "video_url": data["video_url"],
                "operations": data["operations"],
                "created_at": datetime.now().isoformat()
            }

        # Запуск фоновой обработки
        thread = threading.Thread(
            target=process_video_background,
            args=(task_id, data["video_url"], data["operations"])
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            "task_id": task_id,
            "status": "processing"
        })

    except Exception as e:
        logger.error(f"Ошибка в /process-video: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/status/<task_id>', methods=['GET'])
def get_status(task_id: str):
    """Получение статуса задачи."""
    with tasks_lock:
        if task_id not in tasks:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(tasks[task_id])


@app.route('/exec', methods=['POST'])
def exec_code():
    """
    Выполнение Python кода.

    Body JSON:
        {
            "code": "print(1+1)"
        }
    """
    try:
        data = request.get_json()

        if not data or "code" not in data:
            return jsonify({"error": "'code' required"}), 400

        code = data["code"]

        # Захват stdout и stderr
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        stdout_capture = StringIO()
        stderr_capture = StringIO()

        success = True
        error_msg = None

        try:
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture

            # Выполнение кода с таймаутом через threading
            def execute():
                exec(code, {"__builtins__": __builtins__})

            thread = threading.Thread(target=execute)
            thread.daemon = True
            thread.start()
            thread.join(timeout=30)

            if thread.is_alive():
                raise TimeoutError("Code execution timeout (30s)")

        except Exception as e:
            success = False
            error_msg = str(e)
            stderr_capture.write(traceback.format_exc())
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        return jsonify({
            "success": success,
            "stdout": stdout_capture.getvalue(),
            "stderr": stderr_capture.getvalue(),
            "error": error_msg
        })

    except Exception as e:
        logger.error(f"Ошибка в /exec: {e}")
        return jsonify({"error": str(e)}), 500


# Периодическая очистка временных файлов
def periodic_cleanup():
    """Периодическая очистка временных файлов."""
    while True:
        time.sleep(3600)  # Каждый час
        cleanup_old_temp_files()


# Запуск фонового потока очистки
cleanup_thread = threading.Thread(target=periodic_cleanup)
cleanup_thread.daemon = True
cleanup_thread.start()


if __name__ == '__main__':
    # Получение порта из переменной окружения (для Railway)
    port = int(os.environ.get('PORT', 5055))

    logger.info(f"Запуск сервера на порту {port}")
    logger.info(f"Директории: INPUT={INPUT_DIR}, OUTPUT={OUTPUT_DIR}, TEMP={TEMP_DIR}")

    # Для локальной разработки используем встроенный сервер Flask
    # В продакшене используется Gunicorn (см. Dockerfile)
    app.run(host='0.0.0.0', port=port, debug=False)
