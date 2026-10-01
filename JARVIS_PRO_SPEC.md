# JARVIS Pro — Build Specification

**Version:** 1.0.0
**Date:** 2026-10-01
**Author:** Justin Diaz
**Status:** Ready to build

---

## 1. Overview

Jarvis Pro is a multi-user fork of the JARVIS // MIND workout dashboard. Each user gets their own isolated deployment (Railway project + Postgres database) while sharing the same codebase. Justin's personal JARVIS instance at `jarvis-production-3101.up.railway.app` remains untouched.

### Architecture (unchanged from JARVIS v5.0.5)

```
Claude Desktop/App  ──MCP JSON-RPC──▶  JARVIS Pro (FastAPI)
     (voice coach)                          │
                                      PostgreSQL (truth)
                                            │
                              Browser SPA ◄──┘ (Jinja2, polls /api/*)
```

- **Claude** = conversational control plane (external, via MCP tools)
- **PostgreSQL** = source of truth for all workout data
- **JARVIS Pro** = visual dashboard that reads the DB and renders changes
- **No chat, no voice, no mic** in the dashboard itself — Claude handles all of that externally

### What ships with Jarvis Pro

- 242-exercise library (9 exercise types, muscle mapping, rehab targets)
- 60+ MCP tools for Claude integration
- Budget-vs-actual variance tracking
- Soft deletes + audit log on all mutations
- Demo mode (tagged rows, clean wipe on end)
- 4-tab SPA: HOME / TRAIN / STATS / PLAN
- Dark theme (cyan/teal/orange, JetBrains Mono)
- Mobile-first, one-handed workout use

---

## 2. Fork & Clean

### 2.1 Source

Fork from: `https://github.com/justind2416-hash/Jarvis`
New repo: `https://github.com/justind2416-hash/Jarvis-Pro`

### 2.2 Files to modify

| File | What to change |
|---|---|
| `mcp_server.py` | Replace `DEFAULT_PROFILE` with empty/generic template. Replace `DEFAULT_PROGRAM` with empty program. Change tool descriptions from "Justin's" to "the athlete's". |
| `fitness.py` | Remove `SEED_GOALS` list (Justin-specific goals). Replace with empty list — goals seeded during onboarding. Remove `BIRTHDAY` constant. |
| `exercise_library_data.py` | **Keep as-is.** The 242-exercise library is universal. Equipment-specific entries (G15) stay — they're valid exercises. |
| `database.py` | Remove hardcoded carry-forward seed items (Justin-specific). Make carry-forward seeding conditional on onboarding. Change "Justin" label in chat history to use profile name. |
| `main.py` | Add onboarding detection middleware. Add onboarding routes. Add feedback routes. Add connect-your-coach route. |
| `templates/index.html` | Keep entirely. No Justin-specific content in the template. |
| `config.yaml` | Keep as-is (generic app config). |
| `brain.py` | Review for hardcoded name references. |
| `railway.toml` | Update for one-click deploy template. |
| `.env.example` | Add `MCP_SECRET`, document all env vars. |

### 2.3 Data removal checklist

- [ ] `DEFAULT_PROFILE` in `mcp_server.py` → generic empty profile
- [ ] `DEFAULT_PROGRAM` in `mcp_server.py` → empty program placeholder
- [ ] `SEED_GOALS` in `fitness.py` → empty list
- [ ] `BIRTHDAY` in `fitness.py` → removed (read from profile)
- [ ] Carry-forward seeds in `database.py` `init_db()` → removed
- [ ] "Justin" label in `database.py` chat formatting → use profile name
- [ ] Tool descriptions referencing "Justin" → "the athlete"

---

## 3. First-Launch Onboarding

### 3.1 Detection

On every request to `/` (the SPA), check whether onboarding is complete:

```python
def is_onboarded() -> bool:
    """Check if a profile exists (i.e., onboarding was completed)."""
    profile = _load("profile.json", None)
    return profile is not None and profile.get("name") not in (None, "", "New Athlete")
```

If not onboarded, redirect to `/onboard` instead of serving the SPA.

### 3.2 Onboarding flow

**Route:** `GET /onboard` — serves `templates/onboard.html`

Multi-step form (single page, JS-driven steps):

#### Step 1: Welcome
- "Welcome to JARVIS Pro"
- "Let's set up your training profile"
- [Get Started] button

#### Step 2: Basic Info
- Name (text, required)
- Age (number, required)
- Birthday (date picker, required)
- Height (text, e.g. "5'9\"" or "175 cm")

#### Step 3: Body Composition
- Current weight (number + unit selector: lb/kg)
- Target weight (number + unit selector)

