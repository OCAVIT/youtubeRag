import os
import re
import json
import shutil
import threading
import logging
import subprocess
import tempfile
from functools import wraps
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, request, jsonify
from supabase import create_client, Client

# ====================================
# CONFIGURATION
# ====================================

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Env vars — валидация при старте
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
API_KEY = os.environ.get("FLASK_API_KEY")
ROOT_VIDEOS_DIR = os.environ.get("ROOT_VIDEOS_DIR", "/output/final-videos")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Семафор — максимум 2 параллельных рендера
render_semaphore = threading.Semaphore(2)

# Whisper — грузим один раз (lazy)
_whisper_model = None
_whisper_lock = threading.Lock()


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                logger.info("[Whisper] Loading model...")
                _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
                logger.info("[Whisper] ✅ Model loaded")
    return _whisper_model


# ====================================
# HELPER FUNCTIONS
# ====================================

def parse_json_field(value):
    """Парсит поле которое может быть JSON-строкой или уже dict"""
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def extract_gdrive_file_id(url: str):
    """Извлекает file_id из Google Drive URL. Возвращает None если не GDrive."""
    # https://drive.google.com/file/d/FILE_ID/view?usp=sharing
    # https://drive.google.com/open?id=FILE_ID
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    return None


def download_file(url: str, save_path: str) -> str:
    """Скачивает файл. Поддерживает Google Drive и обычные URL."""
    logger.info(f"[Download] {url}")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    gdrive_id = extract_gdrive_file_id(url)

    if gdrive_id:
        # Google Drive — прямая скачка + обработка подтверждения для больших файлов
        download_url = f"https://drive.google.com/uc?export=download&id={gdrive_id}"
        session = requests.Session()
        resp = session.get(download_url, stream=True, timeout=60)
        resp.raise_for_status()

        # Если Google просит подтверждение (большой файл) — достаём confirm token
        if 'text/html' in resp.headers.get('Content-Type', ''):
            confirm_match = re.search(r'confirm=([0-9A-Za-z_-]+)', resp.text)
            if confirm_match:
                download_url += f"&confirm={confirm_match.group(1)}"
                resp = session.get(download_url, stream=True, timeout=60)
                resp.raise_for_status()

        with open(save_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
    else:
        # Обычный URL (unsplash, S3, и т.д.)
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        with open(save_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    file_size = os.path.getsize(save_path)
    logger.info(f"[Download] ✅ Saved {save_path} ({file_size} bytes)")
    return save_path


def run_ffmpeg(cmd: list[str], step_name: str):
    """Запускает FFmpeg/FFprobe, логирует stderr при ошибке"""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"[{step_name}] FFmpeg stderr:\n{result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def get_audio_duration(audio_path: str) -> float:
    """Получает длительность аудио через FFprobe"""
    cmd = [
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        audio_path
    ]
    result = run_ffmpeg(cmd, "FFprobe")
    return float(result.stdout.strip())


def create_looped_video_with_audio(image_path: str, audio_path: str, output_path: str, duration: float) -> str:
    """Создаёт видео: зацикленная картинка + аудио"""
    logger.info(f"[Video] Creating video ({duration:.1f}s)")
    cmd = [
        'ffmpeg', '-y',
        '-loop', '1', '-i', image_path,
        '-i', audio_path,
        '-c:v', 'libx264', '-tune', 'stillimage',
        '-c:a', 'aac', '-b:a', '192k',
        '-pix_fmt', 'yuv420p', '-r', '24',
        '-t', str(duration), '-shortest',
        output_path
    ]
    run_ffmpeg(cmd, "Video")
    logger.info(f"[Video] ✅ {output_path}")
    return output_path


def format_srt_time(seconds: float) -> str:
    """00:01:23,456"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_subtitles_srt(audio_path: str, output_srt_path: str) -> bool:
    """Генерирует SRT через Faster Whisper"""
    logger.info(f"[Whisper] Transcribing...")
    try:
        model = get_whisper_model()
        segments, _ = model.transcribe(audio_path, language="en")

        srt_lines = []
        for i, seg in enumerate(segments, start=1):
            srt_lines.append(
                f"{i}\n{format_srt_time(seg.start)} --> {format_srt_time(seg.end)}\n{seg.text.strip()}\n"
            )

        os.makedirs(os.path.dirname(output_srt_path), exist_ok=True)
        with open(output_srt_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(srt_lines))

        logger.info(f"[Whisper] ✅ {output_srt_path}")
        return True
    except Exception as e:
        logger.error(f"[Whisper] Failed: {e}", exc_info=True)
        return False


def add_subtitles_to_video(video_path: str, srt_path: str, output_path: str) -> str:
    """Вшивает субтитры в видео (burn-in)"""
    # Экранирование для ffmpeg subtitles filter
    escaped_srt = srt_path.replace('\\', '/').replace(':', '\\:')
    subtitles_filter = (
        f"subtitles={escaped_srt}:force_style='"
        "FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=3,Outline=2,Shadow=0,"
        "MarginV=50,Alignment=2'"
    )
    cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-vf', subtitles_filter,
        '-c:a', 'copy', output_path
    ]
    run_ffmpeg(cmd, "Subtitles")
    logger.info(f"[Subs] ✅ {output_path}")
    return output_path


def concatenate_videos(video_paths: list[str], output_path: str) -> str:
    """Склеивает видео через concat demuxer"""
    logger.info(f"[Concat] Merging {len(video_paths)} videos...")

    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        for vp in video_paths:
            f.write(f"file '{vp}'\n")
        concat_list = f.name

    try:
        cmd = [
            'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
            '-i', concat_list, '-c', 'copy', output_path
        ]
        run_ffmpeg(cmd, "Concat")
        logger.info(f"[Concat] ✅ {output_path}")
        return output_path
    finally:
        os.unlink(concat_list)


# ====================================
# MAIN RENDER FUNCTION
# ====================================

def render_chapter_task(chapter_id: str):
    """
    Фоновая задача: рендер одной главы.
    1. Берёт данные главы и её script_blocks из Supabase
    2. Для каждого блока: скачивает картинку+аудио → видео → субтитры
    3. Склеивает все блоки в один chapter_{number}.mp4
    """

    logger.info(f"\n{'='*60}")
    logger.info(f"[RENDER] Starting chapter {chapter_id}")
    logger.info(f"{'='*60}\n")

    # ── Получаем данные главы ──
    try:
        chapter_resp = supabase.table("chapters") \
            .select("*") \
            .eq("id", chapter_id) \
            .single() \
            .execute()
        chapter = chapter_resp.data
    except Exception as e:
        logger.error(f"[DB] Failed to fetch chapter: {e}")
        return

    project_id = chapter['project_id']
    chapter_number = chapter['chapter_number']

    logger.info(f"[RENDER] Project: {project_id}, Chapter #{chapter_number}")

    # ── Ставим статус "rendering" ──
    try:
        supabase.table("chapters").update({
            "status": "rendering",
            "updated_at": datetime.utcnow().isoformat()
        }).eq("id", chapter_id).execute()
    except Exception as e:
        logger.error(f"[DB] Failed to set rendering status: {e}")
        return

    # ── Семафор ──
    acquired = render_semaphore.acquire(timeout=300)
    if not acquired:
        logger.error("[RENDER] Semaphore timeout — too many concurrent renders")
        supabase.table("chapters").update({
            "status": "failed", "updated_at": datetime.utcnow().isoformat()
        }).eq("id", chapter_id).execute()
        return

    # Пути
    project_dir = Path(ROOT_VIDEOS_DIR) / f"project_{project_id}"
    chapter_dir = project_dir / f"chapter_{chapter_number}"
    temp_dir = chapter_dir / "temp_scripts"

    try:
        # ========================================
        # STEP 1: Загрузка script_blocks из Supabase
        # ========================================

        logger.info("[DB] Fetching script_blocks...")
        resp = supabase.table("script_blocks") \
            .select("*") \
            .eq("chapter_id", chapter_id) \
            .order("sequence_number") \
            .execute()

        blocks = resp.data
        if not blocks:
            raise ValueError(f"No script_blocks for chapter {chapter_id}")

        logger.info(f"[DB] ✅ Loaded {len(blocks)} blocks")

        # ========================================
        # STEP 2: Обработка каждого блока
        # ========================================

        processed_videos = []

        for block in blocks:
            seq = block['sequence_number']
            assets = parse_json_field(block.get('assets'))

            logger.info(f"\n[Block {seq}] Processing...")

            audio_url = assets.get('audio_url')
            image_url = assets.get('image_url')

            if not audio_url or not image_url:
                logger.error(f"[Block {seq}] ❌ Missing audio_url or image_url — skipping")
                continue

            # Папка блока: temp_scripts/block_{seq}
            block_dir = temp_dir / f"block_{seq}"
            block_dir.mkdir(parents=True, exist_ok=True)

            # ── Скачиваем файлы ──
            audio_path = str(block_dir / "audio.wav")
            image_path = str(block_dir / "image.png")

            download_file(audio_url, audio_path)
            download_file(image_url, image_path)

            # ── Определяем длительность аудио ──
            duration = get_audio_duration(audio_path)
            logger.info(f"[Block {seq}] Audio duration: {duration:.1f}s")

            # ── Создаём видео (картинка + аудио) ──
            raw_video = str(block_dir / "raw.mp4")
            create_looped_video_with_audio(image_path, audio_path, raw_video, duration)

            # ── Субтитры через Whisper ──
            srt_path = str(block_dir / "subs.srt")
            subs_ok = generate_subtitles_srt(audio_path, srt_path)

            # ── Вшиваем субтитры (или берём без них) ──
            if subs_ok and os.path.exists(srt_path):
                final_block_video = str(block_dir / "final.mp4")
                add_subtitles_to_video(raw_video, srt_path, final_block_video)
                processed_videos.append(final_block_video)
            else:
                processed_videos.append(raw_video)

            logger.info(f"[Block {seq}] ✅ Done")

        if not processed_videos:
            raise ValueError("No blocks were processed successfully")

        # ========================================
        # STEP 3: Склейка всех блоков в главу
        # ========================================

        final_path = str(project_dir / f"chapter_{chapter_number}.mp4")
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        concatenate_videos(processed_videos, final_path)

        # ========================================
        # STEP 4: Обновление статуса
        # ========================================

        supabase.table("chapters").update({
            "status": "rendered",
            "video_url": final_path,
            "updated_at": datetime.utcnow().isoformat()
        }).eq("id", chapter_id).execute()

        logger.info(f"\n{'='*60}")
        logger.info(f"[RENDER] ✅ SUCCESS! → {final_path}")
        logger.info(f"{'='*60}\n")

    except Exception as e:
        logger.error(f"[RENDER] ❌ ERROR: {e}", exc_info=True)
        try:
            supabase.table("chapters").update({
                "status": "failed", "updated_at": datetime.utcnow().isoformat()
            }).eq("id", chapter_id).execute()
        except Exception:
            logger.error("[DB] Failed to update status to 'failed'", exc_info=True)

    finally:
        render_semaphore.release()

        # Чистим temp_scripts
        if temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
                logger.info(f"[Cleanup] ✅ Removed {temp_dir}")
            except Exception as e:
                logger.warning(f"[Cleanup] Failed: {e}")


# ====================================
# FLASK ROUTES
# ====================================

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization')
        if not auth or auth != f"Bearer {API_KEY}":
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


@app.route('/render-chapter', methods=['POST'])
@require_api_key
def render_chapter():
    """
    n8n шлёт POST с chapter_id.

    Body: { "chapter_id": "uuid" }
    """
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "Invalid or missing JSON body"}), 400

        chapter_id = data.get('chapter_id')
        if not chapter_id:
            return jsonify({"error": "Missing chapter_id"}), 400

        # Фоновый рендер
        thread = threading.Thread(
            target=render_chapter_task,
            args=(chapter_id,),
            daemon=True
        )
        thread.start()

        logger.info(f"[API] Render started for chapter {chapter_id}")

        return jsonify({
            "status": "accepted",
            "message": "Render task started",
            "chapter_id": chapter_id
        }), 202

    except Exception as e:
        logger.error(f"[API] Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy1"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    logger.info(f"Запуск Flask dev-сервера на порту {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
