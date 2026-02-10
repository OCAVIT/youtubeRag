"""
youtubeRag - HTTP сервер для обработки видео.
Поддерживает транскрипцию, извлечение кадров и выполнение Python кода.
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

from flask import Flask, request, jsonify
from flask_cors import CORS

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("youtuberag")

# Инициализация Flask
app = Flask(__name__)
CORS(app)

# Директории
BASE_DIR = Path(os.environ.get("APP_DIR", "/app"))
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR = BASE_DIR / "temp"
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"

for d in [INPUT_DIR, OUTPUT_DIR, TEMP_DIR, TRANSCRIPTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Хранилище фоновых задач
tasks: Dict[str, Dict[str, Any]] = {}
tasks_lock = threading.Lock()

# Whisper модель (lazy)
_whisper_model = None
_whisper_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def get_whisper_model():
    """Lazy-загрузка Whisper модели."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            logger.info("Загрузка Whisper модели (base, cpu, int8)...")
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
            logger.info("Whisper модель загружена")
        return _whisper_model


def download_video(url: str, output_path: Optional[Path] = None) -> Path:
    """Скачивание видео по URL через yt-dlp."""
    import yt_dlp

    if output_path is None:
        output_path = TEMP_DIR / f"{uuid.uuid4()}.mp4"

    logger.info(f"Скачивание видео: {url}")

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": str(output_path),
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    logger.info(f"Видео скачано: {output_path}")
    return output_path


def cleanup_old_temp_files(max_age_hours: int = 1) -> None:
    """Удаление временных файлов старше max_age_hours."""
    try:
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        removed = 0
        for f in TEMP_DIR.iterdir():
            if f.is_file() and f.name != ".gitkeep":
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink()
                    removed += 1
        if removed:
            logger.info(f"Очищено {removed} старых временных файлов")
    except Exception as e:
        logger.error(f"Ошибка при очистке: {e}")


def transcribe_video(video_path: Path) -> Dict[str, Any]:
    """Транскрипция видео с помощью faster-whisper."""
    logger.info(f"Транскрипция: {video_path}")

    model = get_whisper_model()
    segments_gen, info = model.transcribe(str(video_path), beam_size=5)

    full_text: List[str] = []
    segments: List[Dict[str, Any]] = []

    for seg in segments_gen:
        full_text.append(seg.text)
        segments.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
        })

    result = {
        "text": " ".join(full_text).strip(),
        "segments": segments,
        "language": info.language,
    }
    logger.info(f"Транскрипция завершена. Язык: {info.language}, сегментов: {len(segments)}")
    return result


def extract_frames(video_path: Path, interval_sec: float = 5.0) -> Dict[str, Any]:
    """Извлечение кадров из видео через ffmpeg."""
    import ffmpeg

    task_id = str(uuid.uuid4())
    frames_dir = OUTPUT_DIR / "frames" / task_id
    frames_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Извлечение кадров: {video_path}, интервал: {interval_sec}s")

    probe = ffmpeg.probe(str(video_path))
    duration = float(probe["format"]["duration"])

    frame_count = 0
    for ts in range(0, int(duration), int(interval_sec)):
        out_file = frames_dir / f"frame_{ts:06d}.jpg"
        (
            ffmpeg
            .input(str(video_path), ss=ts)
            .filter("scale", 1280, -1)
            .output(str(out_file), vframes=1, format="image2", vcodec="mjpeg")
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True, quiet=True)
        )
        frame_count += 1

    result = {
        "task_id": task_id,
        "frames_count": frame_count,
        "frames_dir": str(frames_dir),
        "interval_sec": interval_sec,
        "video_duration": duration,
    }
    logger.info(f"Извлечено кадров: {frame_count}")
    return result


