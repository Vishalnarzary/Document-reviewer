from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json_write(path: Path, value: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def extract_price(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"\$\s*([0-9][0-9,]*(?:\.\d{1,2})?)", value)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def safe_public_url(value: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False, "The website URL is invalid."
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "Only public HTTP or HTTPS URLs can be reviewed."
    host = parsed.hostname.lower()
    if host in {"localhost", "0.0.0.0", "::1"} or host.endswith(".local"):
        return False, "Local or private network addresses are not allowed."
    if re.match(r"^(10\.|127\.|169\.254\.|192\.168\.)", host):
        return False, "Local or private network addresses are not allowed."
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) > 1 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return False, "Local or private network addresses are not allowed."
    return True, ""


def relative_to_root(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
def format_exception(exc: BaseException) -> str:
    """Keep operational warnings useful even for exceptions with empty messages."""
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


