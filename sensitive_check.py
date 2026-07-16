import re
import os
import shutil
from pathlib import Path

# Regular expressions to check for secrets and sensitive numbers
SENSITIVE_PATTERNS = [
    (r'sk-[A-Za-z0-9]{20,}', "API key (sk- prefix)"),
    (r'sk-ant-[A-Za-z0-9\-_]{20,}', "Anthropic API key"),
    (r'Bearer\s+[A-Za-z0-9\-_\.]{20,}', "Bearer token"),
    (r'(?i)(api[_\-]?key|apikey)\s*[=:]\s*["\']?[A-Za-z0-9\-_]{16,}', "API key assignment"),
    (r'(?i)(password|passwd|pwd)\s*[=:]\s*["\']?.{6,}', "Password"),
    (r'(?i)(secret|credential)\s*[=:]\s*["\']?[A-Za-z0-9\-_]{8,}', "Secret/credential"),
    (r'-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----', "Private key"),
    (r'\b\d{3}-\d{2}-\d{4}\b', "SSN pattern"),
    (r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b', "Credit card pattern"),
]

# File name terms that indicate sensitive contents
SENSITIVE_FILENAME_TERMS = [
    "secure", "private", "confidential", "password", "passwd",
    "creds", "credentials", "secret", "keys", "keychain",
]

TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
    ".env", ".cfg", ".ini", ".conf", ".toml", ".csv", ".html", ".xml",
}


def check_file(file_path: str) -> tuple[bool, list[str]]:
    """
    Scans a file locally for sensitive metadata, file terms, or raw patterns.
    Returns (is_sensitive, list_of_reasons).
    Does NOT call any APIs.
    """
    path = Path(file_path)
    reasons = []

    # Filename check
    name_lower = path.name.lower()
    for term in SENSITIVE_FILENAME_TERMS:
        if term in name_lower:
            reasons.append(f"Filename contains sensitive term '{term}'")

    # Content check (text files only, skip binaries)
    ext = path.suffix.lower()
    if ext in TEXT_EXTENSIONS and path.exists():
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            # Only scan first 50KB to avoid massive log/data files
            sample = content[:51200]
            for pattern, label in SENSITIVE_PATTERNS:
                if re.search(pattern, sample):
                    reasons.append(f"Content matches sensitive pattern: {label}")
        except Exception:
            pass

    return bool(reasons), reasons


def quarantine(file_path: str, reasons: list[str], vault_root: str) -> str:
    """
    Moves a flagged file to a secure local folder within the vault 
    and stops further ingestion processing.
    """
    src = Path(file_path)
    quarantine_dir = Path(vault_root) / "Secure" / "_inbox-quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    dest = quarantine_dir / src.name
    
    # Avoid overwriting existing files in quarantine
    if dest.exists():
        dest = quarantine_dir / f"{src.stem}_{src.stat().st_mtime_ns}{src.suffix}"
        
    shutil.move(str(src), str(dest))
    return str(dest)
