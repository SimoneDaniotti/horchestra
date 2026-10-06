# Recorded Herdr responses

Real responses from Herdr **0.9.3**, used by `tests/test_contract.py` to check
that Horchestra still parses what Herdr returns. CI runs them without Herdr
installed.

## How they were captured

They were recorded from a live Herdr 0.9.3 session that had several spaces and
a full Horchestra team (an orchestrator, Claude and Codex members, and a map
pane in every tab). Only read-only commands were run:

```
herdr workspace list
herdr workspace get <id>
herdr tab list --workspace <id>
herdr pane list
herdr pane list --workspace <id>
herdr pane get <id>                        # an agent pane and a map pane
herdr pane edges --pane <id>
herdr pane process-info --pane <id>        # agent, map and plain-shell panes
herdr agent list
herdr agent get <name>                     # and by pane id
herdr plugin list                          # text, and --json
herdr integration status                   # text
herdr api schema --json
```

The files under `errors/` come from the same read-only commands with ids that
do not exist (`pane get w99:p99`, `agent get no-such-agent`, ...). Herdr
prints those errors as JSON on stderr and exits with status 1, so each error
file stores the whole result: `argv`, `returncode`, `stdout` and `stderr`.

`index.json` lists each fixture with the argv that produced it, its exit
status, and whether its output is JSON or text.

## Sanitized

This repository is public, so everything personal was replaced before the
files were written here. Paths, user name, workspace, tab and pane labels,
terminal titles, agent names, pane token values, agent session ids and
terminal ids were swapped for neutral placeholders that are used consistently
across files. For example, `/home/user/projects/webshop`, the spaces `webshop`
and `docs-site`, the roles `orchestrator`, `backend`, `frontend`, `docs`,
`tests` and `reviewer`, session ids like `00000000-0000-4000-8000-000000000007`,
and terminal ids like `term_0000000000000d`. A token that points at a
parent's terminal id points at the same placeholder as that terminal.

The JSON structure is unchanged: field names, types, nesting, enum values,
pane, tab and workspace ids, and numbers such as rects and pids. The API
schema is stored exactly as Herdr printed it.

## Re-recording for a new Herdr version

1. Run the commands above against the new version into a folder **outside**
   the repository.
2. Sanitize them the same way. Keep the structure, change only the values,
   and keep `index.json` in step with the files.
3. Search the result for your user name, home path, project names, hostname,
   e-mail addresses and real session or terminal ids before copying it here.
4. Update `HERDR_VERSION` and `HERDR_CLI` in `tests/test_contract.py` from the
   new `herdr <group> help` output, then run the tests. A failing test names
   the Herdr field, command or flag that changed and the plugin code that
   depends on it.
