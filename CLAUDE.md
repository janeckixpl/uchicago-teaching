# UChicago Teaching Assistant Project

## Who I am
Jan Mrozowski, TA for 3 courses fall 2026, teaching 1 course winter 2026.

## Credentials
- Canvas API token: ~/.uchicago_canvas_token
- Canvas base URL: https://canvas.uchicago.edu/api/v1
- Ed token: ~/.uchicago_ed_token

## Rules
- Never print tokens to screen or logs
- Never commit tokens to git
- Always verify Canvas objects with a GET after creating them
- All Canvas content stays unpublished until I explicitly approve
- Student text goes through Bedrock only, never direct Anthropic API

## Autumn 2026 Course IDs
- 73389 — ADSP 31014 IP03 (TA)
- 73394 — ADSP 31014 IP11 (TA)
- 73379 — (co-teacher, not TA)

## Structure
course/     — per course content
ta_bot/     — Ed assistant (pending)
scripts/    — Canvas/Ed tooling