def process_video_background(task_id: str, video_url: str, operations: List[str]) -> None:
    """Фоновая обработка видео."""
    try:
        with tasks_lock:
            tasks[task_id]["status"] = "processing"

        video_path = download_video(video_url)
        result: Dict[str, Any] = {}

        if "transcribe" in operations:
            transcript = transcribe_video(video_path)
            result["transcription"] = transcript
            transcript_file = TRANSCRIPTS_DIR / f"{task_id}.json"
            transcript_file.write_text(
                json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        if "extract_frames" in operations:
            result["frames"] = extract_frames(video_path)

        if video_path.exists():
            video_path.unlink()

        with tasks_lock:
            tasks[task_id]["status"] = "completed"
            tasks[task_id]["result"] = result
            tasks[task_id]["completed_at"] = datetime.now().isoformat()

        logger.info(f"Задача {task_id} завершена")

    except Exception as e:
        logger.error(f"Ошибка в задаче {task_id}: {e}\n{traceback.format_exc()}")
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"] = str(e)


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------

@app.before_request
def log_request():
    """Логирование каждого входящего запроса."""
    logger.info(f"{request.method} {request.path} - {request.remote_addr}")


@app.errorhandler(Exception)
def handle_error(error):
    """Глобальный обработчик ошибок — всегда JSON."""
    logger.error(f"Unhandled: {error}\n{traceback.format_exc()}")
    return jsonify({"error": str(error), "type": type(error).__name__}), 500


@app.route("/health", methods=["GET"])
def health():
    """Health check."""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


@app.route("/transcribe", methods=["POST"])
def transcribe_endpoint():
    """Транскрипция видео (синхронная)."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    cleanup_after = False
    if "video_url" in data:
        video_path = download_video(data["video_url"])
        cleanup_after = True
    elif "file_path" in data:
        video_path = Path(data["file_path"])
        if not video_path.exists():
            return jsonify({"error": f"File not found: {video_path}"}), 404
    else:
        return jsonify({"error": "Either 'video_url' or 'file_path' required"}), 400

    try:
        result = transcribe_video(video_path)
    finally:
        if cleanup_after and video_path.exists():
            video_path.unlink()
        cleanup_old_temp_files()

    return jsonify(result)


@app.route("/extract-frames", methods=["POST"])
def extract_frames_endpoint():
    """Извлечение кадров из видео."""
    data = request.get_json(silent=True)
    if not data or "video_url" not in data:
        return jsonify({"error": "'video_url' required"}), 400

    interval_sec = data.get("interval_sec", 5.0)
    video_path = download_video(data["video_url"])

    try:
        result = extract_frames(video_path, interval_sec)
    finally:
        if video_path.exists():
            video_path.unlink()
        cleanup_old_temp_files()

    return jsonify(result)


@app.route("/process-video", methods=["POST"])
def process_video():
    """Фоновая обработка видео."""
    data = request.get_json(silent=True)
    if not data or "video_url" not in data or "operations" not in data:
        return jsonify({"error": "'video_url' and 'operations' required"}), 400

    task_id = str(uuid.uuid4())
    with tasks_lock:
        tasks[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "video_url": data["video_url"],
            "operations": data["operations"],
            "created_at": datetime.now().isoformat(),
        }

    t = threading.Thread(
        target=process_video_background,
        args=(task_id, data["video_url"], data["operations"]),
        daemon=True,
    )
    t.start()

    return jsonify({"task_id": task_id, "status": "processing"})


@app.route("/status/<task_id>", methods=["GET"])
def get_status(task_id: str):
    """Получение статуса фоновой задачи."""
    with tasks_lock:
        if task_id not in tasks:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(tasks[task_id])


@app.route("/exec", methods=["POST"])
def exec_code():
    """Выполнение Python кода (таймаут 30 с)."""
    data = request.get_json(silent=True)
    if not data or "code" not in data:
        return jsonify({"error": "'code' required"}), 400

    code = data["code"]
    stdout_capture = StringIO()
    stderr_capture = StringIO()
    success = True
    error_msg = None

    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = stdout_capture
        sys.stderr = stderr_capture

        exec_result = {}

        def _run():
            try:
                exec(code, {"__builtins__": __builtins__}, exec_result)
            except Exception as exc:
                exec_result["__error__"] = exc

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=30)

        if t.is_alive():
            success = False
            error_msg = "Code execution timeout (30s)"
        elif "__error__" in exec_result:
            success = False
            error_msg = str(exec_result["__error__"])

    except Exception as e:
        success = False
        error_msg = str(e)
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    return jsonify({
        "success": success,
        "stdout": stdout_capture.getvalue(),
        "stderr": stderr_capture.getvalue(),
        "error": error_msg,
    })


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    logger.info(f"Запуск Flask dev-сервера на порту {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