#### Step 4: Training Goals
- Multi-select checkboxes:
  - Lose weight
  - Build muscle
  - Improve endurance
  - Rehab / Recovery
  - General fitness

#### Step 5: Experience & Equipment
- Training experience: radio (Beginner / Intermediate / Advanced)
- Available equipment: multi-select checkboxes:
  - Bodyweight only
  - Dumbbells
  - Full gym (barbells, racks, cables)
  - Cable machine
  - Home gym (mixed equipment)
  - Resistance bands
  - Pull-up bar
  - Treadmill / Cardio machine

#### Step 6: Training Preferences
- Training frequency: slider or select (2–6 days/week)
- Preferred split: radio:
  - Push / Pull / Legs
  - Full body
  - Upper / Lower
  - Calisthenics
  - Mixed (let Claude decide)

#### Step 7: Injury & Limitations
- Free text area: "Any injuries, limitations, or areas of concern?"
- "Your coach (Claude) will factor these into every workout."

#### Step 8: Review & Confirm
- Summary of all selections
- [Start Training] button

### 3.3 Onboarding submission

**Route:** `POST /api/onboard`

**Actions on submit:**
1. Save profile to `profile.json` (via `_save()` in mcp_server.py):
   ```json
   {
     "name": "Alex",
     "age": 32,
     "dob": "1994-03-15",
     "height": "5'11\"",
     "weight_lbs": 185,
     "goals": {
       "primary": "Build muscle, Improve endurance",
       "selected": ["build_muscle", "improve_endurance"]
     },
     "experience": "intermediate",
     "equipment": ["dumbbells", "cable_machine", "resistance_bands"],
     "training_frequency": 4,
     "training_split": "push_pull_legs",
     "limitations": "Minor lower back tightness from sitting all day",
     "onboarded_at": "2026-10-01T08:30:00"
   }
   ```

2. Save empty program to `program.json`:
   ```json
   {
     "name": "Awaiting Coach Setup",
     "phase": "Onboarding",
     "exercises": [],
     "notes": "Connect Claude to design your first program."
   }
   ```

3. Seed goals based on selections:
   - "Lose weight" selected → create `bodyweight_cut` goal with their target weight
   - "Build muscle" selected → create `training_frequency` goal matching their frequency preference
   - Always create a `training_frequency` goal

4. Seed the exercise library (runs on `init_db()` — already ships with the app, no per-user action needed)

5. Mark the planned schedule as "needs_plan" so the PLAN tab shows a prompt to connect Claude

6. Redirect to `/coach` (Connect Your Coach page)

### 3.4 Onboarding template design

Same JARVIS visual identity:
- Dark background (`#04060A`)
- Cyan accents (`#38e0ff`)
- JetBrains Mono font
- Animated step transitions
- Mobile-responsive (works on phone during gym setup)
- Progress indicator (step X of 8)

---

## 4. Connect Your Coach Page

### 4.1 Route

**`GET /coach`** — serves `templates/coach.html`

Accessible after onboarding and from a gear/settings menu in the header.

### 4.2 Content

```
┌─────────────────────────────────────────┐
│          CONNECT YOUR COACH             │
│                                         │
│  JARVIS is your visual dashboard.       │
│  Claude is your voice coach.            │
│                                         │
│  Talk to Claude → JARVIS updates live.  │
│                                         │
│  ─── YOUR MCP CONNECTOR URL ───        │
│  ┌─────────────────────────────────┐    │
│  │ https://your-app.up.railway.app │    │
│  │ /mcp/YOUR_SECRET                │    │
│  └─────────────────────────────────┘    │
│           [Copy URL]                    │
│                                         │
│  ─── SETUP STEPS ───                   │
│                                         │
│  1. Download Claude Desktop             │
│     claude.ai/download                  │
│                                         │
│  2. Open Settings → Connectors          │
│                                         │
│  3. Click "Add MCP Connector"           │
│                                         │
│  4. Paste the URL above                 │
│                                         │
│  5. Start talking:                      │
│     "Show me my workout for today"      │
│     "I just did 4 sets of bench at 135" │
│     "Design me a 4-day program"         │
│                                         │
│  [Go to Dashboard →]                    │
└─────────────────────────────────────────┘
```

### 4.3 Dynamic URL generation

The connector URL is built from:
- `request.url` (the current host, e.g. `https://alex-jarvis.up.railway.app`)
- `/mcp/{MCP_SECRET}` if MCP_SECRET is set, else `/mcp`

