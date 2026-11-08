# Same as app/__init__.py: sync modules can read env vars before importing app.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
