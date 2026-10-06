#!/usr/bin/env python3
"""Install or remove the machine-level parts of Horchestra.

`herdr plugin install` only registers the plugin. This adds what lives
outside the plugin directory, and `teardown` removes exactly that:

- `horchestra-team` and `horchestra-tag` symlinks in ~/.local/bin (and, only
  where a pre-0.1 install linked them, the deprecated `agentmap-*` aliases)
- the `orchestrator` Claude Code session agent in ~/.claude/agents
- `prefix+m` (maps in this space) and `prefix+shift+m` (all-spaces
  overview), in a marked block of Herdr's config.toml (only keys that are free)

It never overwrites files it did not create, and only suggests (never
writes) personal settings such as notifications.

    python3 install.py [install|teardown]    or the horchestra.setup / .teardown actions
"""

import os
import re
import subprocess
import sys
import tempfile

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

ROOT = os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(os.path.realpath(__file__))
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
BIN_DIR = os.path.expanduser("~/.local/bin")
DEFAULT_AGENTS_DIR = os.path.expanduser("~/.claude/agents")
# Claude Code reads agents from $CLAUDE_CONFIG_DIR/agents when that is set.
AGENTS_DIR = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "agents")
# (key, plugin action, description, older action names that count as bound)
BINDINGS = [
    ("prefix+m", "horchestra.toggle", "toggle agent maps", ("agent-map.toggle",)),
    ("prefix+shift+m", "horchestra.overview", "all-spaces agent map", ()),
]
BLOCK_START = "# >>> horchestra (managed by horchestra.setup; remove with horchestra.teardown)"
BLOCK_END = "# <<< horchestra"
# Any managed block, including ones written under the pre-0.1 names.
BLOCK_RE = re.compile(r"# >>> (?:horchestra|herdr-orchestra)\b.*?# <<< (?:horchestra|herdr-orchestra)[^\n]*\n?", re.S)

# (source relative to the plugin root, destination)
LINKS = [
    ("bin/horchestra-team", os.path.join(BIN_DIR, "horchestra-team")),
    ("bin/horchestra-tag", os.path.join(BIN_DIR, "horchestra-tag")),
    ("agents/orchestrator.md", os.path.join(AGENTS_DIR, "orchestrator.md")),
]
# Deprecated pre-0.1 command names: only kept up to date where an older
# install already linked them (running agents may still call them).
LEGACY_LINKS = [
    ("bin/agentmap-team", os.path.join(BIN_DIR, "agentmap-team")),
    ("bin/agentmap-tag", os.path.join(BIN_DIR, "agentmap-tag")),
]
if os.path.realpath(AGENTS_DIR) != os.path.realpath(DEFAULT_AGENTS_DIR):
    # Set up before CLAUDE_CONFIG_DIR was set: teardown still removes that link.
    LEGACY_LINKS.append(("agents/orchestrator.md", os.path.join(DEFAULT_AGENTS_DIR, "orchestrator.md")))


def read(path):
    if not os.path.exists(path):
        return ""
    with open(path) as fh:
        return fh.read()


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr", "config.toml")


# ---- symlinks -------------------------------------------------------------


# Plugin ids whose checkouts we treat as ours (current and pre-0.1 name).
PLUGIN_IDS = ("horchestra", "agent-map")
# The one pre-0.1 development layout recognised without a manifest: the old
# checkout folder is gone, so only a dangling link with this exact folder
# name and file name counts.
LEGACY_DANGLING = {"agents/orchestrator.md": ["herdr-agent-map/orchestrator.md"]}


def manifest_id(root):
    """The `id` in <root>/herdr-plugin.toml, or None if unreadable."""
    path = os.path.join(root, "herdr-plugin.toml")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if tomllib is not None:
        try:
            value = tomllib.loads(data.decode("utf-8")).get("id")
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            return None
        return value if isinstance(value, str) else None
    match = re.search(r'^id\s*=\s*"([^"]*)"', data.decode("utf-8", "replace"), re.M)
    return match.group(1) if match else None


def plugin_root_of(target, source_rel):
    """The directory `target` would be the `source_rel` file of, or None."""
    parts = source_rel.split("/")
    path = os.path.normpath(target)
    for part in reversed(parts):
        head, tail = os.path.split(path)
        if tail != part:
            return None
        path = head
    return path


def is_plugin_root(root):
    return bool(root) and manifest_id(root) in PLUGIN_IDS


