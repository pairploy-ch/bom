# SSK The Cat Workspace

Two implementations live in this repo:

- **`app.py`** — the original Streamlit app. Untouched, still runs exactly as before (`streamlit run app.py`), using `bom_app_projects.db`.
- **`backend/` + `frontend/`** — a Next.js UI over a FastAPI backend, built to replace it: same 3-step pipeline (Excel template → GPT furniture-list extraction → GPT price matching → Excel export with formulas preserved), same pricing rules, but with real server-side persistence instead of Streamlit's session-state-plus-URL-query-param workaround. See `backend/app/logic.py` for the ported business logic — every function is a straight port of `app.py`'s, with only the `st.*` calls removed.

## Running the new backend + frontend

**Backend (FastAPI):**

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate        # .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env          # fill in OPENAI_API_KEY
uvicorn app.main:app --reload --port 8010
```

Uses its own SQLite file, `backend/bom_backend.db` — created automatically on first run, and deliberately separate from `bom_app_projects.db` (the original app's data is never touched).

**Frontend (Next.js):**

```bash
cd frontend
npm install
npm run dev
```

Reads the backend URL from `frontend/.env.local` (`NEXT_PUBLIC_API_BASE_URL`, defaults to `http://localhost:8010/api`).

Then open the frontend's URL, create a project, and work through the same 3 steps as before.

## What's different from the original app

- **State lives in the database, not the browser tab.** Every edit (furniture rows, price mapping, column config aside — see below) is saved via the API immediately, so refreshing, closing the tab, or opening the project on another device all just re-fetch the same data. No more `?project=...` URL trick.
- **Column Mapping** (the sidebar's Excel column settings) is intentionally still session-only, matching the original — it was never persisted per project there either.
- **Baseline furniture value** and the **ALT Loading Factor's AI-extracted starting numbers** now *are* persisted (the original set them in `st.session_state` but never actually included them in `save_project_state()`, so they silently reset on every refresh — this was closed as part of the state-management fix).
- **No in-app "Reset All / Start Over" button.** The original's version wiped the whole Streamlit session. The equivalent now is deleting the project from the home page (or just starting a new one) — there's no single "wipe this project's data back to empty" action.

## Testing notes

The backend's non-AI-dependent endpoints (projects, template upload, furniture list, mapping edits, price preview math, the Loading Factor calculator, Excel export/formula-writing, PDF/text storage) were exercised end-to-end against a synthetic template and verified against hand-calculated numbers. Frontend type-checks, lints, and builds cleanly, and every route was smoke-tested for a clean render with no server errors.

**Not live-tested**: the two GPT-4o-mini-backed endpoints (furniture-list extraction, price matching) — this environment has no `OPENAI_API_KEY` configured. Set one in `backend/.env` and run through Steps 2–3 with a real PDF to confirm those before relying on it.
