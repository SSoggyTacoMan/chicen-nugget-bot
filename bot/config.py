"""Environment-backed settings.

Import `settings` anywhere. Required tokens are only validated by
`settings.missing_required()`, which `main.py` calls at startup, so importing
the package (or running tests) never requires a real Slack app.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _str(name)
    if not raw:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _path(name: str, default: str) -> Path:
    raw = _str(name, default)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str
    slack_app_token: str
    slack_user_token: str | None
    slack_user_cookie: str | None
    bot_name: str
    owner_user_id: str
    admin_user_ids: frozenset[str]
    timezone: str

    data_dir: Path
    log_dir: Path

    # ai
    ai_api_key: str | None
    ai_base_url: str
    ai_model: str
    ai_fallback_model: str | None
    ai_max_tokens: int
    ai_history_limit: int
    ai_channels: frozenset[str]
    ai_allow_admins: bool
    ai_rate_limit_per_minute: int
    ai_global_rate_limit_per_minute: int
    ai_daily_budget_usd: float
    ai_user_daily_budget_usd: float
    ai_max_context_chars: int
    ai_image_cost_usd: float
    ai_gate_enabled: bool
    ai_gate_model: str
    ai_gate_timeout: float
    ai_jev_url: str
    ai_gate_min_confidence: float
    ai_gate_addressed_confidence: float
    ai_gate_spam_threshold: float
    ai_ambient_channels: frozenset[str]
    ai_ambient_cooldown: int
    ai_thread_cooldown: int
    imagine_model: str | None
    imagine_fallback_model: str | None
    image_user_ids: frozenset[str]

    # welcomer
    watched_channel_ids: frozenset[str]
    welcome_public: bool
    welcome_message: str
    welcome_dm: str
    welcome_dm_once: bool
    welcome_ping: str
    logs_channel_id: str | None
    usergroup_id: str | None
    usergroup_prompt: bool
    usergroup_prompt_text: str
    usergroup_prompt_once: bool

    # tier 2 access
    access_channel_id: str | None
    access_panel_channel_id: str | None
    access_panel_text: str
    access_cooldown_hours: int
    admin_channel_id: str | None

    # usergroup helpers
    usergroup_guard: bool
    thread_helper: bool

    # tier 2 access extras
    access_auto_approve: bool

    # daily recap
    recap_channel_id: str | None
    recap_private_channel_id: str | None
    recap_time: str | None
    recap_reminder_minutes: int
    recap_ping: str | None
    recap_private_ping: str | None

    # extras
    daily_digest_time: str | None
    weather_default_location: str | None
    tavily_api_key: str | None
    max_reminders: int
    repo_url: str
    github_username: str | None

    # email (imap)
    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_api_key)

    @property
    def imagine_enabled(self) -> bool:
        return bool(self.ai_api_key and self.imagine_model)

    @property
    def welcomer_enabled(self) -> bool:
        return bool(self.watched_channel_ids)

    @property
    def access_enabled(self) -> bool:
        return bool(self.access_channel_id)

    @property
    def recaps_enabled(self) -> bool:
        return bool(self.recap_channel_id and self.recap_time)

    @property
    def watch_everywhere(self) -> bool:
        return "*" in self.watched_channel_ids

    @property
    def ai_everywhere(self) -> bool:
        return "*" in self.ai_channels

    def is_admin(self, user_id: str | None) -> bool:
        return bool(user_id) and (user_id in self.admin_user_ids)

    def image_allowed(self, user_id: str | None) -> bool:
        """Admins always may; everyone else needs IMAGE_ALLOWED_USER_IDS."""
        return self.is_admin(user_id) or (
            bool(user_id) and user_id in self.image_user_ids
        )

    def watches(self, channel_id: str | None) -> bool:
        if not channel_id:
            return False
        return self.watch_everywhere or channel_id in self.watched_channel_ids

    def ai_allowed(self, channel_id: str | None) -> bool:
        if not channel_id:
            return False
        return self.ai_everywhere or channel_id in self.ai_channels

    def ambient_allowed(self, channel_id: str | None) -> bool:
        """May the bot listen in this channel and decide on its own to reply?"""
        if not channel_id or not self.ai_gate_enabled:
            return False
        return "*" in self.ai_ambient_channels or channel_id in self.ai_ambient_channels

    def missing_required(self) -> list[str]:
        problems: list[str] = []
        if not self.slack_bot_token or not self.slack_bot_token.startswith("xoxb-"):
            problems.append("SLACK_BOT_TOKEN must be a bot token starting with xoxb-")
        if not self.slack_app_token or not self.slack_app_token.startswith("xapp-"):
            problems.append("SLACK_APP_TOKEN must be an app token starting with xapp-")
        if not self.owner_user_id:
            problems.append("OWNER_USER_ID is required (your Slack member id, e.g. U012ABCDEF)")
        if self.access_enabled and not self.admin_user_ids:
            problems.append("ADMIN_USER_IDS or OWNER_USER_ID is required for access requests")
        return problems


def load_settings() -> Settings:
    owner = _str("OWNER_USER_ID")
    admins = set(_list("ADMIN_USER_IDS"))
    if owner:
        admins.add(owner)

    digest = _str("DAILY_DIGEST_TIME") or None
    if digest and not _valid_hhmm(digest):
        digest = None

    recap_time = _str("RECAP_TIME") or None
    if recap_time and not _valid_hhmm(recap_time):
        recap_time = None

    return Settings(
        slack_bot_token=_str("SLACK_BOT_TOKEN"),
        slack_app_token=_str("SLACK_APP_TOKEN"),
        slack_user_token=_str("SLACK_USER_TOKEN") or None,
        slack_user_cookie=_str("SLACK_USER_COOKIE") or None,
        bot_name=_str("BOT_NAME", "Chicen Nugget Bot") or "Chicen Nugget Bot",
        owner_user_id=owner,
        admin_user_ids=frozenset(admins),
        timezone=_str("TIMEZONE", "UTC") or "UTC",
        data_dir=_path("DATA_DIR", "data"),
        log_dir=_path("LOG_DIR", "logs"),
        ai_api_key=_str("AI_API_KEY") or None,
        ai_base_url=_str("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        ai_model=_str("AI_MODEL", "anthropic/claude-haiku-5.5"),
        ai_fallback_model=_str("AI_FALLBACK_MODEL", "google/gemini-2.5-flash") or None,
        ai_max_tokens=_int("AI_MAX_TOKENS", 1200),
        ai_history_limit=_int("AI_HISTORY_LIMIT", 24),
        ai_channels=frozenset(_list("AI_CHANNELS", ("*",))),
        ai_allow_admins=_bool("AI_ALLOW_ADMINS", True),
        ai_rate_limit_per_minute=max(0, _int("AI_RATE_LIMIT_PER_MINUTE", 12)),
        ai_global_rate_limit_per_minute=max(0, _int("AI_GLOBAL_RATE_LIMIT_PER_MINUTE", 30)),
        ai_daily_budget_usd=max(0.0, _float("AI_DAILY_BUDGET_USD", 0.0)),
        ai_user_daily_budget_usd=max(0.0, _float("AI_USER_DAILY_BUDGET_USD", 0.50)),
        ai_max_context_chars=max(2_000, _int("AI_MAX_CONTEXT_CHARS", 24_000)),
        ai_image_cost_usd=max(0.0, _float("AI_IMAGE_COST_USD", 0.04)),
        ai_gate_enabled=_bool("AI_GATE_ENABLED", True),
        ai_gate_model=_str("AI_GATE_MODEL", "jev-latest") or "jev-latest",
        ai_gate_timeout=_float("AI_GATE_TIMEOUT", 8.0),
        ai_jev_url=_str("AI_JEV_URL"),
        ai_gate_min_confidence=_float("AI_GATE_MIN_CONFIDENCE", 0.5),
        ai_gate_addressed_confidence=_float("AI_GATE_ADDRESSED_CONFIDENCE", 0.6),
        ai_gate_spam_threshold=_float("AI_GATE_SPAM_THRESHOLD", 0.7),
        ai_ambient_channels=frozenset(_list("AI_AMBIENT_CHANNELS")),
        ai_ambient_cooldown=max(0, _int("AI_AMBIENT_COOLDOWN", 120)),
        ai_thread_cooldown=max(0, _int("AI_THREAD_COOLDOWN", 120)),
        imagine_model=_str("IMAGINE_MODEL") or None,
        imagine_fallback_model=_str("IMAGINE_FALLBACK_MODEL") or None,
        image_user_ids=frozenset(_list("IMAGE_ALLOWED_USER_IDS")),
        watched_channel_ids=frozenset(_list("WATCHED_CHANNEL_IDS")),
        welcome_public=_bool("WELCOME_PUBLIC", True),
        welcome_message=_str(
            "WELCOME_MESSAGE",
            "welcome to the server, {user}! :wave: glad you're here.",
        ),
        welcome_dm=_str(
            "WELCOME_DM",
            "hey {user}! welcome to the server :blob-wave: i'm {bot}, the resident bot. "
            "if you want access to the private tier 2 channel, hit the button below and "
            "i'll pass it along.",
        ),
        welcome_dm_once=_bool("WELCOME_DM_ONCE", True),
        welcome_ping=_str("WELCOME_PING"),
        logs_channel_id=_str("LOGS_CHANNEL_ID") or None,
        usergroup_id=_str("USERGROUP_ID") or None,
        usergroup_prompt=_bool("USERGROUP_PROMPT", True),
        usergroup_prompt_text=_str(
            "USERGROUP_PROMPT_TEXT",
            "want me to add you to the ping group? you'll get pinged about the good stuff.",
        ),
        usergroup_prompt_once=_bool("USERGROUP_PROMPT_ONCE", True),
        access_channel_id=_str("ACCESS_CHANNEL_ID") or None,
        access_panel_channel_id=_str("ACCESS_PANEL_CHANNEL_ID") or None,
        access_panel_text=_str(
            "ACCESS_PANEL_TEXT",
            "*tier 2 access* :lock:\nwant into the private tier 2 channel? "
            "hit the button, tell me why, and i'll send it to the admins.",
        ),
        access_cooldown_hours=max(0, _int("ACCESS_COOLDOWN_HOURS", 24)),
        admin_channel_id=_str("ADMIN_CHANNEL_ID") or None,
        access_auto_approve=_bool("ACCESS_AUTO_APPROVE", False),
        usergroup_guard=_bool("USERGROUP_GUARD", True),
        thread_helper=_bool("THREAD_HELPER", True),
        recap_channel_id=_str("RECAP_CHANNEL_ID") or None,
        recap_private_channel_id=_str("RECAP_PRIVATE_CHANNEL_ID") or None,
        recap_time=recap_time,
        recap_reminder_minutes=max(0, _int("RECAP_REMINDER_MINUTES", 10)),
        recap_ping=_str("RECAP_PING") or None,
        recap_private_ping=_str("RECAP_PRIVATE_PING") or None,
        daily_digest_time=digest,
        weather_default_location=_str("WEATHER_DEFAULT_LOCATION") or None,
        tavily_api_key=_str("TAVILY_API_KEY") or None,
        max_reminders=max(1, _int("MAX_REMINDERS", 200)),
        repo_url=_str("REPO_URL"),
        github_username=_str("GITHUB_USERNAME") or None,
        imap_host=_str("IMAP_HOST") or None,
        imap_port=_int("IMAP_PORT", 993),
        imap_user=_str("IMAP_USER") or None,
        imap_password=_str("IMAP_PASSWORD") or None,
    )


def _valid_hhmm(value: str) -> bool:
    try:
        hour, minute = value.split(":", 1)
        return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59
    except (TypeError, ValueError):
        return False


settings = load_settings()
