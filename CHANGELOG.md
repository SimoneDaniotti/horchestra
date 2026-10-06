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
- Safer setup/teardown: symlinks in `~/.local/bin` and `~/.claude/agents` are
  replaced or removed only if they point into a Horchestra plugin directory
  (yours are SKIPPED/kept); `config.toml` edits are atomic, keep permissions,
  write through a symlink, and are refused (ERROR, file unchanged) if they
  would produce invalid TOML.
- Role files: `.claude/agents/horchestra-<role>.md` is rewritten only if it
  carries the generated marker; the user skills directory follows
  `CLAUDE_CONFIG_DIR`.
- Agent names are `<folder>-<hash>-<role>`; existing plain names are kept
  until the next Herdr restart. `scan`/`roles` and other read commands never
  create team.toml; `up`/`init`/`adopt` refuse your home folder or `/` without
  `--file`. team.toml is type-checked with field-named errors; reserved Claude
  flags are rejected in `args`; `CLAUDE_CONFIG_DIR` is honoured.
- `fire` refuses `orchestrator` and closes only a pane that runs the member's
  recorded session (or carries both label and role tag, uniquely); `respawn`
  and restore never act on a pane matched by label alone, and `sync`/`status`
  never record a label-matched pane's session.
- Restore repairs each team independently (3 retries; an unreadable or
  non-UTF-8 team.toml is skipped), gives sessions shared by a copied project
  to the team whose folder the agents run in (skipping both when unclear), and closes map/overview panes only with the view title,
  no agent and an idle foreground shell. Tabs are renamed only for a lone
  running agent of that role.
- `hire` failures after pane creation keep the member for `sync`; `reopen`
  refuses while any pane still runs the team; new `horchestra-team forget`.