def is_ours(dest, source_rel):
    """A symlink to <plugin_root>/<source_rel> of some copy of this plugin.

    Ownership is strict because setup replaces and teardown deletes what
    this returns True for: a user's own link that merely ends in the same
    file name (dotfiles, another tool) must never count. The target's
    directory must hold a herdr-plugin.toml with our id, checked both as
    written and fully resolved. A dangling link counts only through that
    manifest (its file was dropped from a still-present checkout) or the
    exact pre-0.1 `herdr-agent-map` dev layout.
    """
    if not os.path.islink(dest):
        return False
    target = os.path.join(os.path.dirname(dest), os.readlink(dest))
    candidates = [target]
    if os.path.exists(target):
        candidates.append(os.path.realpath(target))
    if any(is_plugin_root(plugin_root_of(c, source_rel)) for c in candidates):
        return True
    if os.path.exists(target):
        return False
    norm = os.path.normpath(target).replace(os.sep, "/")
    return any(norm.endswith("/" + legacy) for legacy in LEGACY_DANGLING.get(source_rel, []))


def install_links(report):
    legacy = [(src, dest) for src, dest in LEGACY_LINKS if is_ours(dest, src)]
    for source_rel, dest in LINKS + legacy:
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
    for source_rel, dest in LINKS + LEGACY_LINKS:
        if is_ours(dest, source_rel):
            os.remove(dest)
            report(f"removed  {dest}")
        elif os.path.lexists(dest):
            report(f"kept     {dest} (not created by setup)")


# ---- config block -----------------------------------------------------------


def managed_block(bindings=None):
    lines = [BLOCK_START]
    for key, command, description, _legacy in bindings or BINDINGS:
        lines += ["[[keys.command]]", f'key = "{key}"', 'type = "plugin_action"',
                  f'command = "{command}"', f'description = "{description}"']
    return "\n".join(lines + [BLOCK_END])


def strip_block(text):
    return BLOCK_RE.sub("", text)


def active_lines(text):
    return [l.split("#", 1)[0].strip() for l in strip_block(text).splitlines()]


def key_in_use(text, key):
    return any(re.fullmatch(r'key\s*=\s*"' + re.escape(key) + '"', l) or
               re.search(r'=\s*"' + re.escape(key) + '"', l) for l in active_lines(text))


def command_bound(text, commands):
    pattern = r'command\s*=\s*"(' + "|".join(map(re.escape, commands)) + ')"'
    return any(re.fullmatch(pattern, l) for l in active_lines(text))


def add_block(text):
    """Return (new_text, message). Pure, for tests.

    Rebuilds the managed block from scratch, skipping any binding whose key
    is taken or whose action the user already bound outside the block.
    """
    base = strip_block(text)
    chosen, notes = [], []
    for key, command, description, legacy in BINDINGS:
        if command_bound(base, (command,) + legacy):
            notes.append(f"ok       {command} is already bound in your config")
        elif key_in_use(base, key):
            notes.append(f"SKIPPED  {key} is already used in your config; bind {command} "
                         "to another key yourself (see README)")
        else:
            chosen.append((key, command, description, legacy))
    body = base.rstrip("\n")
    new = ((body + "\n\n") if body else "") + managed_block(chosen) + "\n" if chosen else base
    if new == text:
        notes.append("ok       key bindings already managed by setup")
    elif BLOCK_RE.search(text):
        notes.append("updated  key bindings: " + ", ".join(f"{k} -> {d}" for k, _, d, _ in chosen))
    elif chosen:
        notes.append("added    " + ", ".join(f"{k} -> {d}" for k, _, d, _ in chosen))
    return new, "\n".join(notes)


def install_binding(report):
    path = config_path()
    text = read(path)
    new, message = add_block(text)
    if new != text:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not write_config(path, text, new, report):
            return
        reload_config(report)
    report(message)


def remove_binding(report):
    path = config_path()
    text = read(path)
    new = strip_block(text)
    if new != text:
        if not write_config(path, text, new, report):
            return
        reload_config(report)
        report(f"removed  key bindings from {path}")


def parses_as_toml(text):
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    return True


def write_config(path, old, new, report):
    """Replace Herdr's config atomically; return False if nothing was written.

    The config is the user's, so a crash or full disk must never leave it
    truncated: write a sibling temp file, fsync it, keep the original mode
    and os.replace it in. A symlinked config (dotfiles) is written through
    to its target so the link survives. If our edit would turn valid TOML
    into invalid TOML, refuse and leave the file untouched.
    """
    if tomllib is not None and parses_as_toml(old) and not parses_as_toml(new):
        report(f"ERROR    not writing {path}: the edited config would not be valid TOML; "
               "your config was left untouched")
        return False
    path = os.path.realpath(path)
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".config.toml.", suffix=".tmp", dir=os.path.dirname(path))
        with os.fdopen(fd, "w") as fh:
            fh.write(new)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as err:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        report(f"ERROR    could not write {path}: {err}")
        return False
    return True


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
