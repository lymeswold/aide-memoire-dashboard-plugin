from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter

try:
    from hermes_cli import config as _config
    _HERMES_HOME = _config.get_hermes_home()
except Exception:
    _HERMES_HOME = Path.home() / ".hermes"

router = APIRouter()

USER_SKILLS = _HERMES_HOME / "skills"
USER_PLUGINS = _HERMES_HOME / "plugins"


def _find_repo() -> Path:
    """Locate the Hermes source checkout to read bundled content from.

    There is no canonical API for this, so candidates are probed in order and
    the first one that looks like a checkout wins. `AIDE_MEMOIRE_REPO` overrides
    everything for hosts whose clone lives somewhere unusual.
    """
    override = os.environ.get("AIDE_MEMOIRE_REPO")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    candidates = [
        _HERMES_HOME / "hermes-agent",
        home / "hermes-agent",
        home / "hermes" / "hermes-agent",
    ]
    if os.name == "nt":
        # Windows installs land on other drives and under Program Files, so walk
        # a few extra roots before giving up.
        candidates += [
            Path("C:/hermes-agent"),
            Path("D:/hermes-agent"),
            home / "source" / "repos" / "hermes-agent",
            home / "Documents" / "hermes-agent",
        ]
    for c in candidates:
        if (c / "skills").is_dir() or (c / ".git").exists():
            return c
    return candidates[0]


REPO = _find_repo()
BUNDLED_SKILLS = REPO / "skills"
BUNDLED_PLUGIN_DIRS = [REPO / "plugins", REPO / "plugins" / "memory"]

# Manual overrides let you correct any wrong call without touching code.
OVERRIDES_FILE = _HERMES_HOME / "plugins" / "aide-memoire" / "overrides.json"

# Scan budget. The provenance walk is bounded rather than exhaustive: this
# checkout is a blob:none partial clone with thousands of refs, and an
# unrestricted `git rev-list --objects --all` cannot complete inside the
# dashboard's request window (measured: > 5 min, versus ~3 s bounded).
_SCAN_TIMEOUT = 15.0
_HISTORY_REF_LIMIT = 400
_SUBTREE = ("SKILL.md", "DESCRIPTION.md")

# A promisor clone has no network route to its origin in this environment,
# so content git has never fetched cannot be read. Turn every background
# fetch attempt into an immediate, silent failure instead of a long retry
# that would blow the scan budget.
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")

# Windows: every git spawn would otherwise flash a console window. CREATE_NO_WINDOW
# keeps the scan silent when it runs from a windowless service, and on a
# non-UTF-8 code page git's output must be read as bytes decoded by us rather
# than via the locale-dependent text wrapper.
_GIT_SPAWN_KW: dict = {"creationflags": 0}
if os.name == "nt":
    _GIT_SPAWN_KW["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_CACHE: dict = {"blobs": None, "paths": None, "shape": None, "ts": 0.0, "shape_ts": 0.0}
_CACHE_TTL = 300.0


# ---- provenance signals ----------------------------------------------------

def _load_overrides() -> dict:
    """{"authored": ["path", ...], "stock": ["path", ...]} - both optional."""
    try:
        data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
        return {
            "authored": set(data.get("authored", [])),
            "stock": set(data.get("stock", [])),
        }
    except Exception:
        return {"authored": set(), "stock": set()}


def _git(args: list[str], timeout: float) -> str | None:
    """Run a git command, returning stdout or None on any failure/timeout."""
    if not (REPO / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_GIT_SPAWN_KW,
        )
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _history_refs(limit: int) -> list[str]:
    """The refs worth walking for skill history, newest first, de-duplicated.

    Tags come first: this checkout keeps a tag per release plus thousands of
    remote-tracking refs that mostly duplicate the same trees, so walking tags
    first yields far more distinct history for the same time spent.

    `%(objectname)` is emitted alongside the ref name and collapsed here, so a
    release tag and its branch (same commit) cost one `ls-tree` rather than two.
    That keeps the spawn count proportional to distinct trees, which is what
    makes the scan affordable on Windows where each spawn is expensive and would
    otherwise flash a console window.
    """
    out = _git(
        [
            "for-each-ref",
            "--format=%(objectname) %(refname)",
            "--sort=-committerdate",
            f"--count={limit}",
            "refs/tags",
        ],
        timeout=10.0,
    )
    refs = _collapse_duplicate_objects(out)
    if len(refs) < limit:
        extra = _git(
            [
                "for-each-ref",
                "--format=%(objectname) %(refname)",
                "--sort=-committerdate",
                f"--count={limit - len(refs)}",
                "refs/heads",
                "refs/remotes",
            ],
            timeout=10.0,
        )
        refs.extend(_collapse_duplicate_objects(extra))
    return refs


def _collapse_duplicate_objects(out: str | None) -> list[str]:
    """`<objectname> <refname>` lines, keeping one ref per distinct object."""
    if not out:
        return []
    seen: set[str] = set()
    keep: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        obj, ref = parts
        if obj in seen:
            continue
        seen.add(obj)
        keep.append(ref)
    return keep


def _walk_ref_blobs(refs: list[str], budget: float) -> set[str]:
    """Blob ids of SKILL.md/DESCRIPTION.md reachable from the given refs.

    Reads trees only, never the blobs themselves, so it works on a blob:none
    partial clone without any network access. Spends at most `budget` seconds
    in total and stops early once spent.

    One process per ref, deliberately: `git ls-tree` given several tree-ish
    arguments walks only the first and silently ignores the rest, so batching
    refs into one call would quietly lose most of the history. Callers keep the
    spawn count down instead by passing de-duplicated refs (see
    `_distinct_refs`), which matters most on Windows where a process spawn is
    expensive and would flash a console window each time.
    """
    ids: set[str] = set()
    deadline = time.time() + budget
    for ref in refs:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        out = _git(["ls-tree", "-r", ref, "--", "skills"], timeout=remaining)
        if not out:
            continue
        for line in out.splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) < 4 or parts[1] != "blob":
                continue
            if parts[3].rsplit("/", 1)[-1] in _SUBTREE:
                ids.add(parts[2])
    return ids


