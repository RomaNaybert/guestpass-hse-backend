import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    signing_key: str
    bot_api_key: str
    bot_token: str
    organizer_ids: frozenset[int]
    event_slug: str = "business-club-demo"
    event_name: str = "Встреча Бизнес-клуба"
    check_in_points: int = 10
    pass_ttl_days: int = 7
    public_url: str = "http://localhost:8080"
    api_url: str = "http://api:8080"
    cookie_secure: bool = False
    bot_username: str = ""
    review_enabled: bool = True

    @classmethod
    def from_env(cls):
        signing_key = os.environ.get("SIGNING_KEY", "")
        bot_api_key = os.environ.get("BOT_API_KEY", "")
        if min(len(signing_key), len(bot_api_key)) < 32:
            raise ValueError("SIGNING_KEY and BOT_API_KEY must each contain at least 32 random characters")
        settings = cls(
            database_url=os.environ["DATABASE_URL"],
            signing_key=signing_key,
            bot_api_key=bot_api_key,
            bot_token=os.environ.get("BOT_TOKEN", ""),
            organizer_ids=frozenset(int(x) for x in os.environ.get("ORGANIZER_IDS", "").split(",") if x.strip()),
            event_slug=os.environ.get("EVENT_SLUG", "business-club-demo"),
            event_name=os.environ.get("EVENT_NAME", "Встреча Бизнес-клуба"),
            check_in_points=int(os.environ.get("CHECK_IN_POINTS", "10")),
            pass_ttl_days=int(os.environ.get("PASS_TTL_DAYS", "7")),
            public_url=os.environ.get("PUBLIC_URL", "http://localhost:8080").rstrip("/"),
            api_url=os.environ.get("API_URL", "http://api:8080").rstrip("/"),
            cookie_secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
            bot_username=os.environ.get("BOT_USERNAME", ""),
            review_enabled=os.environ.get("REVIEW_ENABLED", "true").lower() == "true",
        )
        if not 1 <= settings.check_in_points <= 10000 or not 1 <= settings.pass_ttl_days <= 365:
            raise ValueError("Invalid points or pass lifetime")
        return settings
