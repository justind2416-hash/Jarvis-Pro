# Jarvis — Phase 1 Specification

## Overview
Jarvis is a modular personal assistant with a CLI interface, web dashboard, and plugin system. Phase 1 establishes the core architecture and four built-in plugins.

## Architecture

```
jarvis/
├── jarvis/              # Python package
│   ├── __init__.py
│   ├── __main__.py      # CLI entry point (click + rich)
│   ├── config.py        # YAML + env config (pydantic-settings)
│   ├── core.py          # Plugin registry & dispatch
│   ├── morning.py       # Morning report generator
│   ├── plugins/         # Built-in plugins
│   │   ├── weather.py   # OpenWeather API
│   │   ├── news.py      # NewsAPI headlines
│   │   ├── notes.py     # Local JSON notes
│   │   └── tasks.py     # Local JSON task tracker
│   ├── utils/
│   └── web/             # FastAPI web UI
│       ├── app.py
│       └── templates/   # Jinja2 HTML templates
├── data/                # Runtime data (notes, tasks JSON)
├── docs/
├── tests/
├── config.yaml
├── requirements.txt
└── .env.example
```

## Plugins (Phase 1)

| Plugin  | Source         | Features                          |
|---------|---------------|-----------------------------------|
| weather | OpenWeather   | Current conditions by city        |
| news    | NewsAPI       | Top headlines by category         |
| notes   | Local JSON    | Add, list, pin notes              |
| tasks   | Local JSON    | Add, list, complete, clear tasks  |

## Interfaces

### CLI (`python -m jarvis`)
- `jarvis status` — show loaded plugins
- `jarvis morning` — generate morning report
- `jarvis run <plugin> <command>` — run a plugin
- `jarvis serve` — start web UI

### Web UI (`uvicorn jarvis.web.app:app`)
- `/` — dashboard with plugin status
- `/morning` — rendered morning report
- `/api/plugins` — JSON plugin list
- `/api/plugin/{name}/{cmd}` — run plugin via API
- `/api/morning` — morning report JSON

## Configuration
Settings are loaded from `config.yaml` (structure/defaults) and `.env` (secrets). Environment variables override YAML values.

## Phase 2 Roadmap
- Calendar integration (Google/Outlook)
- Scheduled morning reports via system cron
- Email digest plugin
- LLM-powered natural language commands
- Database backend (SQLite) replacing JSON files
- Authentication for web UI