```python
@app.get("/coach")
async def coach_page(request: Request):
    base = str(request.base_url).rstrip("/")
    secret = os.environ.get("MCP_SECRET", "")
    mcp_url = f"{base}/mcp/{secret}" if secret else f"{base}/mcp"
    return templates.TemplateResponse("coach.html", {
        "request": request,
        "mcp_url": mcp_url,
        "athlete_name": _load_profile_name(),
    })
```

---

## 5. Feedback System

### 5.1 Database table

```sql
CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'other',
    message TEXT NOT NULL,
    athlete_name TEXT DEFAULT '',
    page_context TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
```

Categories: `bug`, `feature`, `design`, `other`

### 5.2 UI

- Gear icon in the SPA header (top-right, next to settings)
- Opens a slide-up modal:
  - Category dropdown (Bug / Feature Request / Design / Other)
  - Message textarea
  - [Submit Feedback] button
- Toast confirmation on submit

### 5.3 API

**`POST /api/feedback`**

```json
{
  "category": "feature",
  "message": "Would love a dark mode toggle",
  "page_context": "HOME tab"
}
```

Response: `{"status": "ok", "id": 123}`

### 5.4 Webhook forwarding (optional)

If `FEEDBACK_WEBHOOK_URL` env var is set, POST the feedback JSON to that URL.
This lets Justin receive feedback in Slack, a Google Sheet, or any webhook endpoint.

```python
FEEDBACK_WEBHOOK = os.environ.get("FEEDBACK_WEBHOOK_URL", "")

async def _forward_feedback(data: dict):
    if FEEDBACK_WEBHOOK:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(FEEDBACK_WEBHOOK, json=data, timeout=5)
```

---

## 6. Railway Deploy Template

### 6.1 `railway.toml`

```toml
[build]
builder = "NIXPACKS"

[deploy]
startCommand = "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"
healthcheckPath = "/api/status"
healthcheckTimeout = 30
```

### 6.2 Environment variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Auto | Provisioned by Railway Postgres plugin |
| `MCP_SECRET` | Recommended | Secret token for MCP endpoint auth. Generate with `openssl rand -hex 16` |
| `OPENAI_API_KEY` | Optional | For TTS (text-to-speech) feature |
| `FEEDBACK_WEBHOOK_URL` | Optional | URL to forward feedback submissions |
| `TZ` | Optional | Timezone, default `America/New_York` |

### 6.3 Deploy button

README includes:

```markdown
[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/template/XXXXX)
```

The template auto-provisions:
- Python service (from this repo)
- PostgreSQL database
- Sets `DATABASE_URL` automatically

### 6.4 One-click deployment steps (for Justin as admin)

1. Click Deploy on Railway (or create project manually)
2. Railway provisions the app + Postgres
3. Set environment variables:
   - `MCP_SECRET` = generate a random token
   - `OPENAI_API_KEY` = Justin's key (optional, for TTS)
   - `FEEDBACK_WEBHOOK_URL` = Justin's webhook (optional)
4. Deploy completes → app is live at `*.up.railway.app`
5. Share the URL with the user
6. User opens the URL → onboarding flow starts
7. User connects Claude Desktop → starts training

---

## 7. Multi-User Deployment Model

### 7.1 One database per user (isolation model)

Each person gets their own Railway project:
```
Railway Account (Justin)
├── Project: jarvis-alex
│   ├── Service: jarvis-pro (from this repo)
│   └── Postgres: alex's workout data
├── Project: jarvis-sarah
│   ├── Service: jarvis-pro (from this repo)
│   └── Postgres: sarah's workout data
└── Project: jarvis-production (original JARVIS — untouched)
```

Benefits:
- Complete data isolation (HIPAA-friendly)
- Independent deploys — one user's issue doesn't affect others
- Easy teardown — delete the Railway project
- No auth system needed — the URL IS the access control
- Each user's Claude connects only to their MCP endpoint

### 7.2 Cost estimate (Railway)

Per user per month:
- Hobby plan: $5/month base
- Postgres: ~$0.50–1/month (tiny DB)
- Compute: ~$1–2/month (FastAPI idle + polls)
- **Total: ~$7–8/user/month**

### 7.3 Future: shared multi-tenant (Phase 2)

Not in this build, but the path forward:
- Add user authentication (email/password or OAuth)
- Single deployment, user_id column on all tables
- Role-based access: athlete, coach, admin
- Coach dashboard showing all their athletes
- Stripe billing for trainer subscriptions

---

## 8. File-by-File Changes

### 8.1 `mcp_server.py`

**`DEFAULT_PROFILE`** → Replace with:
```python
DEFAULT_PROFILE = {
    "name": "New Athlete",
    "age": 0,
    "dob": "",
    "height": "",
    "weight_lbs": 0,
    "goals": {
        "primary": "",
        "selected": []
    },
    "experience": "",
    "equipment": [],
    "training_frequency": 0,
    "training_split": "",
    "limitations": "",
    "onboarded_at": ""
}
```

