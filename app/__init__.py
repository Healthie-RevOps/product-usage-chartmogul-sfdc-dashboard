# Load .env for local runs. Never overrides real env vars, and there is no .env in
# production (Railway), so this only affects local development.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
