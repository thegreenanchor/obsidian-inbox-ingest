import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import html2text

YOUTUBE_PATTERN = re.compile(
    r'(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]+'
)

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
URL_EXTENSIONS = {".url", ".webloc"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff", ".tif", ".bmp", ".heic"}
PDF_EXTENSIONS = {".pdf"}
TEXT_PASSTHROUGH_EXTENSIONS = {".md", ".txt", ".html", ".htm", ".rtf", ".csv"}
NATIVE_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls",
    ".md", ".txt", ".html", ".htm", ".rtf",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff", ".tif", ".bmp", ".heic",
}
UNSUPPORTED_EXTENSIONS = {".key"}

# Platform independent pdftotext executable search paths
PDFTOTEXT_PATHS = ["pdftotext"]
if os.name == "nt":
    # Optional fallback paths on Windows
    PDFTOTEXT_PATHS.extend([
        r"C:\Program Files\WinGet\Packages\oschwartz10612.Poppler_Microsoft.Winget.Source_8wekyb3d8bbwe\poppler-25.07.0\Library\bin\pdftotext.exe",
        r"C:\Program Files\Git\mingw64\bin\pdftotext.exe"
    ])


def pdf_to_text(pdf_path: str) -> str:
    """Extracts text content from a PDF using pdftotext CLI."""
    for cmd in PDFTOTEXT_PATHS:
        try:
            result = subprocess.run(
                [cmd, "-layout", "-enc", "UTF-8", pdf_path, "-"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return f"[PDF text extraction failed for {Path(pdf_path).name}. Make sure pdftotext is installed.]"


def get_file_group(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    if ext in NATIVE_EXTENSIONS:
        return "native"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in URL_EXTENSIONS:
        return "url"
    if ext == ".txt":
        return "text_or_url"
    if ext in UNSUPPORTED_EXTENSIONS:
        return "unsupported"
    return "native"


def transcribe_audio(audio_path: str) -> str:
    """Transcribes audio using faster-whisper. Gracefully falls back if module is missing."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return f"[Audio transcription skipped: faster-whisper not installed. File: {Path(audio_path).name}]"
        
    try:
        # Load a base model on CPU. Users can change this to 'tiny', 'small', or GPU (cuda)
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path)
        return " ".join(seg.text for seg in segments)
    except Exception as e:
        return f"[Audio transcription failed: {e}]"


def extract_audio_from_video(video_path: str) -> str:
    """Extracts audio channel from video into WAV via ffmpeg CLI."""
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(
            ["ffmpeg", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", tmp, "-y"],
            check=True, capture_output=True,
        )
        return tmp
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise RuntimeError(f"FFmpeg audio extraction failed: {e}. Ensure ffmpeg is installed.") from e


def get_url_from_file(file_path: str) -> str | None:
    """Reads a .url or .webloc file and returns the destination link."""
    content = Path(file_path).read_text(encoding="utf-8", errors="ignore").strip()
    
    # Standard INI style Windows .url files
    for line in content.splitlines():
        if line.startswith("URL="):
            return line[4:].strip()
            
    # Plain URL inside text files
    if content.startswith("http"):
        return content.split()[0]
        
    return None


def fetch_youtube_transcript(url: str) -> str:
    """Extracts description or automatic subtitles from YouTube link using yt-dlp."""
    try:
        # Platform-independent run of yt-dlp.
        # Fall back to description if subtitle download fails.
        tmp_out = os.path.join(tempfile.gettempdir(), "ytdlp_out")
        
        result = subprocess.run(
            ["yt-dlp", "--skip-download", "--write-auto-sub",
             "--sub-format", "vtt", "--sub-lang", "en", "-o", tmp_out, url],
            capture_output=True, text=True, timeout=90
        )
        
        # Read subtitles if created
        vtt_path = f"{tmp_out}.en.vtt"
        if os.path.exists(vtt_path):
            content = Path(vtt_path).read_text(encoding="utf-8", errors="ignore")
            os.unlink(vtt_path)
            lines = [
                l.strip() for l in content.splitlines()
                if l.strip() and not l.startswith("WEBVTT") and "-->" not in l and not l.startswith("NOTE")
            ]
            return " ".join(lines)
            
        # Fallback to metadata description
        result_desc = subprocess.run(
            ["yt-dlp", "--skip-download", "--print", "description", url],
            capture_output=True, text=True, timeout=30
        )
        if result_desc.returncode == 0 and result_desc.stdout.strip():
            return result_desc.stdout
            
    except Exception as e:
        return f"[YouTube extract failed: {e}. Ensure yt-dlp CLI is installed.]"
        
    return f"[YouTube URL: {url} — transcript/details unavailable]"


def fetch_url_content(url: str) -> str:
    """Fetches a URL and converts HTML to clean readable markdown-like text."""
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True)
        h = html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        return h.handle(resp.text)
    except Exception as e:
        return f"[URL fetch failed: {e}]"


def prepare_for_ingest(file_path: str) -> tuple[str, bool]:
    """
    Standardizes inputs for extraction. Converts PDFs, audio, videos, and links 
    into plain text files.
    Returns (path_to_ingest_content, temp_file_created_boolean).
    """
    group = get_file_group(file_path)

    if group == "unsupported":
        raise ValueError(f"Unsupported file type: {Path(file_path).suffix} — export to PDF/text first.")

    if group == "native":
        ext = Path(file_path).suffix.lower()
        if ext in PDF_EXTENSIONS:
            text = pdf_to_text(file_path)
            tmp = tempfile.mktemp(suffix=".txt")
            Path(tmp).write_text(text, encoding="utf-8")
            return tmp, True
        if ext in TEXT_PASSTHROUGH_EXTENSIONS:
            return file_path, False
        if ext in IMAGE_EXTENSIONS:
            return file_path, False  # Images are handled directly by Vision models
        return file_path, False

    if group == "audio":
        transcript = transcribe_audio(file_path)
        tmp = tempfile.mktemp(suffix=".txt")
        Path(tmp).write_text(transcript, encoding="utf-8")
        return tmp, True

    if group == "video":
        audio_path = extract_audio_from_video(file_path)
        transcript = transcribe_audio(audio_path)
        if os.path.exists(audio_path):
            os.unlink(audio_path)
        tmp = tempfile.mktemp(suffix=".txt")
        Path(tmp).write_text(transcript, encoding="utf-8")
        return tmp, True

    if group in ("url", "text_or_url"):
        url = get_url_from_file(file_path)
        if url:
            if YOUTUBE_PATTERN.search(url):
                text = fetch_youtube_transcript(url)
            else:
                text = fetch_url_content(url)
        else:
            text = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        tmp = tempfile.mktemp(suffix=".txt")
        Path(tmp).write_text(text, encoding="utf-8")
        return tmp, True

    return file_path, False


def get_sources_subfolder(file_path: str) -> str:
    """Helper mapping extensions to vault subdirectory categories."""
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        return "PDFs"
    if ext in {".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls"}:
        return "Notes"
    if ext in IMAGE_EXTENSIONS:
        return "Images"
    if ext in AUDIO_EXTENSIONS | VIDEO_EXTENSIONS:
        return "Media"
    if ext in URL_EXTENSIONS:
        return "URLs"
    if ext in {".html", ".htm"}:
        return "Web"
    return "Articles"
