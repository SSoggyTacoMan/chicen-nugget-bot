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


def _quiet_hours(name: str) -> tuple[int, int]:
    """Parse ``HH:MM-HH:MM`` into minutes since midnight. Empty disables."""
    raw = _str(name)
    if not raw:
        return -1, -1
    try:
        start_raw, end_raw = raw.split("-", 1)
        start_h, start_m = (int(part) for part in start_raw.strip().split(":", 1))
        end_h, end_m = (int(part) for part in end_raw.strip().split(":", 1))
        if not (0 <= start_h < 24 and 0 <= end_h < 24):
            return -1, -1
        if not (0 <= start_m < 60 and 0 <= end_m < 60):
            return -1, -1
        return start_h * 60 + start_m, end_h * 60 + end_m
    except ValueError:
        return -1, -1


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
    slack_web_token: str | None
    slack_web_cookie: str | None
    slack_owner_token: str | None
    slack_owner_cookie: str | None
    bot_name: str
    owner_user_id: str
    admin_user_ids: frozenset[str]
    timezone: str
    auto_join_channels: frozenset[str]

    data_dir: Path
    log_dir: Path

    # ai
    ai_api_key: str | None
    ai_base_url: str
    ai_model: str
    ai_fallback_model: str | None
    ai_assistant_model: str
    ai_assistant_fallback_model: str | None
    ai_code_model: str | None
    ai_code_fallback_model: str | None
    ai_embedding_model: str | None
    transcribe_model: str
    transcribe_fallback_model: str | None
    ai_router_base_url: str
    ai_router_api_key: str | None
    ai_router_models: tuple[str, ...]
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
    chat_chunk_delay: float
    chat_chunk_max: int
    quiet_hours_start: int
    quiet_hours_end: int
    proactive_daily_cap: int
    sandbox_dir: Path
    sandbox_max_workspaces: int
    sandbox_exec_timeout: int
    sandbox_max_exec_timeout: int
    sandbox_ttl_days: int
    memory_retention_days: int
    storage_min_free_gb: float
    browser_enabled: bool
    browser_headless: bool
    browser_timeout: int
    browser_max_sessions: int
    browser_idle_timeout: int
    browser_dir: Path
    github_watch: bool
    github_watch_interval: int
    watch_github_notifications: bool
    watch_github_notifications_interval: int
    watch_email: bool
    watch_email_interval: int
    watch_email_limit: int
    watch_urls: tuple[str, ...]
    watch_url_interval: int
    watch_url_timeout: int
    imagine_model: str | None
    imagine_fallback_model: str | None
    image_user_ids: frozenset[str]
    tts_model: str | None
    tts_voice: str
    tts_language: str
    tts_style: str | None
    tts_max_chars: int
    tts_cost_per_1k_chars: float

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

    # google workspace (calendar, gmail, drive)
    google_access_token: str | None = None
    google_refresh_token: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://localhost:9876/oauth2callback"

    # github API (optional token raises the rate limit and enables notifications)
    github_token: str | None = None

    # mac companion link
    mac_bridge_url: str | None = None
    mac_bridge_token: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_api_key)

    @property
    def ai_router_enabled(self) -> bool:
        return bool(
            self.ai_router_api_key
            and self.ai_router_base_url
            and self.ai_router_models
        )

    @property
    def imagine_enabled(self) -> bool:
        return bool(self.ai_api_key and self.imagine_model)

    @property
    def tts_enabled(self) -> bool:
        return bool(self.ai_api_key and self.tts_model)

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

    data_dir = _path("DATA_DIR", "data")
    sandbox_dir = (
        _path("SANDBOX_DIR", "")
        if _str("SANDBOX_DIR")
        else data_dir / "sandboxes"
    )
    browser_dir = (
        _path("BROWSER_DIR", "")
        if _str("BROWSER_DIR")
        else data_dir / "browser"
    )
    quiet_start, quiet_end = _quiet_hours("QUIET_HOURS")
    router_models = _list("AI_ROUTER_MODELS")
    if not router_models:
        legacy = _str("AI_ROUTER_MODEL")
        router_models = (legacy,) if legacy else ("kgw/kilo-auto/free",)

    return Settings(
        slack_bot_token=_str("SLACK_BOT_TOKEN"),
        slack_app_token=_str("SLACK_APP_TOKEN"),
        slack_user_token=_str("SLACK_USER_TOKEN") or None,
        slack_user_cookie=_str("SLACK_USER_COOKIE") or None,
        slack_web_token=_str("SLACK_WEB_TOKEN") or None,
        slack_web_cookie=_str("SLACK_WEB_COOKIE") or None,
        slack_owner_token=_str("SLACK_OWNER_TOKEN") or None,
        slack_owner_cookie=_str("SLACK_OWNER_COOKIE") or None,
        bot_name=_str("BOT_NAME", "Chicen Nugget Bot") or "Chicen Nugget Bot",
        owner_user_id=owner,
        admin_user_ids=frozenset(admins),
        timezone=_str("TIMEZONE", "UTC") or "UTC",
        auto_join_channels=frozenset(_list("AUTO_JOIN_CHANNELS")),
        data_dir=data_dir,
        log_dir=_path("LOG_DIR", "logs"),
        ai_api_key=_str("AI_API_KEY") or None,
        ai_base_url=_str("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        ai_model=_str("AI_MODEL", "deepseek/deepseek-v4.1-flash"),
        ai_fallback_model=_str("AI_FALLBACK_MODEL", "anthropic/claude-haiku-5.5") or None,
        ai_assistant_model=_str("AI_ASSISTANT_MODEL", "anthropic/claude-sonnet-5.5")
        or "anthropic/claude-sonnet-5.5",
        ai_assistant_fallback_model=_str(
            "AI_ASSISTANT_FALLBACK_MODEL", "openai/gpt-5.5"
        )
        or "openai/gpt-5.5",
        ai_code_model=_str("AI_CODE_MODEL", "anthropic/claude-sonnet-5.5")
        or "anthropic/claude-sonnet-5.5",
        ai_code_fallback_model=_str(
            "AI_CODE_FALLBACK_MODEL", "openai/gpt-5.5"
        )
        or "openai/gpt-5.5",
        ai_embedding_model=_str("AI_EMBEDDING_MODEL", "qwen/qwen3-embedding-8b") or None,
        transcribe_model=_str("TRANSCRIBE_MODEL", "openai/gpt-audio")
        or "openai/gpt-audio",
        transcribe_fallback_model=_str(
            "TRANSCRIBE_FALLBACK_MODEL", "google/gemini-2.5-flash"
        )
        or "google/gemini-2.5-flash",
        ai_router_base_url=_str(
            "AI_ROUTER_BASE_URL", "https://router.ssoggy.me/v1"
        )
        or "https://router.ssoggy.me/v1",
        ai_router_api_key=_str("AI_ROUTER_API_KEY") or None,
        ai_router_models=router_models,
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
        chat_chunk_delay=max(0.0, _float("CHAT_CHUNK_DELAY_SECONDS", 0.6)),
        chat_chunk_max=max(1, min(8, _int("CHAT_CHUNK_MAX", 4))),
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        proactive_daily_cap=max(0, _int("PROACTIVE_DAILY_CAP", 0)),
        sandbox_dir=sandbox_dir,
        sandbox_max_workspaces=max(1, _int("SANDBOX_MAX_WORKSPACES", 20)),
        sandbox_exec_timeout=max(5, _int("SANDBOX_EXEC_TIMEOUT", 120)),
        sandbox_max_exec_timeout=max(5, _int("SANDBOX_MAX_EXEC_TIMEOUT", 600)),
        sandbox_ttl_days=max(0, _int("SANDBOX_TTL_DAYS", 30)),
        memory_retention_days=max(0, _int("MEMORY_RETENTION_DAYS", 180)),
        storage_min_free_gb=max(0.0, _float("STORAGE_MIN_FREE_GB", 1.0)),
        browser_enabled=_bool("BROWSER_ENABLED", True),
        browser_headless=_bool("BROWSER_HEADLESS", True),
        browser_timeout=max(5, _int("BROWSER_TIMEOUT", 30)),
        browser_max_sessions=max(1, _int("BROWSER_MAX_SESSIONS", 2)),
        browser_idle_timeout=max(60, _int("BROWSER_IDLE_TIMEOUT", 900)),
        browser_dir=browser_dir,
        github_watch=_bool("WATCH_GITHUB_CI", False),
        github_watch_interval=max(60, _int("WATCH_GITHUB_CI_INTERVAL", 120)),
        watch_github_notifications=_bool("WATCH_GITHUB_NOTIFICATIONS", False),
        watch_github_notifications_interval=max(
            60, _int("WATCH_GITHUB_NOTIFICATIONS_INTERVAL", 180)
        ),
        watch_email=_bool("WATCH_EMAIL", False),
        watch_email_interval=max(60, _int("WATCH_EMAIL_INTERVAL", 300)),
        watch_email_limit=max(1, min(10, _int("WATCH_EMAIL_LIMIT", 3))),
        watch_urls=tuple(_list("WATCH_URLS")),
        watch_url_interval=max(60, _int("WATCH_URL_INTERVAL", 300)),
        watch_url_timeout=max(5, _int("WATCH_URL_TIMEOUT", 15)),
        imagine_model=_str("IMAGINE_MODEL", "google/gemini-3.1-flash-image") or None,
        imagine_fallback_model=_str("IMAGINE_FALLBACK_MODEL", "google/gemini-2.5-flash-image") or None,
        image_user_ids=frozenset(_list("IMAGE_ALLOWED_USER_IDS")),
        tts_model=_str("TTS_MODEL", "qwen/qwen3-tts") or None,
        tts_voice=_str("TTS_VOICE", "Aiden") or "Aiden",
        tts_language=_str("TTS_LANGUAGE", "English") or "English",
        tts_style=_str("TTS_STYLE") or None,
        tts_max_chars=max(50, min(1500, _int("TTS_MAX_CHARS", 500))),
        tts_cost_per_1k_chars=max(0.0, _float("TTS_COST_PER_1K_CHARS", 0.02)),
        watched_channel_ids=frozenset(_list("WATCHED_CHANNEL_IDS")),
        welcome_public=_bool("WELCOME_PUBLIC", True),
        welcome_message=_str(
            "WELCOME_MESSAGE",
            "welcome to {channel}, {user}! :wave: glad you're here.",
        ),
        welcome_dm=_str(
            "WELCOME_DM",
            "hey {user}! welcome to {channel} :blob-wave: i'm {bot}. "
            "if you ever need anything in here, just ping me.",
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
        google_access_token=_str("GOOGLE_ACCESS_TOKEN") or None,
        google_refresh_token=_str("GOOGLE_REFRESH_TOKEN") or None,
        google_client_id=_str("GOOGLE_CLIENT_ID") or None,
        google_client_secret=_str("GOOGLE_CLIENT_SECRET") or None,
        google_redirect_uri=_str(
            "GOOGLE_REDIRECT_URI", "http://localhost:9876/oauth2callback"
        )
        or "http://localhost:9876/oauth2callback",
        github_token=_str("GITHUB_TOKEN") or None,
        mac_bridge_url=_str("MAC_BRIDGE_URL") or None,
        mac_bridge_token=_str("MAC_BRIDGE_TOKEN") or None,
    )


def _valid_hhmm(value: str) -> bool:
    try:
        hour, minute = value.split(":", 1)
        return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59
    except (TypeError, ValueError):
        return False


settings = load_settings()
