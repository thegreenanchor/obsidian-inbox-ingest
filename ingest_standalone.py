"""
Obsidian Inbox Ingest Pipeline
Inspired by Andrej Karpathy's "LLM Wiki" paradigm of compiling raw sources into a structured wiki.

Runs as a one-shot script to process files in the vault's _inbox/ directory:
1. Local safety scanning (sensitive_check.py)
2. Content extraction/transcription (processors.py)
3. Structured metadata compiling via the Unified LLM Adapter (supporting Anthropic, OpenAI, Gemini, OpenRouter, and Ollama)
4. Writing structured wiki pages
5. Organizing files and logging progress
"""

import os
import sys
import json
import uuid
import shutil
import base64
from datetime import date
from pathlib import Path
import urllib.request
import httpx
from dotenv import load_dotenv

from sensitive_check import check_file, quarantine
from processors import prepare_for_ingest, get_sources_subfolder

# Ensure UTF-8 execution for PowerShell/Command Prompt pipelines
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(override=True)

# Path configuration
VAULT_ROOT = Path(os.getenv("VAULT_ROOT", "./vault")).resolve()
INBOX_PATH = VAULT_ROOT / "Sources" / "_inbox"
TASKNOTES_FOLDER = os.getenv("TASKNOTES_FOLDER", "TaskNotes")

# LLM Configuration
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_API_URL = os.getenv("LLM_API_URL", "")

# Notifications Configuration
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Set default model names per provider if unspecified
if not LLM_MODEL:
    LLM_MODEL = {
        "anthropic": "claude-3-5-sonnet-latest",
        "openai": "gpt-4o",
        "gemini": "gemini-1.5-flash",
        "openrouter": "anthropic/claude-3.5-sonnet",
        "ollama": "llama3"
    }.get(LLM_PROVIDER, "claude-3-5-sonnet-latest")

