# Herdr Orchestra

Orchestrator-led agent teams for [Herdr](https://herdr.dev), with a live
agent map in every tab.

Start one orchestrator in a space. It adopts the agents already running
there, hires new ones when the work needs them, and keeps the team in a
`team.toml` you can read and edit. Every agent tab gets a map of the team;
the agents in the tab you are looking at are highlighted.

```text
 AGENT MAP            [cards]      zoomed map ([graph])
 webshop
 !1 ●2 ○1                                ╭────────────────╮
──────────────────────────────           │ ✻ orchestrator │
╭────────────────────────────╮           │   ● working    │
│ ✻ orchestrator   ● working │           ╰────────┬───────╯
│ coordinates the team       │             ┌──────┴────────┐
╰┬───────────────────────────╯      ╭──────┴─────╮   ╭─────┴────╮
 ├─╭─────────────────────────╮      │ ◆ research │   │ ✻ slides │
▌│ │ ✻ slides         ○ idle │      │ ● working  │   │  ○ idle  │
▌│ │ deck 40%: 6/15 slides   │      ╰──────┬─────╯   ╰──────────╯
▌│ ╰─────────────────────────╯      ╭──────┴──────╮
 └─╭─────────────────────────╮      │    ✻ QA     │
   │ ◆ research    ● working │      │ ! needs you │
   │ Researches rate limits  │      ╰─────────────╯
   ╰─────────────────────────╯
```

## Requirements

- Herdr 0.9 or newer, on macOS or Linux
- Python 3.11+ (standard library only; nothing to install)
- Claude Code for the `orchestrator` session agent; members can be any agent
  Herdr can start (`claude`, `codex`, `gemini`, `pi`, …)

## Install

```bash
herdr plugin install SimoneDaniotti/herdr-orchestra
herdr plugin action invoke agent-map.setup
```

For a private repository, `plugin install` clones with your own git
credentials, so you need read access to the repo.

`setup` adds the pieces that live outside the plugin directory, and prints
what it did:

| What | Where |
| --- | --- |
| `agentmap-team`, `agentmap-tag` commands | symlinks in `~/.local/bin` |
| `orchestrator` Claude Code session agent | symlink in `~/.claude/agents` |
| `prefix+m` toggles the maps | marked block in `~/.config/herdr/config.toml`, only if the key is free |

It never overwrites files it did not create. It also checks that Herdr's
Claude/Codex integrations are current (needed to resume agents after a Herdr
restart) and suggests turning on notifications if they are off.

## Quick start

In any space, open a tab and run:

```bash
claude --agent orchestrator
```

Then tell it what you want, or just "set up the team". On its first turn it:

1. registers itself as the space's orchestrator and creates `team.toml` at
   the project's git root (`agentmap-team init`)
2. finds agents already running in the space (`agentmap-team scan`) and adopts
   them, naming roles after their tabs, without restarting or messaging them
3. opens a map in every agent tab and summarizes the team

From then on it hires and fires members itself. You can drive the same
commands by hand.

## The map

`prefix+m` shows or hides a map in every tab of the space that runs an agent.

| Key / mouse | Action |
| --- | --- |
| `↑` `↓` / `j` `k` / wheel | move selection |
| `←` `→` / `h` `l` | go to parent / first child |
| `Enter` / double-click | focus that agent's pane |
| `Space` | fold or unfold a subtree |
| `v` | cycle views: auto → cards → graph → compact |
| `d` | show or hide the details box |
| `r` / `q` | refresh / close this map |

- **cards**: one card per agent: icon, status, and a detail line (a
  needs-you question, the agent's reported status, or its task).
- **graph**: top-down tree. *auto* uses it whenever the pane is wide enough,
  e.g. when you zoom the map pane with `prefix+z`.
- **compact**: one line per agent.
- `▌` (cyan) marks agents in the map's own tab.
- The **details box** shows the selected agent's kind, state and for how
  long, tab, reported status, task, and the last thing it said.

Icons: `✻` Claude · `◆` Codex · `✦` Gemini · `π` Pi.
Status: `●` working · `▲` blocked · `✓` done · `○` idle · `!` needs you.

## Teams

`team.toml` is the team's source of truth. It starts with only the
orchestrator and records each agent's session id so the team can be found
again after a Herdr restart:

```toml
default_kind = "claude"

[orchestrator]
kind = "claude"
session = "900a16f3-…"
# brief = "Optional standing goal for the orchestrator"

[[member]]
role = "slides"
kind = "claude"
task = "Designs the quarterly review deck in web/checkout/"
session = "a3ff62c9-…"
# reports_to = "research"   # nest under another member
# cwd = "presentation"      # relative to team.toml
# args = ["--model", "sonnet"]
```

| Command | What it does |
| --- | --- |
| `agentmap-team init` | register the calling agent as orchestrator; open maps |
| `agentmap-team scan` | list running agents in the space that are not on the team |
| `agentmap-team adopt <pane> --role R [--task T]` | add a running agent, unchanged |
| `agentmap-team hire R --kind K --task T [--profile P] [--only-skill S] [--uses-skill S] [--deny-skill S]` | add a member: own tab named after the role, start the agent with its role profile, send its brief |
| `agentmap-team fire R` | remove a member and close its pane |
| `agentmap-team status` | show the team, with each member's latest reported line |
| `agentmap-team message R "text"` | send a message to a team agent by role (e.g. `orchestrator`); queued if it is busy |
| `agentmap-team sync` | apply a hand-edited team.toml: start missing members, repair names and map links |
| `agentmap-team roles` | list role profiles in `.orchestra/roles` and who uses them |
| `agentmap-team respawn R \| --all` | restart a Claude agent in place with its current profile, keeping its conversation |
| `agentmap-team up` | start `claude --agent orchestrator` in the current pane (also the `agent-map.team-up` action) |

Names stay in step with roles:

- each hired member gets its own tab named after its role, and every
  `agentmap-team` command renames an agent's tab to its role when it is the
  only agent in that tab (set `name_tabs = false` in team.toml to turn off)
- Claude members and the orchestrator are started with `--name <role>`, so the
  conversation (prompt box, `/resume` picker, terminal title) has the same
  name; `respawn` and the restart repair apply it again
- panes are labelled `team:<role>`, and Herdr agent names are
  `<project>-<role>` (e.g. `webshop-slides`) for
  `herdr agent prompt <name> "…"`

### Role profiles: each member's own instructions and skills

Give a role its own context with a folder in the project:

```text
.orchestra/roles/slides/
├── ROLE.md                      # the member's own instructions
├── CLAUDE.md                    # optional extra memory for this role
└── .claude/skills/deck-style/   # skills only this role gets
```

`hire slides` picks the folder up automatically (or `--profile <folder>`),
and team.toml can add skills to use or block:

```toml
deny_skills = ["legacy:*"]                 # blocked for every hired member

[[member]]
role = "slides"
only_skills = ["slide-kit"]         # allowlist (role-folder skills stay allowed)
# uses_skills = ["slide-kit"]       # or: skills it must use, others still allowed
# deny_skills = ["media-kit*"]         # block specific names or patterns
```

`only_skills` puts "use only these skills" in the member's instructions and
blocks every other skill found in `~/.claude/skills` and the project's
`.claude/skills`. Skills from Claude Code plugins and built-ins cannot be
listed from disk, so for those only the instruction applies.

For a Claude member, hiring generates a session agent
`.claude/agents/orchestra-<role>.md` (team context + ROLE.md + skills to use)
and starts it with `claude --agent orchestra-<role> --settings <deny rules>
--add-dir <profile folder>`. The member still sees the project's own
CLAUDE.md and skills. Other agent kinds get the same instructions in their
first message.

What is and is not guaranteed (tested with Claude Code 2.1):

| | |
| --- | --- |
| Role instructions | in the member's system prompt; kept by `claude --resume` |
| Role-only skills | available, from the profile folder |
| Denied skills | cannot be used (the call is refused), but are still listed |
| `only_skills` | instruction for all skills; hard block for skills on disk; plugin and built-in skills rely on the instruction |
| After a Herdr restart | the startup hook re-applies each member's profile (see below) |

Check a member from its pane with `/skills` and `/context`; `agentmap-team
status`, `agentmap-team roles`, and the map's details box show each member's
profile.

**Applying a profile to a running agent.** `agentmap-team respawn <role>`
quits the member's Claude session and restarts it in the same pane with
`claude --resume <its session> --system-prompt-snapshot off` plus its profile
flags. It keeps its whole conversation and gains the role instructions,
skills, and deny rules. Use it after adopting an agent, after editing a
ROLE.md, or with `--all` for the whole team. Wait until the member is idle
(or pass `--force`). Claude only; it takes a few seconds per member.

### Status lines and "needs you"

Members keep their own line in the map:

```bash
agentmap-team report "tests 3/5 passing"
agentmap-team report --needs-you "Use v1 or v2 API docs?"
agentmap-team report --clear
```

`report` only updates the map (and `status`); to tell the orchestrator
something, a member runs `agentmap-team message orchestrator "…"`, which
arrives in the orchestrator's conversation as
`[agent-map] message from <role>: …`. Hired and respawned members have both
commands in their instructions; the orchestrator tells adopted ones. A needs-you report shows `!` until the agent works again and sends a
Herdr notification. Herdr's own "agent finished / needs input" alerts cover
the rest. Both follow `[ui.toast]` in Herdr's config, which is off by default:

```toml
[ui.toast]
delivery = "system"   # or "herdr" for in-app toasts
```

### After a Herdr restart

Herdr resumes the agents' conversations itself when its Claude/Codex
integrations are current. The plugin's startup hook then:

- matches resumed agents to roles by session id and restores names, labels
  and map links
- sends a resumed orchestrator a short note listing who came back (not after
  a live handoff, where nothing restarted)
- starts a fresh orchestrator in its pane if its conversation could not be
  resumed
- reopens map panes, which come back as idle shells

- respawns every Claude member launched with a profile, because Herdr's plain
  `claude --resume` would bring back the instructions recorded when the
  conversation began; the orchestrator is refreshed from the current
  `orchestrator.md` the same way

Members whose conversation did not resume are reported to the orchestrator,
never restarted automatically, so nothing runs twice. Results are in
`herdr plugin log list --plugin agent-map`.

## Configuration

| Setting | Where | Default |
| --- | --- | --- |
| Map width | a number in `$(herdr plugin config-dir agent-map)/width` | 32 |
| Map toggle key | the managed block in Herdr's config, or bind `agent-map.toggle` yourself | `prefix+m` |
| Team-start key | bind the `agent-map.team-up` action | none |
| Default member agent | `default_kind` in team.toml | `claude` |
| Plugin state | `AGENTMAP_STATE_DIR` | `~/.local/state/herdr-agent-map` |

## Uninstall

```bash
herdr plugin action invoke agent-map.teardown
herdr plugin uninstall agent-map
```

`teardown` removes only what `setup` added. `team.toml` files in your
projects and the state directory are left in place.

## How it works

- Uses only Herdr's public surfaces: the `herdr` CLI and the documented event
  socket. No private TUI protocol, no screen scraping for state.
- Events only trigger a fresh read, and unknown fields are ignored, so new
  Herdr releases can add fields or events without breaking it.
- The map reconnects with backoff after server restarts and updates, and
  falls back to polling if subscriptions are rejected.
- Agent relationships are pane tokens (`agentmap_parent`, `agentmap_role`,
  …) shown by the map; `team.toml` and session ids make them durable.

## Security

The plugin runs as your user. `setup` writes symlinks into `~/.local/bin` and
`~/.claude/agents` and a marked block into Herdr's config; the startup hook
and `hire` start agents on your machine. Read `install.py` and
`herdr-plugin.toml` before installing, as with any Herdr plugin.

## Known limits

- An agent restarted before it ever replied has no saved conversation and
  cannot be resumed (a fresh orchestrator is started; such members are
  reported).
- Each map takes about 32 columns in its tab.
- `hire` and `fire` rewrite team.toml, dropping comments added by hand.
- Files in `~/.claude/agents` are also offered as subagents in every Claude
  session; the orchestrator's description asks Claude not to use it that way.
- Manually tagged panes (`agentmap-tag`) lose their tags on a Herdr restart.
- Hiring a Claude member writes `.claude/agents/orchestra-<role>.md` into the
  project; commit it or add `.claude/agents/orchestra-*.md` to `.gitignore`.

## Development

```bash
git clone git@github.com:SimoneDaniotti/herdr-orchestra.git
cd herdr-orchestra
herdr plugin link .            # run your working copy
python3 install.py install     # same as the setup action
python3 -m unittest discover -s tests -t .
```

## License

MIT
