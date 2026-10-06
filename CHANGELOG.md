# Changelog

## 0.1.0 — 2026-10-05

First version, published as Horchestra (plugin id `horchestra`; commands
`horchestra-team` / `horchestra-tag`; `agentmap-team` / `agentmap-tag` remain
as deprecated aliases for this version). Requires Herdr 0.9.3+ and, for Claude features, Claude Code 2.1+.

- `horchestra-team reopen`: rebuild a closed space with every agent's
  conversation (Claude and Codex).
- All-spaces overview (`prefix+M`) and an in-map key list (`?`).
- Agent map pane in every agent tab: cards, top-down graph (auto when wide),
  and compact views; current-tab highlight; details box; mouse and keyboard.
- `claude --agent orchestrator` session agent that registers itself, adopts
  agents already running in the space, and hires/fires members.
- `team.toml` per project as the team's source of truth (`horchestra-team`
  `init`, `scan`, `adopt`, `hire`, `fire`, `sync`, `status`, `report`).
- Member status lines and "needs you" flags with Herdr notifications, and
  `horchestra-team message` for agent-to-agent messages by role.
- Startup hook that repairs teams after a Herdr restart using recorded
  agent session ids.
- `horchestra.setup` / `horchestra.teardown` for the machine-level pieces.
- Role profiles (`.orchestra/roles/<role>/`): per-member instructions and
  role-only skills, plus `only_skills` (allowlist), `uses_skills` and
  `deny_skills` in team.toml.
- `horchestra-team respawn`: apply a profile to a running Claude agent without
  losing its conversation; the startup hook re-applies profiles after a
  Herdr restart.
- Tabs and Claude conversations named after each role (`--name`), kept in
  step on hire, adopt, respawn and restart.