SKIP_FILES = {
    "rag-anything-n8n-workflow.json",
    "n8n-wiki-workflow.json",
    "n8n-wiki-workflow.docker.json",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff", ".tif", ".bmp", ".heic"}


def fallback_extract(original_path: str, ingest_path: str) -> dict:
    """Creates a basic metadata structure if LLM extraction fails."""
    try:
        text = Path(ingest_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = ""

    return {
        "title": Path(original_path).stem,
        "domain": "PERSONAL",
        "page_type": "concept",
        "summary": text[:200] if text else "No summary available.",
        "key_points": [],
        "entities": [],
        "source_subfolder": get_sources_subfolder(original_path),
    }


def normalize_extract(data: dict, original_path: str, ingest_path: str) -> dict:
    """Enforces boundaries and formats on LLM output."""
    fallback = fallback_extract(original_path, ingest_path)
    allowed_domains = {"MNA", "TGA", "PPH", "SHL", "TGAH", "PERSONAL", "CROSS", "WORK", "RESEARCH"}
    allowed_page_types = {"concept", "project", "content", "person", "company", "brand", "campaign"}
    allowed_subfolders = {"PDFs", "Notes", "Images", "Media", "URLs", "Web", "Social", "Articles", "Data"}

    result = {**fallback, **(data or {})}
    result["title"] = str(result.get("title") or fallback["title"]).strip() or fallback["title"]
    
    # Capitalize domain and validate
    domain = str(result.get("domain") or fallback["domain"]).upper()
    result["domain"] = domain if domain in allowed_domains else "PERSONAL"
    
    # Lowercase page type and validate
    page_type = str(result.get("page_type") or fallback["page_type"]).lower()
    result["page_type"] = page_type if page_type in allowed_page_types else "concept"
    
    result["summary"] = str(result.get("summary") or fallback["summary"]).strip()
    result["key_points"] = result.get("key_points") if isinstance(result.get("key_points"), list) else []
    result["entities"] = result.get("entities") if isinstance(result.get("entities"), list) else []
    
    subfolder = str(result.get("source_subfolder") or fallback["source_subfolder"])
    result["source_subfolder"] = subfolder if subfolder in allowed_subfolders else fallback["source_subfolder"]
    
    result["is_calendar_item"] = bool(result.get("is_calendar_item", False))
    result["event_date"] = result.get("event_date")
    result["event_time"] = result.get("event_time")
    result["event_end_time"] = result.get("event_end_time")
    result["event_location"] = result.get("event_location")
    result["event_contexts"] = result.get("event_contexts") if isinstance(result.get("event_contexts"), list) else ["event"]
    
    return result


# Provider-Agnostic LLM Adapter
def call_llm_api(system_prompt: str, prompt: str, image_path: str = None) -> dict:
    """
    Unified client adapter executing HTTP calls to LLM backends.
    Supports Anthropic, OpenAI, Gemini, OpenRouter, and Ollama.
    """
    # Load image data if present
    img_b64 = ""
    img_mime = ""
    if image_path:
        suffix = Path(image_path).suffix.lower()
        mime_types = {
            ".png": "image/png", ".jpg": "image/jpeg", 
            ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"
        }
        img_mime = mime_types.get(suffix, "")
        if img_mime:
            try:
                img_b64 = base64.b64encode(Path(image_path).read_bytes()).decode("utf-8")
            except Exception as e:
                print(f"    [warn] Failed to read image for vision: {e}")

    with httpx.Client(timeout=120.0) as client:
        # --- 1. Anthropic API ---
        if LLM_PROVIDER == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": LLM_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            content = []
            if img_b64 and img_mime:
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": img_mime, "data": img_b64}
                })
            content.append({"type": "text", "text": prompt})
            
            payload = {
                "model": LLM_MODEL,
                "max_tokens": 2048,
                "system": system_prompt,
                "messages": [{"role": "user", "content": content}],
            }
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"]

        # --- 2. OpenAI API / OpenRouter / Gemini OpenAI-Compat ---
        elif LLM_PROVIDER in ("openai", "openrouter", "gemini"):
            if LLM_PROVIDER == "openai":
                url = LLM_API_URL or "https://api.openai.com/v1/chat/completions"
                headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
            elif LLM_PROVIDER == "openrouter":
                url = LLM_API_URL or "https://openrouter.ai/api/v1/chat/completions"
                headers = {
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "HTTP-Referer": "https://github.com/karpathy/llm-wiki",
                    "X-Title": "Obsidian Inbox Ingest"
                }
            else: # Gemini via OpenAI endpoint compatibility
                url = LLM_API_URL or "https://generativelanguage.googleapis.com/v1beta/openai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {LLM_API_KEY}"}

            headers["content-type"] = "application/json"
            
            user_content = []
            if img_b64 and img_mime:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{img_mime};base64,{img_b64}"}
                })
            user_content.append({"type": "text", "text": prompt})

            payload = {
                "model": LLM_MODEL,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content if image_path else prompt}
                ]
            }
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]

        # --- 3. Local Ollama ---
        elif LLM_PROVIDER == "ollama":
            url = LLM_API_URL or "http://localhost:11434/api/chat"
            headers = {"content-type": "application/json"}
            
            msg = {"role": "user", "content": prompt}
            if img_b64:
                msg["images"] = [img_b64]

            payload = {
                "model": LLM_MODEL,
                "format": "json",
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    msg
                ]
            }
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            text = resp.json()["message"]["content"]

        else:
            raise ValueError(f"Unknown model provider: {LLM_PROVIDER}")

        # Clean JSON wrappers
        clean = text.strip()
        if clean.startswith("```json"):
            clean = clean.split("```json", 1)[1]
        if clean.endswith("```"):
            clean = clean.rsplit("```", 1)[0]
        return json.loads(clean.strip())


_SYSTEM_PROMPT = "You are a professional knowledge management assistant. Extract structured metadata and return ONLY a valid JSON object."

_EXTRACTION_PROMPT = """Analyze the document details and return a JSON object with these exact keys:
- title: concise title for the wiki page (string)
- domain: one of MNA, TGA, PPH, SHL, TGAH, PERSONAL, CROSS, WORK, RESEARCH
- page_type: one of concept, project, content, person, company, brand, campaign
- summary: 2-4 sentence summary of the document (string)
- key_points: list of 3-7 bullet strings
- entities: list of named people, companies, products, or concepts mentioned
- source_subfolder: one of PDFs, Notes, Images, Media, URLs, Web, Social, Articles, Data
- is_calendar_item: true if this contains ANY reference to an event, appointment, booking, ticket, meeting, deadline, or potential calendar-worthy date (even if ambiguous or in the past); false otherwise
- event_date: "YYYY-MM-DD" if is_calendar_item is true, else null
- event_time: "HH:MM" (24-hour) if a specific start time is known, else null
- event_end_time: "HH:MM" (24-hour) if a specific end time is known, else null
- event_location: location string if known, else null
- event_contexts: list of context strings e.g. ["event"], ["conference"], ["appointment"], ["deadline"], ["ticket"]

Document Source Name: {name}
Document Content Snippet:
{content}

Return ONLY valid JSON, no other text."""


