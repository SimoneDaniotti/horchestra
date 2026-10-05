#!/usr/bin/env python3
"""Install or remove the machine-level parts of Horchestra.

`herdr plugin install` only registers the plugin. This adds what lives
outside the plugin directory, and `teardown` removes exactly that:

- `horchestra-team` and `horchestra-tag` symlinks in ~/.local/bin (plus the
  deprecated `agentmap-team` / `agentmap-tag` aliases, kept for one version)
- the `orchestrator` Claude Code session agent in ~/.claude/agents
- a `prefix+m` binding for the map toggle, in a marked block of Herdr's
  config.toml (only if that key is free)

It never overwrites files it did not create, and only suggests (never
writes) personal settings such as notifications.

    python3 install.py [install|teardown]    or the horchestra.setup / .teardown actions
"""

import os
import re
import subprocess
import sys

ROOT = os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(os.path.realpath(__file__))
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
BIN_DIR = os.path.expanduser("~/.local/bin")
AGENTS_DIR = os.path.expanduser("~/.claude/agents")
TOGGLE_KEY = "prefix+m"
TOGGLE_COMMAND = "horchestra.toggle"
BLOCK_START = "# >>> horchestra (managed by horchestra.setup; remove with horchestra.teardown)"
BLOCK_END = "# <<< horchestra"
# Any managed block, including ones written under the pre-0.1 names.
BLOCK_RE = re.compile(r"# >>> (?:horchestra|herdr-orchestra)\b.*?# <<< (?:horchestra|herdr-orchestra)[^\n]*\n?", re.S)
LEGACY_TOGGLE_COMMANDS = ("agent-map.toggle",)

# (source relative to the plugin root, destination)
LINKS = [
    ("bin/horchestra-team", os.path.join(BIN_DIR, "horchestra-team")),
    ("bin/horchestra-tag", os.path.join(BIN_DIR, "horchestra-tag")),
    ("bin/agentmap-team", os.path.join(BIN_DIR, "agentmap-team")),  # deprecated alias
    ("bin/agentmap-tag", os.path.join(BIN_DIR, "agentmap-tag")),  # deprecated alias
    ("agents/orchestrator.md", os.path.join(AGENTS_DIR, "orchestrator.md")),
]


def read(path):
    if not os.path.exists(path):
        return ""
    with open(path) as fh:
        return fh.read()


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr", "config.toml")


# ---- symlinks -------------------------------------------------------------


# Paths used by pre-0.1 development layouts, recognised as ours on upgrade.
LEGACY_SUFFIXES = {"agents/orchestrator.md": ["herdr-agent-map/orchestrator.md"]}


def is_ours(dest, source_rel):
    """A symlink into some copy of this plugin (e.g. an older checkout)."""
    if not os.path.islink(dest):
        return False
    target = os.readlink(dest).replace(os.sep, "/")
    suffixes = [source_rel] + LEGACY_SUFFIXES.get(source_rel, [])
    return any(target.endswith("/" + suffix) for suffix in suffixes)


