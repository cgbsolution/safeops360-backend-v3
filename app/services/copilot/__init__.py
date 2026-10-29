"""SafeOps Copilot — one conversational assistant in front of every agent.

The copilot is the single chat surface the user talks to. Behind it:

  * read tools over the user's own records (scoped exactly like the screens);
  * the PTW drafting skill, which fills the New Permit form live while the
    user watches and checks it against the same rules the server enforces;
  * the five specialist agents (RCA, permit risk review, triage, HIRA, CAPA),
    which the copilot starts on the user's behalf and reports back on.

Package layout:
  ptw_draft.py  — draft schema, patch validation, deterministic rule checks
  tools.py      — tool definitions + handlers (the copilot's whole surface)
  runtime.py    — the streaming tool loop, audit row, agent bootstrap
  prompts/      — the system prompt
"""