def extract_metadata(original_path: str, ingest_path: str) -> dict:
    """Decides between vision or text extraction and fires unified API query."""
    ext = Path(ingest_path).suffix.lower()
    
    if ext in IMAGE_EXTENSIONS:
        try:
            print("    Running Vision Ingest API call...")
            prompt = _EXTRACTION_PROMPT.format(name=Path(original_path).name, content="(Image file parsed via Vision)")
            result = call_llm_api(_SYSTEM_PROMPT, prompt, image_path=ingest_path)
            result["_extractor"] = f"{LLM_PROVIDER}-vision"
            return normalize_extract(result, original_path, ingest_path)
        except Exception as e:
            print(f"    [warn] Unified Vision extract failed: {e} — using image fallback")
            return {
                "title": Path(original_path).stem,
                "domain": "PERSONAL",
                "page_type": "content",
                "summary": f"Image file: {Path(original_path).name}",
                "key_points": [],
                "entities": [],
                "source_subfolder": "Images",
                "_extractor": "image-fallback",
            }

    # Text-based documents
    try:
        text = Path(ingest_path).read_text(encoding="utf-8", errors="ignore")[:6000]
        prompt = _EXTRACTION_PROMPT.format(name=Path(original_path).name, content=text)
        result = call_llm_api(_SYSTEM_PROMPT, prompt)
        result["_extractor"] = LLM_PROVIDER
        return normalize_extract(result, original_path, ingest_path)
    except Exception as e:
        print(f"    [warn] Unified text extract failed: {e} — using fallback")
        extract = fallback_extract(original_path, ingest_path)
        extract["_extractor"] = "local-fallback"
        return extract


def write_wiki_page(extract: dict, source_path: str) -> str:
    """Writes a clean frontmatter markdown file to the correct wiki directory."""
    today = date.today().isoformat()
    wiki_subdir = {
        "concept": "Concepts",
        "project": "Projects",
        "content": "Content",
        "person": "People",
        "company": "Companies",
        "brand": "Brands",
        "campaign": "Campaigns",
    }.get(extract.get("page_type", "concept"), "Concepts")

    title = extract.get("title", Path(source_path).stem)
    wiki_dir = VAULT_ROOT / "Wiki" / wiki_subdir
    wiki_dir.mkdir(parents=True, exist_ok=True)
    wiki_path = wiki_dir / f"{title}.md"

    key_points = extract.get("key_points") or []
    entities = extract.get("entities") or []
    kp_block = "\n".join(f"- {p}" for p in key_points) if key_points else "- (see source)"
    ent_block = ", ".join(str(e) for e in entities) if entities else "(none)"
    subfolder = extract.get("source_subfolder", "Articles")

    content = f"""---
title: {title}
domain: {extract.get('domain', 'PERSONAL')}
type: {extract.get('page_type', 'concept')}
status: active
last-updated: {today}
page-type: {extract.get('page_type', 'concept')}
source: "[[Sources/{subfolder}/{Path(source_path).name}]]"
---

# {title}

{extract.get('summary', '')}

## Key Points

{kp_block}

## Entities

{ent_block}

## Source

- File: [[Sources/{subfolder}/{Path(source_path).name}]]
- Ingested: {today}
"""
    wiki_path.write_text(content, encoding="utf-8")
    return str(wiki_path)


def write_pending_calendar_action(extract: dict, wiki_path: str) -> str:
    """Appends a pending calendar transaction to the vault registration index."""
    today = date.today().isoformat()
    title = extract.get("title", "Untitled Event")
    event_date = extract.get("event_date") or today
    event_time = extract.get("event_time") or ""
    location = extract.get("event_location") or ""

    safe_title = "".join(
        c if c.isalnum() or c in " -_" else "" for c in title
    ).strip().lower().replace(" ", "-")[:40]
    row_id = f"{event_date}-{safe_title}"

    pending_path = VAULT_ROOT / "Wiki" / "Synthesis" / "Pending Calendar Actions.md"

    if not pending_path.exists():
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            f"---\ntitle: Pending Calendar Actions\ndomain: PERSONAL\ntype: synthesis\n"
            f"status: active\nlast-updated: {today}\n---\n\n"
            f"# Pending Calendar Actions\n\n"
            f"Items detected by the Inbox Ingest pipeline awaiting calendar confirmation.\n"
            f'Reply "yes [row_id]" or "no [row_id]" to process.\n\n'
            f"| row_id | title | date | time | location | status | wiki_page | added |\n"
            f"|---|---|---|---|---|---|---|---|\n",
            encoding="utf-8",
        )

    row = (
        f"| {row_id} | {title} | {event_date} | {event_time} | {location} "
        f"| pending | [[{Path(wiki_path).stem}]] | {today} |\n"
    )
    with open(pending_path, "a", encoding="utf-8") as f:
        f.write(row)

    return row_id


def _to_12h(time_str: str) -> str:
    try:
        h, m = int(time_str[:2]), int(time_str[3:5])
        period = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {period}"
    except Exception:
        return time_str


