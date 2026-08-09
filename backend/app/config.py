"""
Runtime configuration. Loaded once at import time from environment
variables / a local .env file (see .env.example) — this replaces the
Streamlit-specific `st.secrets` used by the original app.py.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

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

# Postgres connection string (Supabase's "Transaction pooler" string, port
# 6543 — see supabase/README.md). Required; fails fast here rather than on
# the first confusing connection error from psycopg.
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Copy the Transaction pooler connection string from "
        "Supabase (Project Settings -> Database -> Connection string) into backend/.env "
        "— see supabase/README.md."
    )

CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
