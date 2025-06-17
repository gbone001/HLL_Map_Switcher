import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_ENDPOINT = os.getenv("API_ENDPOINT")
MAPS_API_KEY = os.getenv("MAPS_API_KEY")

if not DISCORD_TOKEN or not API_ENDPOINT or not MAPS_API_KEY:
    raise ValueError("Missing required environment variables.")