def send_telegram(message: str, reply_markup: dict = None):
    """Sends notification to Telegram via standard bot message query."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        body = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }
        if reply_markup:
            body["reply_markup"] = reply_markup

        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"    [warn] Telegram notification failed: {e}")


def send_calendar_ask(extract: dict, row_id: str):
    """Sends a formatted confirmation card to Telegram with Action buttons."""
    title = extract.get("title", "Untitled Event")
    event_date = extract.get("event_date") or "(date unknown)"
    event_time = extract.get("event_time") or ""
    location = extract.get("event_location") or ""

    display_time = _to_12h(event_time) if event_time else ""
    date_line = event_date + (f" at {display_time}" if display_time else "")
    loc_line = f"\nLocation: {location}" if location else ""

    message = (
        f"📅 *Calendar item detected*\n"
        f"*{title}*\n"
        f"Date: {date_line}{loc_line}\n\n"
        f"Choose an option below:"
    )

    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ Add to Calendar", "callback_data": f"calendar_yes:{row_id}"},
            {"text": "❌ Skip", "callback_data": f"calendar_no:{row_id}"}
        ]]
    }
    
    send_telegram(message, reply_markup=reply_markup)


def append_log(line: str):
    log_path = VAULT_ROOT / "Wiki" / "log.md"
    today = date.today().isoformat()
    entry = f"\n{today} | {line}"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        print(f"    [warn] log write failed: {e}")


def process_file(file_path: Path) -> tuple[str, str]:
    """Orchestrates ingestion steps for a single inbox file."""
    # 1. Local sensitive scanning
    is_sensitive, reasons = check_file(str(file_path))
    if is_sensitive:
        try:
            qpath = quarantine(str(file_path), reasons, str(VAULT_ROOT))
            append_log(f"QUARANTINE | {file_path.name} | {'; '.join(reasons)}")
            print(f"    QUARANTINED → {qpath}")
            return "quarantined", ""
        except Exception as e:
            print(f"    ERROR: quarantine failed: {e}")
            append_log(f"ERROR | {file_path.name} | quarantine failed: {e}")
            return "error", ""

    # 2. Text preprocessing (e.g. transcribing audio, parsing HTML, extracting PDF layouts)
    ingest_path, is_temp = prepare_for_ingest(str(file_path))

    try:
        # 3. Compile metadata via unified adapter
        extract = extract_metadata(str(file_path), ingest_path)
        extractor = extract.get("_extractor", "unknown")
        print(f"    Extractor: {extractor}")

        # 4. Relocate source file to Sources/ subfolder
        subfolder = extract.get("source_subfolder") or get_sources_subfolder(str(file_path))
        dest = VAULT_ROOT / "Sources" / subfolder / file_path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(file_path), str(dest))

        # 5. Write final markdown file in Wiki/
        wiki_path = write_wiki_page(extract, str(dest))
        print(f"    OK → {wiki_path}")

        # 6. Queue calendar events if detected
        if extract.get("is_calendar_item"):
            try:
                row_id = write_pending_calendar_action(extract, wiki_path)
                send_calendar_ask(extract, row_id)
                print(f"    CALENDAR PENDING → {row_id}")
                append_log(f"CALENDAR PENDING | {file_path.name} | {row_id}")
            except Exception as e:
                print(f"    [warn] Calendar pending write failed: {e}")

        # 7. Audit Logging
        append_log(f"INGEST | {file_path.name} | extractor={extractor} | {wiki_path}")
        return "ok", wiki_path

    except Exception as e:
        print(f"    ERROR: {e}")
        append_log(f"ERROR | {file_path.name} | {e}")
        return "error", ""

    finally:
        if is_temp and Path(ingest_path).exists():
            Path(ingest_path).unlink()


def main():
    if not INBOX_PATH.exists():
        print(f"Inbox not found: {INBOX_PATH}")
        print("Please ensure your vault folder is configured correctly in .env.")
        sys.exit(1)

    files = [
        f for f in INBOX_PATH.iterdir()
        if f.is_file() and f.name not in SKIP_FILES
    ]

    if not files:
        print("Inbox empty — nothing to compile.")
        return

    total = len(files)
    print(f"Inbox compilation starting — {total} file(s)\n")

    counts = {"ok": 0, "quarantined": 0, "error": 0}
    for i, f in enumerate(files, 1):
        print(f"[{i}/{total}] Ingesting: {f.name}")
        status, wiki_path = process_file(f)
        counts[status] = counts.get(status, 0) + 1

    print("\nCompilation Summary:")
    print(f"Successful: {counts['ok']}, Quarantined: {counts['quarantined']}, Failed: {counts['error']}")

    if counts["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
