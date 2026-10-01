# Nightly Workout Sync

This skill runs as a scheduled task each evening to sync workout data between the JARVIS app and the Claude Desktop "40 Strong 40 Fit 40 Fast" project.

## What it does

1. **Pulls today's activity** from the JARVIS database via MCP:
   - Session data (exercises completed, weights, reps, duration)
   - Any skipped or modified exercises
   - Pain reports or form notes from chat history
   - Bodyweight if logged today

2. **Checks compliance** against the planned schedule:
   - Was today's planned workout completed?
   - If not, marks it as skipped
   - Calculates rolling compliance stats

3. **Evaluates and adjusts** the upcoming schedule:
   - If a workout was skipped, decides whether to push it to tomorrow or drop it
   - If the user reported pain or difficulty, may adjust weights or swap exercises
   - If the user is ahead of schedule, may add progressive overload

4. **Pushes the updated schedule** back to the JARVIS database via `set_planned_program`

## MCP Tools Used

- `get_session_summaries` — what happened today
- `get_today_workout` — what was planned
- `get_compliance` — rolling stats
- `search_chat_history` — pain reports, form cues
- `get_bodyweight_history` — weight trends
- `mark_workout_skipped` — if no session today
- `set_planned_program` — push adjusted schedule
- `get_planned_schedule` — current upcoming plan

## Schedule

Runs nightly at 10:00 PM local time.

## Instructions

When this skill runs, follow these steps:

### Step 1: Gather Today's Data
Call these MCP tools on the JARVIS server:
```
get_session_summaries(days=1)  — did Justin work out today?
get_today_workout()            — what was planned?
get_compliance(days=14)        — rolling compliance
search_chat_history(query="pain OR hurt OR sore OR skip OR modify", days=1)  — any issues?
get_bodyweight_history(limit=7) — recent weight trend
get_deviations(days=1)             — any planned vs actual deviations today
```

### Step 2: Evaluate
- Check deviations: if the athlete modified weights, reps, or exercises from the plan, note why and factor into future programming
Compare what was planned vs what happened:
- If planned workout was completed: note any weight/rep changes from the prescription
- If planned workout was NOT completed: mark it skipped and decide what to do with it
- If pain was reported: flag the affected muscle group for the upcoming schedule
- If user exceeded prescribed weights/reps: consider progressive overload

### Step 3: Adjust the Schedule
Based on evaluation:
- Reorder upcoming workouts if needed (e.g., push skipped workout to next available day)
- Adjust weights based on performance (if user hit all reps easily, suggest +5 lb next time)
- Add rest days if fatigue/pain signals detected
- Keep the 2-week rolling window — always have 14 days planned ahead

### Step 4: Push Updated Schedule
Call `set_planned_program` with the adjusted schedule for the next 14 days.

### Step 5: Summary
Log a brief summary of what changed and why. This gets stored in chat history for reference.
