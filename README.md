# aide-memoire - Dashboard Aide-Mémoire plugin

An **Aide-Mémoire** tab for the Hermes dashboard: it lists the skills and
plugins authored on this machine and excludes everything that ships with stock
Hermes. Scanned live from the filesystem, so it never goes stale.

Every excluded item carries the reasons it was excluded, and every verdict can
be overridden by hand, so the list is auditable rather than a black box.

## Install

```bash
hermes plugins install https://github.com/lymeswold/aide-memoire-dashboard-plugin
```

Then enable it and restart the dashboard (backend routes mount at startup):

```bash
hermes plugins enable aide-memoire        # adds it to plugins.enabled
systemctl --user restart hermes-dashboard.service   # or your usual restart
```

Then open the **Aide-Mémoire** tab.

Works on Linux, macOS and Windows. On Windows, restart the dashboard the way
your install does it (services console, Task Manager, or however the dashboard
was started); the plugin needs no build step and no dependencies of its own.

## Windows notes

Two things are handled specifically for Windows:

- Every `git` spawn passes `CREATE_NO_WINDOW`, so the scan never flashes a
  console window. Without it, a few hundred spawns turn the tab into a
  strobing console.
- Refs are de-duplicated by object id before the walk, so the number of git
  processes matches the number of *distinct* commits rather than the number of
  refs. On Windows a process spawn costs far more than on POSIX, so this is the
  difference between a scan that finishes and one that stalls.

Output is decoded as UTF-8 explicitly rather than through the locale, so the
tab behaves the same under a non-UTF-8 code page.


## What it does

It reads two trees and sorts their contents into *authored* and *stock*:

- `~/.hermes/skills/` - every directory containing a `SKILL.md`
- `~/.hermes/plugins/` - every subdirectory

### How a skill is judged

A skill is called **stock** when any of three signals fire. All the reasons
that apply are recorded and shown, not just the first:

1. **Exact-content match** against the Hermes git history - the file's bytes
   are hashed the way git hashes blobs and compared against the blob ids of
   every `SKILL.md` / `DESCRIPTION.md` reachable from the newest refs.
2. **Shadows a bundled category** - its path matches a category directory in
   the repo's own skills tree.
3. **Shares a bundled skill name** - its parent directory name collides with a
   bundled one.

A plugin is judged more simply: if its directory name exists under the repo's
`plugins/` or `plugins/memory/`, it ships with stock Hermes.

### Correcting a wrong call

Editing a skill drops signal 1 (the hash necessarily differs), but signals 2
and 3 still fire if the skill keeps its original path. To take ownership of a
stock skill, either move it to a non-colliding path or record the call
explicitly in `overrides.json` next to this plugin:

```json
{
  "authored": ["my-forked-skill"],
  "stock": ["something-i-installed"]
}
```

Paths are relative to `~/.hermes/skills/` for skills and to
`~/.hermes/plugins/` for plugins. Overrides beat every signal, and the file is
git-ignored so it survives plugin updates.

## Why the history scan is bounded

The provenance walk deliberately does not run `git rev-list --objects --all`.
A Hermes checkout can carry thousands of refs and be a `blob:none` partial
clone, where that command cannot finish inside a dashboard request at all and
content git never downloaded cannot be read offline. The plugin instead reads
**tree entries only** (`ls-tree`, never the blobs themselves) for a capped set
of the newest refs, prioritising tags since those carry the most distinct
history, inside a hard time budget.

The consequence is honest and visible: the content check covers a bounded slice
of history rather than all of it, so an ancient bundled skill that was never
tagged may not be caught by signal 1. The name-based signals still apply, and
when no history can be read at all the tab says
"git history unavailable - provenance is weaker" rather than quietly
downgrading the verdict. Use `overrides.json` for anything the heuristics get
wrong.

## Where the repo is read from

By default the bundled content is read from `~/.hermes/hermes-agent`, falling
back to `~/hermes-agent` and `~/hermes/hermes-agent`. On Windows a few extra
roots are probed (`C:\hermes-agent`, `D:\hermes-agent`,
`%USERPROFILE%\source\repos\hermes-agent`, `%USERPROFILE%\Documents\hermes-agent`)
since installs routinely land on another drive. Set `AIDE_MEMOIRE_REPO` to point
anywhere else:

```bash
AIDE_MEMOIRE_REPO=C:\src\hermes-agent
```

If no checkout is found the plugin still works: it lists your skills and
plugins, simply without the git-history signal.

## Layout

```
plugin.yaml              # CLI/gateway plugin metadata (no tools or hooks)
dashboard/manifest.json  # dashboard discovery
dashboard/plugin_api.py  # FastAPI router, mounted at /api/plugins/aide-memoire/
dashboard/dist/index.js  # UI bundle (plain IIFE, no build step)
dashboard/dist/style.css
```
