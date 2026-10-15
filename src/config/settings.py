import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_ENDPOINT = os.getenv("API_ENDPOINT")
MAPS_API_KEY = os.getenv("MAPS_API_KEY")


def validate_required_env() -> None:
    """Validate required environment variables are set. Call during startup, not at import."""
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not API_ENDPOINT:
        missing.append("API_ENDPOINT")
    if not MAPS_API_KEY:
        missing.append("MAPS_API_KEY")
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")