**`DEFAULT_PROGRAM`** → Replace with:
```python
DEFAULT_PROGRAM = {
    "name": "Awaiting Coach Setup",
    "phase": "Onboarding",
    "exercises": [],
    "notes": "Connect Claude to design your first program."
}
```

**Tool descriptions** → Replace "Justin's" with "the athlete's" throughout.

### 8.2 `fitness.py`

- Remove `BIRTHDAY` constant
- Empty `SEED_GOALS = []`
- `seed_goals()` remains but is a no-op with empty list

### 8.3 `database.py`

- Remove carry-forward seed items from `init_db()`
- Replace `"Justin"` label in chat formatting with dynamic profile name lookup
- Add `feedback` table DDL to `_migrate_integrity()`

### 8.4 `main.py`

New routes:
- `GET /onboard` — onboarding page
- `POST /api/onboard` — save onboarding data
- `GET /coach` — connect-your-coach page
- `POST /api/feedback` — submit feedback
- `GET /api/onboard/status` — check if onboarded

Middleware:
- On `GET /`, check onboarding status → redirect to `/onboard` if needed

### 8.5 New templates

- `templates/onboard.html` — multi-step onboarding wizard
- `templates/coach.html` — connect-your-coach instructions

### 8.6 `templates/index.html`

Add to header:
- Gear icon that opens settings dropdown
- Settings dropdown contains: "Connect Coach", "Send Feedback"
- Feedback modal (slide-up form)

---

## 9. API Reference (new endpoints)

### `GET /api/onboard/status`
Returns: `{"onboarded": true/false, "name": "Alex"}`

### `POST /api/onboard`
Body: Full onboarding payload (see Section 3.3)
Returns: `{"status": "ok", "name": "Alex"}`

### `GET /coach`
Serves the connect-your-coach HTML page.

### `POST /api/feedback`
Body: `{"category": "bug", "message": "...", "page_context": "HOME"}`
Returns: `{"status": "ok", "id": 123}`

### `GET /api/feedback` (admin)
Returns all feedback entries. No auth (URL is access control).

---

## 10. Testing Checklist

### Onboarding
- [ ] Fresh database → app redirects to `/onboard`
- [ ] Complete all 8 steps → profile saved correctly
- [ ] Skip optional fields → still works
- [ ] After onboarding → redirects to `/coach`
- [ ] Second visit → goes straight to dashboard

### Connect Your Coach
- [ ] MCP URL displayed correctly with secret
- [ ] Copy button works
- [ ] "Go to Dashboard" link works

### Feedback
- [ ] Gear icon visible in header
- [ ] Feedback modal opens/closes
- [ ] Submit with all categories works
- [ ] Toast confirmation appears
- [ ] Feedback stored in database
- [ ] Webhook fires if configured

### MCP Tools
- [ ] All 60+ tools still work after profile changes
- [ ] `get_profile` returns onboarding data
- [ ] `get_program` returns empty program for new users
- [ ] Exercise library fully intact

### Railway Deploy
- [ ] One-click deploy provisions Postgres
- [ ] App starts with empty database
- [ ] Onboarding triggers on first visit
- [ ] Full workflow: onboard → connect → train

---

## 11. Repository Structure

```
Jarvis-Pro/
├── main.py                    # FastAPI app + all API routes
├── database.py                # PostgreSQL/SQLite persistence
├── mcp_server.py              # MCP tools (60+)
├── fitness.py                 # Exercise library, goals, measurements
├── fitness_mcp.py             # Fitness-specific MCP tools
├── brain.py                   # Chat/AI integration
├── training.py                # Training logic
├── coaching.py                # Coaching context
├── converter.py               # Data converters
├── exercise_library_data.py   # 242 exercises (seed data)
├── templates/
│   ├── index.html             # Main SPA (4-tab dashboard)
│   ├── onboard.html           # Onboarding wizard (NEW)
│   └── coach.html             # Connect-your-coach page (NEW)
├── static/
│   ├── bust_lines.js          # 3D bust animation
│   ├── hiit-timer.html        # HIIT timer
│   └── body-hologram*.html    # Body visualization
├── tests/
│   ├── test_data_integrity.py
│   ├── test_fitness.py
│   └── test_training.py
├── docs/
│   ├── SPEC.md
│   └── DATA_INTEGRITY.md
├── JARVIS_PRO_SPEC.md         # This document
├── README.md                  # Deploy instructions + overview
├── railway.toml               # Railway deploy config
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
└── .gitignore
```
