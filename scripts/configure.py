"""Interactive first run; actual credentials stay in ignored .env with mode 0600."""

import getpass
from pathlib import Path
import secrets

path = Path(__file__).resolve().parent.parent / ".env"
if path.exists():
    raise SystemExit(".env already exists. Edit it locally; this script never overwrites secrets.")
token = getpass.getpass("BotFather token (hidden): ").strip()
username = input("Bot username without @: ").strip().lstrip("@")
organizers = input("Trusted organizer Telegram IDs separated by commas (may be empty for review only): ").strip()
public_url = input("Public URL [http://localhost:8096]: ").strip() or "http://localhost:8096"
password = secrets.token_urlsafe(24)
values = {
    "DATABASE_URL": f"postgresql://guestpass:{password}@db:5432/guestpass",
    "POSTGRES_PASSWORD": password,
    "BOT_TOKEN": token,
    "BOT_USERNAME": username,
    "SIGNING_KEY": secrets.token_urlsafe(48),
    "BOT_API_KEY": secrets.token_urlsafe(48),
    "ORGANIZER_IDS": organizers,
    "EVENT_SLUG": "business-club-demo",
    "EVENT_NAME": "Встреча сообщества",
    "CHECK_IN_POINTS": "10",
    "PASS_TTL_DAYS": "7",
    "PUBLIC_URL": public_url.rstrip("/"),
    "API_URL": "http://api:8080",
    "COOKIE_SECURE": str(public_url.startswith("https://")).lower(),
}
if not token or any("\n" in value or "\r" in value for value in values.values()):
    raise SystemExit("Provide a token and single-line values")
path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
path.chmod(0o600)
print(".env created. Run: docker compose -f compose.yaml up -d --build")
