# agentic-trader — rules for Claude Code
- This system trades REAL money. Stay in paper mode. Never set
  config.yaml execution_mode to "live" — the human does that after review.
- risk_guard.py is the source of truth for all limits. Never weaken a
  check or move enforcement into prompts.
- Never commit, print, or read .env. Never run git push.
- After every change, run `python risk_guard.py` and `python orchestrator.py`
  and keep them green. Pause and show diffs before moving to the next task.
