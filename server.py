import os
import re
import json
import uuid
import shutil
import threading
import logging
import subprocess
import tempfile
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path

import requests
import yadisk
from flask import Flask, request, jsonify
from supabase import create_client, Client

# ====================================
# CONFIGURATION
# ====================================

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Env vars — валидация при старте
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
API_KEY = os.environ.get("FLASK_API_KEY")
ROOT_VIDEOS_DIR = os.environ.get("ROOT_VIDEOS_DIR", "/output/final-videos")
# Яндекс.Диск
YANDEX_DISK_TOKEN = os.environ.get("YANDEX_DISK_TOKEN")
YANDEX_DISK_ROOT_FOLDER = "/YouTubeRAG"

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
# YANDEX DISK MANAGER
# ====================================

class YandexDiskManager:
    """Менеджер для загрузки финальных видео на Яндекс.Диск"""

    def __init__(self, token: str):
        self.client = yadisk.YaDisk(token=token)
        self.root_folder = YANDEX_DISK_ROOT_FOLDER

    def check_connection(self) -> bool:
        """Проверка валидности токена"""
        try:
            return self.client.check_token()
        except Exception as e:
            logger.error(f"[YaDisk] Token check failed: {e}")
            return False

    def _ensure_folder(self, remote_path: str):
        """Рекурсивно создаёт папки на Яндекс.Диске если не существуют"""
        parts = remote_path.strip("/").split("/")
        current = ""
        for part in parts:
            current += f"/{part}"
            try:
                if not self.client.exists(current):
                    self.client.mkdir(current)
                    logger.info(f"[YaDisk] Created folder: {current}")
            except Exception as e:
                logger.error(f"[YaDisk] Failed to create folder {current}: {e}")
                raise

    def upload_file(self, local_path: str, remote_relative_path: str) -> tuple[bool, str]:
        """
        Загружает файл на Яндекс.Диск.

        Args:
            local_path: Абсолютный путь к локальному файлу
            remote_relative_path: Относительный путь внутри root_folder
                (например: project_xxx/chapter_1.mp4)

        Returns:
            (success, public_url_or_error)
        """
        remote_path = f"{self.root_folder}/{remote_relative_path}"
        remote_dir = "/".join(remote_path.split("/")[:-1])

        try:
            # Создаём структуру папок
            self._ensure_folder(remote_dir)

            # Загружаем файл
            logger.info(f"[YaDisk] Uploading {local_path} → {remote_path}")
            self.client.upload(local_path, remote_path, overwrite=True)
            logger.info(f"[YaDisk] ✅ Upload complete: {remote_path}")

            # Публикуем и получаем ссылку
            self.client.publish(remote_path)
            meta = self.client.get_meta(remote_path)
            public_url = meta.public_url or ""

            logger.info(f"[YaDisk] ✅ Public URL: {public_url}")
            return True, public_url

        except Exception as e:
            logger.error(f"[YaDisk] ❌ Upload failed: {e}", exc_info=True)
            return False, str(e)


# ====================================
# JOB TRACKING
# ====================================

_jobs: dict[str, dict] = {}  # job_id → job info
_jobs_lock = threading.Lock()


def create_job(chapter_id: str) -> str:
    """Создаёт новую задачу рендера, возвращает job_id"""
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "chapter_id": chapter_id,
            "status": "queued",
            "stage": "Waiting in queue",
            "completed": False,
            "video_url": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    logger.info(f"[Job] Created job {job_id} for chapter {chapter_id}")
    return job_id


