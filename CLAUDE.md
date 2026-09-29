# UChicago Teaching Assistant Project

## Who I am
Jan Mrozowski, TA for 3 courses fall 2026, teaching 1 course winter 2026.

## Credentials
- Canvas API token: ~/.uchicago_canvas_token
- Canvas base URL: https://canvas.uchicago.edu/api/v1
- Ed token: ~/.uchicago_ed_token (pending)

## Rules
- Never print tokens to screen or logs
- Never commit tokens to git
- Always verify Canvas objects with a GET after creating them
- All Canvas content stays unpublished until I explicitly approve
- Student text goes through Bedrock only, never direct Anthropic API

## Structure
course/     — per course content
ta_bot/     — Ed assistant (pending)
scripts/    — Canvas/Ed tooling
