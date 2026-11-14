import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")


def validate_required_env() -> None:
    """Validate required environment variables are set. Call during startup, not at import."""
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