def update_job(job_id: str, **kwargs):
    """Обновляет поля задачи"""
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)
            _jobs[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()


def get_job(job_id: str) -> dict | None:
    """Возвращает копию данных задачи"""
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


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
    """Вшивает субтитры в видео (burn-in).
    Использует фильтр 'subtitles' (libass). Если libass недоступен —
    парсит SRT и вшивает через 'drawtext' (всегда встроен в FFmpeg).
    """
    video_abs = os.path.abspath(video_path)
    srt_abs = os.path.abspath(srt_path)
    output_abs = os.path.abspath(output_path)

    if not os.path.exists(srt_abs):
        raise FileNotFoundError(f"Subtitle file not found: {srt_abs}")

    logger.info(f"[Subs] Video: {video_abs}")
    logger.info(f"[Subs] SRT: {srt_abs}")

    # ── Сначала пробуем libass (subtitles filter) ──
    if _has_libass_support():
        # FFmpeg subtitles filter (libass) требует экранирования
        # спецсимволов в пути: \ → /, : → \:, ' → \'
        srt_escaped = srt_abs.replace('\\', '/').replace(':', '\\:').replace("'", "\\'")

        subtitles_filter = (
            f"subtitles='{srt_escaped}':force_style='"
            "FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=3,Outline=2,Shadow=0,"
            "MarginV=50,Alignment=2'"
        )

        cmd = [
            'ffmpeg', '-y',
            '-i', video_abs,
            '-vf', subtitles_filter,
            '-c:a', 'copy', output_abs
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            logger.info(f"[Subs] ✅ {output_abs} (libass)")
            return output_path
        else:
            logger.warning(f"[Subs] libass subtitles filter failed (code {result.returncode}), falling back to drawtext")
            logger.warning(f"[Subs] FFmpeg stderr: {result.stderr[-500:]}")

    # ── Fallback: drawtext (всегда доступен) ──
    logger.info("[Subs] Using drawtext fallback")
    return _burn_subs_drawtext(video_abs, srt_abs, output_abs)


# Кеш проверки libass
_libass_available: bool | None = None


def _has_libass_support() -> bool:
    """Проверяет, поддерживает ли FFmpeg фильтр subtitles (libass)"""
    global _libass_available
    if _libass_available is not None:
        return _libass_available

    try:
        result = subprocess.run(
            ['ffmpeg', '-filters'],
            capture_output=True, text=True, timeout=10
        )
        _libass_available = 'subtitles' in result.stdout
        if not _libass_available:
            logger.warning("[Subs] FFmpeg compiled WITHOUT libass — subtitles filter unavailable")
        else:
            logger.info("[Subs] FFmpeg libass support: ✅")
    except Exception:
        _libass_available = False

    return _libass_available


def _parse_srt(srt_path: str) -> list[dict]:
    """Парсит SRT-файл → список {start, end, text}"""
    entries = []
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()

    blocks = re.split(r'\n\s*\n', content)
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        time_match = re.match(
            r'(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})',
            lines[1]
        )
        if not time_match:
            continue
        g = time_match.groups()
        start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
        end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
        text = ' '.join(lines[2:]).strip()
        entries.append({"start": start, "end": end, "text": text})

    return entries


def _burn_subs_drawtext(video_path: str, srt_path: str, output_path: str) -> str:
    """Вшивает субтитры через drawtext фильтры (не требует libass)"""
    entries = _parse_srt(srt_path)
    if not entries:
        logger.warning("[Subs] No subtitle entries found — copying video without subs")
        shutil.copy2(video_path, output_path)
        return output_path

    # Формируем drawtext-фильтры для каждой строки субтитров
    drawtext_parts = []
    for entry in entries:
        # Экранируем текст для FFmpeg drawtext: ' → '', : → \:, \ → \\
        text = entry['text']
        text = text.replace("\\", "\\\\")
        text = text.replace("'", "'\\''")
        text = text.replace(":", "\\:")
        text = text.replace("%", "%%")

        dt = (
            f"drawtext=text='{text}'"
            f":enable='between(t,{entry['start']:.3f},{entry['end']:.3f})'"
            f":fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            f":fontsize=24:fontcolor=white"
            f":borderw=2:bordercolor=black"
            f":x=(w-text_w)/2:y=h-th-50"
        )
        drawtext_parts.append(dt)

    # FFmpeg -vf поддерживает цепочку фильтров через ","
    vf = ",".join(drawtext_parts)

    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-vf', vf,
        '-c:a', 'copy', output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"[Subs/drawtext] FFmpeg stderr:\n{result.stderr[-1000:]}")
        # Последний fallback — копируем видео без субтитров
        logger.warning("[Subs] drawtext also failed — using video without subtitles")
        shutil.copy2(video_path, output_path)
        return output_path

    logger.info(f"[Subs] ✅ {output_path} (drawtext)")
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

def render_chapter_task(chapter_id: str, job_id: str):
    """
    Фоновая задача: рендер одной главы.
    1. Берёт данные главы и её script_blocks из Supabase
    2. Для каждого блока: скачивает картинку+аудио → видео → субтитры
    3. Склеивает все блоки в один chapter_{number}.mp4
    4. Загружает финальное видео на Яндекс.Диск
    5. Удаляет локальный финальный файл после успешной загрузки
    """

    logger.info(f"\n{'='*60}")
    logger.info(f"[RENDER] Starting chapter {chapter_id} (job {job_id})")
    logger.info(f"{'='*60}\n")

    update_job(job_id, status="in_progress", stage="Fetching chapter data")

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
        update_job(job_id, status="failed", stage="Failed to fetch chapter data", error=str(e))
        return

    project_id = chapter['project_id']
    chapter_number = chapter['chapter_number']

    logger.info(f"[RENDER] Project: {project_id}, Chapter #{chapter_number}")

    # ── Ставим статус "rendering" ──
    try:
        supabase.table("chapters").update({
            "status": "rendering",
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", chapter_id).execute()
    except Exception as e:
        logger.error(f"[DB] Failed to set rendering status: {e}")
        update_job(job_id, status="failed", stage="Failed to update DB status", error=str(e))
        return

    # ── Семафор ──
    update_job(job_id, stage="Waiting for render slot")
    acquired = render_semaphore.acquire(timeout=300)
    if not acquired:
        logger.error("[RENDER] Semaphore timeout — too many concurrent renders")
        supabase.table("chapters").update({
            "status": "failed", "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", chapter_id).execute()
        update_job(job_id, status="failed", stage="Semaphore timeout", error="Too many concurrent renders")
        return

    # Пути
    root_dir = Path(ROOT_VIDEOS_DIR).resolve()
    project_dir = root_dir / f"project_{project_id}"
    chapter_dir = project_dir / f"chapter_{chapter_number}"
    temp_dir = chapter_dir / "temp_scripts"

    try:
        # ========================================
        # STEP 1: Загрузка script_blocks из Supabase
        # ========================================

        update_job(job_id, status="rendering", stage="Fetching script blocks")
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
        total_blocks = len(blocks)

        # ========================================
        # STEP 2: Обработка каждого блока
        # ========================================

        processed_videos = []

        for block in blocks:
            seq = block['sequence_number']
            assets = parse_json_field(block.get('assets'))

            update_job(job_id, stage=f"Rendering block {seq}/{total_blocks}")
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
            audio_path = str((block_dir / "audio.wav").resolve())
            image_path = str((block_dir / "image.png").resolve())

            download_file(audio_url, audio_path)
            download_file(image_url, image_path)

            logger.info(f"[Block {seq}] Audio exists: {os.path.exists(audio_path)}")
            logger.info(f"[Block {seq}] Image exists: {os.path.exists(image_path)}")
            logger.info(f"[Block {seq}] Audio path: {audio_path}")
            logger.info(f"[Block {seq}] Image path: {image_path}")

            # ── Определяем длительность аудио ──
            duration = get_audio_duration(audio_path)
            logger.info(f"[Block {seq}] Audio duration: {duration:.1f}s")

            # ── Создаём видео (картинка + аудио) ──
            raw_video = str((block_dir / "raw.mp4").resolve())
            create_looped_video_with_audio(image_path, audio_path, raw_video, duration)

            # ── Субтитры через Whisper ──
            update_job(job_id, stage=f"Generating subtitles for block {seq}/{total_blocks}")
            srt_path = str((block_dir / "subs.srt").resolve())
            subs_ok = generate_subtitles_srt(audio_path, srt_path)

            # ── Вшиваем субтитры (или берём без них) ──
            if subs_ok and os.path.exists(srt_path):
                final_block_video = str((block_dir / "final.mp4").resolve())
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

        update_job(job_id, stage="Concatenating blocks into final video")
        final_path = str(project_dir / f"chapter_{chapter_number}.mp4")
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        concatenate_videos(processed_videos, final_path)

        # ========================================
        # STEP 4: Загрузка на Яндекс.Диск
        # ========================================

        update_job(job_id, status="uploading", stage="Uploading to Yandex.Disk")
        logger.info("[RENDER] Uploading final video to Yandex.Disk...")

        yadisk_url = None

        if not YANDEX_DISK_TOKEN:
            logger.error("[YaDisk] YANDEX_DISK_TOKEN not set — skipping upload")
            update_job(job_id, stage="Yandex.Disk upload skipped (no token)")
        else:
            yadisk_manager = YandexDiskManager(YANDEX_DISK_TOKEN)

            if not yadisk_manager.check_connection():
                logger.error("[YaDisk] Connection check failed")
                update_job(job_id, stage="Yandex.Disk connection failed")
            else:
                # Относительный путь: project_{id}/chapter_{n}.mp4
                remote_relative = f"project_{project_id}/chapter_{chapter_number}.mp4"
                upload_ok, upload_result = yadisk_manager.upload_file(final_path, remote_relative)

                if upload_ok:
                    yadisk_url = upload_result
                    logger.info(f"[YaDisk] ✅ Uploaded → {yadisk_url}")

                    # Удаляем локальный финальный файл после успешной загрузки
                    try:
                        os.remove(final_path)
                        logger.info(f"[Cleanup] ✅ Removed local final: {final_path}")
                    except Exception as e:
                        logger.warning(f"[Cleanup] Failed to remove local final: {e}")
                else:
                    logger.error(f"[YaDisk] ❌ Upload failed: {upload_result}")
                    update_job(job_id, stage=f"Yandex.Disk upload failed: {upload_result}")

        # ========================================
        # STEP 5: Обновление статуса
        # ========================================

        chapter_update = {
            "status": "rendered",
            "video_url": yadisk_url or final_path,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        supabase.table("chapters").update(chapter_update).eq("id", chapter_id).execute()

        update_job(
            job_id,
            status="completed",
            stage="Done",
            completed=True,
            video_url=yadisk_url or final_path,
        )

        logger.info(f"\n{'='*60}")
        logger.info(f"[RENDER] ✅ SUCCESS! → {yadisk_url or final_path}")
        logger.info(f"{'='*60}\n")

    except Exception as e:
        logger.error(f"[RENDER] ❌ ERROR: {e}", exc_info=True)
        update_job(job_id, status="failed", stage="Render failed", error=str(e))
        try:
            supabase.table("chapters").update({
                "status": "failed", "updated_at": datetime.now(timezone.utc).isoformat()
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

    Response 202: { status, message, chapter_id, job_id }
    """
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "Invalid or missing JSON body"}), 400

        chapter_id = data.get('chapter_id')
        if not chapter_id:
            return jsonify({"error": "Missing chapter_id"}), 400

        # Создаём уникальный ID задачи
        job_id = create_job(chapter_id)

        # Фоновый рендер
        thread = threading.Thread(
            target=render_chapter_task,
            args=(chapter_id, job_id),
            daemon=True
        )
        thread.start()

        logger.info(f"[API] Render started for chapter {chapter_id}, job {job_id}")

        return jsonify({
            "status": "accepted",
            "message": "Render task started",
            "chapter_id": chapter_id,
            "job_id": job_id,
        }), 202

    except Exception as e:
        logger.error(f"[API] Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/job-status/<job_id>', methods=['GET'])
@require_api_key
def job_status(job_id: str):
    """
    Возвращает текущий статус задачи рендера по job_id.

    Response 200:
    {
        "job_id": "uuid",
        "chapter_id": "uuid",
        "status": "queued|in_progress|rendering|uploading|completed|failed",
        "stage": "Human-readable current stage",
        "completed": true/false,
        "video_url": "https://... (Yandex.Disk public link)" | null,
        "error": "..." | null,
        "created_at": "...",
        "updated_at": "..."
    }
    """
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job), 200


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy"}), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5055))
    app.run(host='0.0.0.0', port=port, debug=False)