def _repo_skill_blobs() -> tuple[set[str], bool]:
    """(known stock blob ids, is the history signal usable).

    Walks a bounded set of the newest refs. Unbounded, this checkout's commit
    graph cannot be walked inside a dashboard request at all: it carries
    thousands of refs and is a blob:none promisor clone, so any path whose
    blobs were never downloaded is unfetchable offline.
    """
    now = time.time()
    if _CACHE["blobs"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["blobs"], _CACHE["paths"]

    refs = _history_refs(_HISTORY_REF_LIMIT)
    blobs = _walk_ref_blobs(refs, _SCAN_TIMEOUT) if refs else set()

    _CACHE["blobs"] = blobs
    _CACHE["paths"] = bool(blobs)
    _CACHE["ts"] = now
    return blobs, _CACHE["paths"]


def _git_blob_hash(p: Path) -> str:
    try:
        d = p.read_bytes()
    except Exception:
        return ""
    return hashlib.sha1(b"blob %d\0" % len(d) + d).hexdigest()


def _bundled_shape() -> tuple[set[str], set[str]]:
    """(category relpaths, leaf dir names) in the bundled skills tree."""
    now = time.time()
    cached = _CACHE["shape"]
    if cached is not None and (now - _CACHE["shape_ts"]) < _CACHE_TTL:
        return cached
    cats: set[str] = set()
    leaves: set[str] = set()
    if not BUNDLED_SKILLS.is_dir():
        return cats, leaves
    for f in BUNDLED_SKILLS.rglob("SKILL.md"):
        leaves.add(f.parent.name)
    for d in BUNDLED_SKILLS.rglob("*"):
        if d.is_dir() and any(True for _ in d.rglob("SKILL.md")):
            cats.add(d.relative_to(BUNDLED_SKILLS).as_posix())
    _CACHE["shape"] = (cats, leaves)
    _CACHE["shape_ts"] = now
    return cats, leaves


def _read_frontmatter(md: Path) -> dict:
    out: dict = {}
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    for line in text[3:end].splitlines():
        m = re.match(r"^(name|description|version|author)\s*:\s*(.*)$", line)
        if m:
            val = m.group(2).strip()
            if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            out[m.group(1)] = val
    return out


# ---- classification --------------------------------------------------------

def _classify_skills() -> tuple[list[dict], list[dict]]:
    hist, complete = _repo_skill_blobs()
    cats, leaves = _bundled_shape()
    ov = _load_overrides()

    authored: list[dict] = []
    stock: list[dict] = []

    if not USER_SKILLS.is_dir():
        return authored, stock

    for f in USER_SKILLS.rglob("SKILL.md"):
        rel = f.relative_to(USER_SKILLS)
        if any(p.startswith(".archive") for p in rel.parts):
            continue
        relpath = rel.parent.as_posix()
        fm = _read_frontmatter(f)

        reasons: list[str] = []
        if _git_blob_hash(f) in hist:
            reasons.append("identical to a file in Hermes git history")
        if relpath in cats:
            reasons.append("shadows a bundled category")
        if rel.parent.name in leaves:
            reasons.append("shares a bundled skill name")

        # explicit overrides win
        if relpath in ov["stock"]:
            reasons = ["manually marked stock"]
        elif relpath in ov["authored"]:
            reasons = []

        try:
            mtime = f.stat().st_mtime
        except Exception:
            mtime = 0.0

        item = {
            "name": fm.get("name") or rel.parent.name,
            "path": relpath,
            "description": fm.get("description", ""),
            "version": fm.get("version", ""),
            "author": fm.get("author", ""),
            "mtime": mtime,
            "reasons": reasons,
        }
        (stock if reasons else authored).append(item)

    return authored, stock


def _classify_plugins() -> tuple[list[dict], list[dict]]:
    bundled: set[str] = set()
    for d in BUNDLED_PLUGIN_DIRS:
        if d.is_dir():
            for c in d.iterdir():
                if c.is_dir() and not c.name.startswith("__"):
                    bundled.add(c.name)
    ov = _load_overrides()

    authored: list[dict] = []
    stock: list[dict] = []
    if not USER_PLUGINS.is_dir():
        return authored, stock

    for child in sorted(USER_PLUGINS.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "__")):
            continue
        desc = version = ""
        manifest = child / "dashboard" / "manifest.json"
        plugin_yaml = child / "plugin.yaml"
        if manifest.exists():
            try:
                m = json.loads(manifest.read_text(encoding="utf-8"))
                desc = m.get("description", "")
                version = str(m.get("version", ""))
            except Exception:
                pass
        if not desc and plugin_yaml.exists():
            try:
                for line in plugin_yaml.read_text(encoding="utf-8", errors="replace").splitlines():
                    mm = re.match(r"^(description|version)\s*:\s*(.*)$", line)
                    if mm:
                        v = mm.group(2).strip().strip("\"'")
                        if mm.group(1) == "description" and not desc:
                            desc = v
                        elif mm.group(1) == "version" and not version:
                            version = v
            except Exception:
                pass

        kinds = []
        if (child / "dashboard").is_dir():
            kinds.append("dashboard")
        if (child / "__init__.py").exists() or plugin_yaml.exists():
            kinds.append("agent")

        reasons = ["ships with stock Hermes"] if child.name in bundled else []
        if child.name in ov["stock"]:
            reasons = ["manually marked stock"]
        elif child.name in ov["authored"]:
            reasons = []

        try:
            mtime = child.stat().st_mtime
        except Exception:
            mtime = 0.0

        item = {
            "name": child.name,
            "path": child.name,
            "description": desc,
            "version": version,
            "kinds": kinds,
            "mtime": mtime,
            "reasons": reasons,
        }
        (stock if reasons else authored).append(item)

    return authored, stock


# ---- routes ----------------------------------------------------------------

@router.get("/list")
def list_authored():
    a_sk, s_sk = _classify_skills()
    a_pl, s_pl = _classify_plugins()
    for lst in (a_sk, s_sk, a_pl, s_pl):
        lst.sort(key=lambda x: x["name"].lower())

    _, complete = _repo_skill_blobs()

    return {
        "skills": a_sk,
        "plugins": a_pl,
        "excluded_skills": s_sk,
        "excluded_plugins": s_pl,
        "counts": {
            "skills": len(a_sk),
            "plugins": len(a_pl),
            "excluded_skills": len(s_sk),
            "excluded_plugins": len(s_pl),
        },
        "git_history_available": complete,
        "overrides_file": str(OVERRIDES_FILE),
    }


@router.post("/refresh")
def refresh():
    _CACHE["blobs"] = None
    _CACHE["paths"] = None
    _CACHE["shape"] = None
    _CACHE["ts"] = 0.0
    _CACHE["shape_ts"] = 0.0
    return {"ok": True}
