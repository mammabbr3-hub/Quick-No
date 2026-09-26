import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

def _sqlite_path() -> str:
    raw = os.getenv("SQLITE_PATH", "/app/data/quickotp.db").strip()
    if not raw:
        raw = "/app/data/quickotp.db"
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)

@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    sqlite_path: str = _sqlite_path()
    grizzly_api_key: str = os.getenv("GRIZZLY_API_KEY", "")
    grizzly_base_url: str = os.getenv("GRIZZLY_BASE_URL", "https://api.grizzlysms.com/stubs/handler_api.php")
    permanent_admin_id: int = int(os.getenv("PERMANENT_ADMIN_ID", "7517279474"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "8"))
    report_poll_interval: int = int(os.getenv("REPORT_POLL_INTERVAL", "30"))

settings = Settings()
print(f"[config] Using SQLite database: {settings.sqlite_path}")
MIN_DEPOSIT = Decimal("3")
