# Changelog

## 0.1.0 — unreleased

First version.

- Agent map pane in every agent tab: cards, top-down graph (auto when wide),
  and compact views; current-tab highlight; details box; mouse and keyboard.
- `claude --agent orchestrator` session agent that registers itself, adopts
  agents already running in the space, and hires/fires members.
- `team.toml` per project as the team's source of truth (`agentmap-team`
  `init`, `scan`, `adopt`, `hire`, `fire`, `sync`, `status`, `report`).
- Member status lines and "needs you" flags with Herdr notifications.
- Startup hook that repairs teams after a Herdr restart using recorded
  agent session ids.
- `agent-map.setup` / `agent-map.teardown` for the machine-level pieces.
- Role profiles (`.orchestra/roles/<role>/`): per-member instructions and
  role-only skills, plus `only_skills` (allowlist), `uses_skills` and
  `deny_skills` in team.toml.
- `agentmap-team respawn`: apply a profile to a running Claude agent without
  losing its conversation; the startup hook re-applies profiles after a
  Herdr restart.
- Tabs and Claude conversations named after each role (`--name`), kept in
  step on hire, adopt, respawn and restart.
