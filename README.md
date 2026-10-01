# JARVIS Pro

**Your AI workout dashboard — powered by Claude.**

JARVIS Pro is a personal workout tracking dashboard that pairs with Claude (via MCP) to give you a voice-controlled AI coaching experience. Talk to Claude on your phone, watch your workout update live on your screen.

![Architecture](https://img.shields.io/badge/Claude-Voice_Coach-blue) ![Architecture](https://img.shields.io/badge/JARVIS-Visual_Dashboard-cyan) ![Architecture](https://img.shields.io/badge/PostgreSQL-Source_of_Truth-green)

## How It Works

```
Claude (phone/desktop)  ──MCP──▶  JARVIS Pro (FastAPI)
     voice coach                       │
                                  PostgreSQL
                                       │
                         Browser SPA ◄──┘ (live dashboard)
```

1. **Claude** is your conversational coach — talk to it naturally about your workout
2. **JARVIS** is your visual dashboard — 4-tab SPA showing your workout in real-time
3. **PostgreSQL** is the source of truth — every set, session, and goal lives here

## Features

- 242-exercise library with muscle mapping and rehab targets
- 60+ MCP tools for Claude integration
- Real-time workout tracking with live polling
- Budget-vs-actual variance analysis
- Soft deletes + full audit log
- Demo mode for testing
- Mobile-first design (one-handed gym use)
- First-launch onboarding wizard
- Feedback system

## Quick Deploy (Railway)

### Prerequisites
- A [Railway](https://railway.com) account (Hobby plan: ~$5/mo)
- That's it. No local setup needed.

### Steps

1. **Create a new Railway project**
2. **Add a PostgreSQL database** (Railway plugin)
3. **Deploy from GitHub:**
   - New Service → GitHub Repo → select `Jarvis-Pro`
   - Railway auto-detects Python and builds with Nixpacks
4. **Set environment variables:**
   | Variable | Value |
   |---|---|
   | `MCP_SECRET` | Generate: `openssl rand -hex 16` |
   | `OPENAI_API_KEY` | *(optional — for TTS)* |
   | `FEEDBACK_WEBHOOK_URL` | *(optional — Slack/webhook)* |
5. **Deploy** — Railway provisions everything automatically
6. **Open the app URL** → onboarding wizard starts
7. **Connect Claude** → follow the in-app instructions

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Auto | Set automatically by Railway Postgres |
| `MCP_SECRET` | Recommended | Secret token for MCP endpoint auth |
| `OPENAI_API_KEY` | Optional | For text-to-speech feature |
| `FEEDBACK_WEBHOOK_URL` | Optional | Forward feedback to a webhook |
| `JARVIS_MODEL` | Optional | Claude model (default: `claude-sonnet-5`) |
| `TZ` | Optional | Timezone (default: `America/New_York`) |

## Local Development

```bash
# Clone the repo
git clone https://github.com/justind2416-hash/Jarvis-Pro.git
cd Jarvis-Pro

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your API keys

# Run (uses SQLite locally)
uvicorn main:app --reload --port 8000

# Open http://localhost:8000
```

## Multi-User Deployment

Each person gets their own Railway project with their own database:

```
Railway Account (admin)
├── Project: jarvis-alex    → alex's isolated data
├── Project: jarvis-sarah   → sarah's isolated data
└── Project: jarvis-mike    → mike's isolated data
```

To deploy for a friend:
1. Create a new Railway project
2. Add Postgres, deploy this repo
3. Set `MCP_SECRET` and your API keys
4. Share the URL — they'll onboard themselves
5. They connect Claude Desktop and start training

## Project Structure

```
├── main.py                  # FastAPI app + all routes
├── database.py              # PostgreSQL/SQLite persistence
├── mcp_server.py            # 60+ MCP tools for Claude
├── fitness.py               # Exercise library, goals, measurements
├── brain.py                 # AI integration (dynamic prompts)
├── templates/
│   ├── index.html           # Main SPA (HOME/TRAIN/STATS/PLAN)
│   ├── onboard.html         # First-launch onboarding wizard
│   └── coach.html           # Connect-your-coach instructions
├── exercise_library_data.py # 242 exercises (ships with app)
├── railway.toml             # Railway deploy config
└── JARVIS_PRO_SPEC.md       # Full build specification
```

## Built With

- **FastAPI** + **Uvicorn** — async Python web framework
- **PostgreSQL** (Railway) / **SQLite** (local dev)
- **Jinja2** — server-side templating
- **Claude** (Anthropic) — AI coaching via MCP
- **JetBrains Mono** — monospace font

## License

Private repository. Contact Justin Diaz for access.
