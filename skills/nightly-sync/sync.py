#!/usr/bin/env python3
"""
Nightly Workout Sync Script
Runs at 10 PM to sync JARVIS workout data with the Claude Desktop project.
Can be triggered by a scheduled task or run manually.
"""

import json
import sys
import os
from datetime import date, timedelta
from pathlib import Path

# Add parent directories to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from database import (
    get_recent_sessions,
    get_planned_workout,
    get_workout_compliance,
    mark_planned_workout_skipped,
    get_planned_schedule,
    save_planned_schedule,
    get_bodyweight_history,
)

try:
    from database import search_chat_history
except ImportError:
    search_chat_history = None


def run_nightly_sync():
    """Main sync logic."""
    today = date.today().isoformat()
    print(f"[Nightly Sync] Running for {today}")
    
    # 1. Check today's sessions
    sessions = get_recent_sessions(limit=5)
    today_sessions = [s for s in sessions if s.get("date") == today]
    
    # 2. Check what was planned
    planned = get_planned_workout(today)
    
    # 3. Get compliance
    compliance = get_workout_compliance(14)
    
    # 4. Get bodyweight trend
    bw = get_bodyweight_history(7)
    
    # 5. Check for pain/issues in chat
    issues = []
    if search_chat_history:
        try:
            pain_results = search_chat_history(
                query="pain OR hurt OR sore OR skip OR modify",
                days=1
            )
            if pain_results:
                issues = pain_results[:5]  # Last 5 relevant messages
        except Exception as e:
            print(f"[Nightly Sync] Chat search error: {e}")
    
    # 6. Evaluate
    report = {
        "date": today,
        "worked_out": len(today_sessions) > 0,
        "planned_program": planned["program_name"] if planned else None,
        "planned_status": planned["status"] if planned else "no_plan",
        "sessions_today": len(today_sessions),
        "compliance_14d": compliance,
        "bodyweight_trend": bw[:3] if bw else [],
        "issues_flagged": len(issues),
        "issue_snippets": [str(i)[:100] for i in issues],
    }
    
    # 7. If planned workout exists and wasn't completed, mark as skipped
    if planned and planned["status"] == "pending" and not today_sessions:
        mark_planned_workout_skipped(today, "No session logged")
        report["action"] = "marked_skipped"
        print(f"[Nightly Sync] Marked {today} as skipped — no session logged")
    elif today_sessions:
        report["action"] = "completed"
        # Log what was done
        for s in today_sessions:
            print(f"[Nightly Sync] Session: {s.get('program')} — {s.get('total_sets')} sets, {s.get('duration_min') or '?'} min")
    else:
        report["action"] = "no_plan_no_session"
    
    # 8. Check if schedule needs extending (always keep 14 days planned)
    upcoming = get_planned_schedule(14)
    report["days_planned_ahead"] = len(upcoming)
    
    if len(upcoming) < 7:
        report["needs_replan"] = True
        print(f"[Nightly Sync] WARNING: Only {len(upcoming)} days planned ahead — needs replan")
    else:
        report["needs_replan"] = False
    
    print(f"\n[Nightly Sync] Report:")
    print(json.dumps(report, indent=2))
    
    return report


if __name__ == "__main__":
    run_nightly_sync()
