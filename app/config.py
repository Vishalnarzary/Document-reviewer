from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT_DIR / "output"
REVIEWS_DIR = OUTPUT_DIR / "reviews"
PDF_OUTPUT_DIR = OUTPUT_DIR / "pdf"
CHECKLIST_DIR = ROOT_DIR / "config" / "checklists"
STATIC_DIR = ROOT_DIR / "app" / "static"


def _load_dotenv() -> None:
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    groq_vision_model: str = os.getenv(
        "GROQ_VISION_MODEL",
        "meta-llama/llama-4-scout-17b-16e-instruct",
    )
    groq_discovery_model: str = os.getenv("GROQ_DISCOVERY_MODEL", "groq/compound-mini")
    app_host: str = os.getenv("APP_HOST", "127.0.0.1")
    app_port: int = int(os.getenv("APP_PORT", "8000"))
    crawl_max_pages: int = int(os.getenv("CRAWL_MAX_PAGES", "5"))
    crawl_max_depth: int = int(os.getenv("CRAWL_MAX_DEPTH", "1"))
    crawl_concurrency: int = int(os.getenv("CRAWL_CONCURRENCY", "3"))
    crawl_timeout_ms: int = int(os.getenv("CRAWL_TIMEOUT_MS", "45000"))
    vision_max_images: int = min(5, max(1, int(os.getenv("VISION_MAX_IMAGES", "5"))))
    capture_timezone: str = os.getenv("CAPTURE_TIMEZONE", "America/New_York")

    @property
    def groq_enabled(self) -> bool:
        return bool(self.groq_api_key and "your_groq" not in self.groq_api_key.lower())


settings = Settings()
for directory in (OUTPUT_DIR, REVIEWS_DIR, PDF_OUTPUT_DIR):
    directory.mkdir(parents=True, exist_ok=True)
