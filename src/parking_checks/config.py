"""Settings from the environment, optionally loaded from a .env file. Validated on start."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEDULE = REPO_ROOT / "data" / "street_cleaning.json"
DEFAULT_UA = "cambridge-ma-parking-checks/0.1 (personal tow and street-cleaning alerts)"


class ConfigError(Exception):
    pass


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser. Ignores blanks and # comments; strips matching quotes."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def _list(v: str) -> list[str]:
    return [x.strip() for x in v.split(",") if x.strip()]


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from e


def _center(env: dict[str, str]) -> tuple[float, float] | None:
    lat = env.get("COVERAGE_CENTER_LAT", "").strip()
    lon = env.get("COVERAGE_CENTER_LON", "").strip()
    if not lat and not lon:
        return None
    try:
        c = (float(lat), float(lon))
    except ValueError as e:
        raise ConfigError("COVERAGE_CENTER_LAT and COVERAGE_CENTER_LON must both be numbers") from e
    if not (-90 <= c[0] <= 90 and -180 <= c[1] <= 180):
        raise ConfigError("COVERAGE_CENTER_LAT/LON out of range")
    return c


@dataclass
class Config:
    plate: str = ""
    plate_state: str = "MA"
    alert_emails: list[str] = field(default_factory=list)
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    email_webhook_url: str = ""
    email_webhook_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_ids: list[str] = field(default_factory=list)
    intake_token: str = ""
    intake_host: str = "127.0.0.1"
    intake_port: int = 8787
    public_base_url: str = ""
    freshness_max_age_hours: int = 24
    tow_reminder_hours: int = 12
    tow_reminder_max: int = 4
    cleaning_lookahead_days: int = 2
    latency_sample_days: int = 14
    db_path: Path = Path("/var/lib/parking-checks/state.db")
    schedule_path: Path = DEFAULT_SCHEDULE
    http_user_agent: str = DEFAULT_UA
    coverage_center: tuple[float, float] | None = None

    @property
    def email_enabled(self) -> bool:
        return bool(self.alert_emails and self.smtp_user and self.smtp_password)

    @property
    def email_webhook_enabled(self) -> bool:
        return bool(self.alert_emails and self.email_webhook_url and self.email_webhook_secret)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_ids)

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Config:
        cfg = cls(
            plate=env.get("PLATE", "").strip(),
            plate_state=env.get("PLATE_STATE", "MA").strip(),
            alert_emails=_list(env.get("ALERT_EMAILS", "")),
            smtp_host=env.get("SMTP_HOST", "smtp.gmail.com").strip(),
            smtp_port=_int(env, "SMTP_PORT", 587),
            smtp_user=env.get("SMTP_USER", "").strip(),
            smtp_password=env.get("SMTP_PASSWORD", "").replace(" ", ""),
            smtp_from=env.get("SMTP_FROM", "").strip() or env.get("SMTP_USER", "").strip(),
            email_webhook_url=env.get("EMAIL_WEBHOOK_URL", "").strip(),
            email_webhook_secret=env.get("EMAIL_WEBHOOK_SECRET", "").strip(),
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_ids=_list(env.get("TELEGRAM_CHAT_IDS", "")),
            intake_token=env.get("INTAKE_TOKEN", "").strip(),
            intake_host=env.get("INTAKE_HOST", "127.0.0.1").strip(),
            intake_port=_int(env, "INTAKE_PORT", 8787),
            public_base_url=env.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            freshness_max_age_hours=_int(env, "FRESHNESS_MAX_AGE_HOURS", 24),
            tow_reminder_hours=_int(env, "TOW_REMINDER_HOURS", 12),
            tow_reminder_max=_int(env, "TOW_REMINDER_MAX", 4),
            cleaning_lookahead_days=_int(env, "CLEANING_LOOKAHEAD_DAYS", 2),
            latency_sample_days=_int(env, "LATENCY_SAMPLE_DAYS", 14),
            db_path=Path(env.get("DB_PATH", "").strip() or "/var/lib/parking-checks/state.db"),
            schedule_path=Path(env.get("SCHEDULE_PATH", "").strip() or DEFAULT_SCHEDULE),
            http_user_agent=env.get("HTTP_USER_AGENT", "").strip() or DEFAULT_UA,
            coverage_center=_center(env),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not 1 <= self.cleaning_lookahead_days <= 7:
            raise ConfigError("CLEANING_LOOKAHEAD_DAYS must be between 1 and 7")
        if self.freshness_max_age_hours < 1:
            raise ConfigError("FRESHNESS_MAX_AGE_HOURS must be positive")
        # Half-configured channels are disabled with a warning, so the services can run before
        # every secret is in place. notify-test reports which channels are live.
        if self.email_webhook_url and not self.email_webhook_url.startswith("https://script.google.com/"):
            raise ConfigError("EMAIL_WEBHOOK_URL must be an https://script.google.com/... web app URL")
        if self.alert_emails and not (self.email_webhook_enabled or self.email_enabled):
            log.warning(
                "email alerts disabled: ALERT_EMAILS set but neither EMAIL_WEBHOOK_URL/SECRET "
                "nor SMTP_USER/SMTP_PASSWORD is complete"
            )
        if self.telegram_chat_ids and not self.telegram_bot_token:
            log.warning("telegram alerts disabled: TELEGRAM_CHAT_IDS set but TELEGRAM_BOT_TOKEN missing")
        if not self.schedule_path.is_file():
            raise ConfigError(f"schedule file not found: {self.schedule_path}")

    def require_plate(self) -> None:
        if len(self.plate.replace(" ", "")) < 3:
            raise ConfigError("PLATE must be set in .env (at least 3 characters)")

    def require_intake_token(self) -> None:
        if len(self.intake_token) < 24:
            raise ConfigError("INTAKE_TOKEN must be set and at least 24 characters")


def load_config(env_file: Path | None = None) -> Config:
    env: dict[str, str] = {}
    candidate = env_file or Path(os.environ.get("PARKING_CHECKS_ENV", REPO_ROOT / ".env"))
    if candidate.is_file():
        env.update(load_env_file(candidate))
    elif env_file is not None:
        raise ConfigError(f"env file not found: {env_file}")
    env.update({k: v for k, v in os.environ.items() if k.isupper()})
    return Config.from_env(env)