def install_links(report):
    for source_rel, dest in LINKS:
        source = os.path.join(ROOT, source_rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.islink(dest) and os.readlink(dest) == source:
            report(f"ok       {dest}")
            continue
        if os.path.lexists(dest) and not is_ours(dest, source_rel):
            report(f"SKIPPED  {dest} exists and is not ours; remove it and re-run setup")
            continue
        if os.path.lexists(dest):
            os.remove(dest)
        os.symlink(source, dest)
        report(f"linked   {dest} -> {source}")
    if BIN_DIR not in os.environ.get("PATH", "").split(os.pathsep):
        report(f"NOTE     {BIN_DIR} is not on your PATH; add it so agents can run horchestra-team")


def remove_links(report):
    for source_rel, dest in LINKS:
        if is_ours(dest, source_rel):
            os.remove(dest)
            report(f"removed  {dest}")
        elif os.path.lexists(dest):
            report(f"kept     {dest} (not created by setup)")


# ---- config block -----------------------------------------------------------


def managed_block():
    return "\n".join([
        BLOCK_START,
        "[[keys.command]]",
        f'key = "{TOGGLE_KEY}"',
        'type = "plugin_action"',
        f'command = "{TOGGLE_COMMAND}"',
        'description = "toggle agent map"',
        BLOCK_END,
    ])


def strip_block(text):
    return BLOCK_RE.sub("", text)


def active_lines(text):
    return [l.split("#", 1)[0].strip() for l in strip_block(text).splitlines()]


def key_in_use(text, key):
    return any(re.fullmatch(r'key\s*=\s*"' + re.escape(key) + '"', l) or
               re.search(r'=\s*"' + re.escape(key) + '"', l) for l in active_lines(text))


def toggle_bound_elsewhere(text):
    commands = (TOGGLE_COMMAND,) + LEGACY_TOGGLE_COMMANDS
    return any(re.fullmatch(r'command\s*=\s*"(' + "|".join(map(re.escape, commands)) + ')"', l)
               for l in active_lines(text))


def add_block(text):
    """Return (new_text, message). Pure, for tests."""
    if managed_block() in text:
        return text, f"ok       {TOGGLE_KEY} binding already managed by setup"
    if BLOCK_RE.search(text):
        # A block from an older version: rewrite it with the current names.
        stripped = strip_block(text)
        new, _ = add_block(stripped)
        return new, f"updated  {TOGGLE_KEY} binding to the current plugin name"
    if toggle_bound_elsewhere(text):
        return text, "ok       the map toggle is already bound in your config"
    if key_in_use(text, TOGGLE_KEY):
        return text, (f"SKIPPED  {TOGGLE_KEY} is already used in your config; bind "
                      f"{TOGGLE_COMMAND} to another key yourself (see README)")
    sep = "" if text.endswith("\n\n") or not text else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + managed_block() + "\n", f"added    {TOGGLE_KEY} -> map toggle"


def install_binding(report):
    path = config_path()
    text = read(path)
    new, message = add_block(text)
    if new != text:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(new)
        reload_config(report)
    report(message)


def remove_binding(report):
    path = config_path()
    text = read(path)
    new = strip_block(text)
    if new != text:
        with open(path, "w") as fh:
            fh.write(new)
        reload_config(report)
        report(f"removed  {TOGGLE_KEY} binding from {path}")


def reload_config(report):
    try:
        subprocess.run([HERDR, "server", "reload-config"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        report("NOTE     could not reload Herdr's config; run `herdr server reload-config`")


# ---- advice -------------------------------------------------------------------


def check_integrations(report):
    try:
        out = subprocess.run([HERDR, "integration", "status"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for line in out.splitlines():
        name = line.split(":", 1)[0].strip()
        if name in ("claude", "codex") and ("outdated" in line or "not installed" in line):
            report(f"ADVICE   {line.strip()} -> run `herdr integration install {name}` "
                   "so Herdr can resume its sessions after a restart")


def check_notifications(report):
    path = config_path()
    text = read(path)
    if not any(re.fullmatch(r'delivery\s*=\s*"(herdr|terminal|system)"', l) for l in active_lines(text)):
        report('ADVICE   notifications are off; for "agent finished / needs you" alerts add '
               'delivery = "system" (or "herdr") under [ui.toast] in ' + path)


# ---- main -------------------------------------------------------------------


def main(argv):
    command = argv[1] if len(argv) > 1 else "install"
    report = print
    if command == "install":
        install_links(report)
        install_binding(report)
        check_integrations(report)
        check_notifications(report)
        report("done. Start a team with `claude --agent orchestrator` inside a Herdr pane.")
    elif command == "teardown":
        remove_links(report)
        remove_binding(report)
        report("done. team.toml files in your projects and ~/.local/state/horchestra "
               "were left in place; `herdr plugin uninstall horchestra` removes the plugin.")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
