"""
Runtime configuration. Loaded once at import time from environment
variables / a local .env file (see .env.example) — this replaces the
Streamlit-specific `st.secrets` used by the original app.py.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY") or None
# gpt-4o (not -mini) — the mini model was noticeably weaker at reading Thai
# furniture/procurement text (PDF extraction + price matching), so this
# defaults to the full model. Override via OPENAI_MODEL in .env if needed.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# Optional — when set, furniture-list extraction (Step 2) prefers Claude over
# OpenAI, since it tends to read Thai text more accurately. Price matching
# (Step 3) still always uses OpenAI. Leave unset to use OpenAI everywhere.
ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY") or None
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

AI_TIMEOUT_SECONDS = 60
AI_MAX_RETRIES = 2

# Deliberately NOT the original Streamlit app's bom_app_projects.db — that
# file's `projects` table has a completely different (single flat row with
# JSON-blob columns) shape, incompatible with the normalized schema below.
# Keeping a separate file means the original app (if ever run again) is
# never touched by this backend, and vice versa.
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "bom_backend.db")))

CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
