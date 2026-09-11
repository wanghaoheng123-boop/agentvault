#!/usr/bin/env python3
"""AgentVault coordination CLI — leases, maildir, IDs, board refresh, doctor, tests.

Usage:
  python3 scripts/coord/avcoord.py <command> [options]

Environment:
  AVCOORD_ROOT  Optional workspace root (default: repo root containing this script).
                Trail/smoke tests set this to an isolated temp sandbox.

Commands:
  claim | release | renew | post | recv | ack | next-id | refresh | doctor | test
  hydrate | intent | query | done | compact | fingerprint   # AK-SPWS (ADR-009)
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import fcntl
import hashlib
import hmac
import secrets
import threading
import types
import importlib.util
import json
import os
import posixpath
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

STALE_HOURS_DEFAULT = 48
DEFAULT_TTL_MIN = 15
MAX_TTL_MIN = 120
TZ = timezone(timedelta(hours=8))
MAIL_CUR_WARN = 20

# Path globals — configured by configure_paths()
REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT: Path = REPO_ROOT
MB: Path
COORD: Path
LEASES: Path
MAIL: Path
NEXT_IDS: Path
AUDIT: Path
REGISTRY: Path
CURRENT: Path
BOARD: Path
ACTIVE: Path
EVENTS: Path


def configure_paths(root: Path | None = None) -> None:
    """Bind all MemoryBank/coord paths to root (or AVCOORD_ROOT / repo default)."""
    global ROOT, MB, COORD, LEASES, MAIL, NEXT_IDS, AUDIT, REGISTRY, CURRENT, BOARD, ACTIVE, EVENTS
    if root is None:
        env = os.environ.get("AVCOORD_ROOT")
        root = Path(env).resolve() if env else REPO_ROOT
    ROOT = Path(root).resolve()
    MB = ROOT / "MemoryBank"
    COORD = MB / "coord"
    LEASES = COORD / "leases"
    MAIL = COORD / "mail"
    NEXT_IDS = COORD / "next_ids.json"
    AUDIT = COORD / "audit.jsonl"
    REGISTRY = MB / "agents" / "registry.json"
    CURRENT = MB / "CURRENT.md"
    BOARD = MB / "board.md"
    ACTIVE = MB / "activeContext.md"
    EVENTS = ROOT / "EpisodicTracker" / "events.jsonl"


configure_paths()


# Injectable clock. Tests set this to make expiry/staleness deterministic instead of
# forging stored timestamps. None means "use the wall clock".
_CLOCK: Any = None


def set_clock(fn: Any) -> None:
    """Install (or clear, with None) a zero-arg callable returning an aware datetime."""
    global _CLOCK
    _CLOCK = fn


def now() -> datetime:
    if _CLOCK is not None:
        return _CLOCK()
    return datetime.now(TZ)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_ttl(s: str) -> int:
    s = s.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(m|min|mins|h|hr|hrs)?", s)
    if not m:
        raise ValueError(f"bad ttl: {s}")
    n = int(m.group(1))
    unit = m.group(2) or "m"
    mins = n * 60 if unit.startswith("h") else n
    if mins < 1 or mins > MAX_TTL_MIN:
        raise ValueError(f"ttl must be 1..{MAX_TTL_MIN} minutes")
    return mins


def fsync_dir(path: Path) -> None:
    """fsync a directory so a rename or creation inside it survives a host crash.

    Without this the new file contents are durable but the directory entry pointing at
    them may not be, which is how an atomic-looking replace loses data on power loss.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# Shared across importlib-loaded avcoord instances; a nested writer must not deadlock
# itself, while threads and other processes still serialize on the same inode.
_LOCK_STATE = sys.modules.setdefault("_agentvault_lock_state", types.ModuleType("_agentvault_lock_state"))
if getattr(_LOCK_STATE, "pid", None) != os.getpid():
    _LOCK_STATE.pid = os.getpid()
    _LOCK_STATE.guard = threading.Lock()
    _LOCK_STATE.locks = {}


class FileLock:
    """Reentrant per thread, kernel-released on process exit; never steal live locks."""

    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path, self.timeout = Path(path), timeout
        self._entry = None

    def __enter__(self):
        if _LOCK_STATE.pid != os.getpid():
            _LOCK_STATE.pid = os.getpid()
            _LOCK_STATE.guard = threading.Lock()
            _LOCK_STATE.locks = {}
        key = str(self.path.resolve())
        with _LOCK_STATE.guard:
            entry = _LOCK_STATE.locks.setdefault(key, {"mutex": threading.RLock(), "depth": 0, "fd": None})
        if not entry["mutex"].acquire(timeout=self.timeout):
            raise RuntimeError(f"could not acquire {self.path.name} within {self.timeout}s")
        try:
            if not entry["depth"]:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                deadline = time.monotonic() + self.timeout
                try:
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise RuntimeError(f"could not acquire {self.path.name} within {self.timeout}s")
                            time.sleep(0.01)
                except BaseException:
                    os.close(fd)
                    raise
                entry["fd"] = fd
            entry["depth"] += 1
            self._entry = entry
            return self
        except BaseException:
            entry["mutex"].release()
            raise

    def __exit__(self, *exc):
        entry, self._entry = self._entry, None
        if entry is None:
            return
        try:
            entry["depth"] -= 1
            if not entry["depth"]:
                try:
                    fcntl.flock(entry["fd"], fcntl.LOCK_UN)
                finally:
                    os.close(entry["fd"])
                    entry["fd"] = None
        finally:
            entry["mutex"].release()


def coord_lock(name: str = "coord.lock", timeout: float = 10.0) -> FileLock:
    """The single serialization point for lease and journal mutations."""
    return FileLock(COORD / name, timeout=timeout)


def audit(event: str, **payload: Any) -> None:
    """Append one durable, non-interleaved record to the audit ledger.

    Locks the ledger's own fd (never the coord lock — audit() is called from inside
    coord-locked sections). One os.write of one complete line under O_APPEND + flock
    is what stops concurrent writers from tearing each other's records.
    """
    COORD.mkdir(parents=True, exist_ok=True)
    rec = {"ts": iso(), "event": event, **payload}
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(AUDIT), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
        if os.environ.get("AVCOORD_AUDIT_FSYNC", "1") != "0":
            os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def resource_hash(resource: str) -> str:
    return hashlib.sha256(resource.encode("utf-8")).hexdigest()[:16]


def lease_path(resource: str) -> Path:
    return LEASES / f"{resource_hash(resource)}.json"


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def lease_expired(lease: dict) -> bool:
    try:
        exp = datetime.fromisoformat(lease["expires_at"])
    except Exception:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=TZ)
    return now() >= exp


def read_leases(*, reap: bool = False) -> list[dict]:
    """Return live leases. A pure read unless reap=True.

    reap=True unlinks expired leases and must only be called with the lease lock held.
    Expired leases are filtered out of the result either way, so every caller sees exactly
    what it saw before — only the side effect goes away. Previously every reader (status,
    doctor, check-lease, refresh) deleted lease files while cmd_claim held the lock.
    """
    out: list[dict] = []
    if not LEASES.exists():
        return out
    for p in sorted(LEASES.glob("*.json")):
        if p.name.startswith("."):
            continue
        try:
            lease = load_json(p)
        except Exception as e:
            if reap:
                audit("lease_read_error", path=str(p), error=str(e))
            continue
        if not lease:
            continue
        if not lease_expired(lease):
            lease["_path"] = str(p)
            out.append(lease)
        elif reap:
            p.unlink(missing_ok=True)
            audit("lease_reaped", resource=lease.get("resources"), agent=lease.get("agent_id"))
    return out


def read_live_leases() -> list[dict]:
    """Back-compat alias. Pure read — never reaps."""
    return read_leases()


# --------------------------------------------------------------------------------------
# Canonical path kernel
#
# Every resource denotes a SUBTREE ROOT: "X", "X/" and "X/**" mean the same thing, so the
# trailing marker is display metadata only. This keeps contested.json (trailing slash) and
# registry write_scopes (trailing /**) comparable against bare lease paths.
#
# Two polarities, and they must not be unified:
#   * authorization is RESTRICTIVE and strictly downward (a lease never covers its ancestor)
#   * contested detection is PERMISSIVE and symmetric (a write to MemoryBank/ must still be
#     flagged, because it swallows MemoryBank/CURRENT.md)
# --------------------------------------------------------------------------------------


class CanonError(ValueError):
    """A resource could not be canonicalized safely. Callers must treat this as denial."""

    def __init__(self, code: str, msg: str) -> None:
        self.code = code
        super().__init__(msg)


class Res(NamedTuple):
    display: str  # canonical POSIX relative path, as-entered case, no leading/trailing slash
    key: str  # comparison key: NFC-normalized and casefolded
    explicit_subtree: bool  # input ended in "/" or "/**" — display metadata only
    raw: str  # original string, preserved for lease filenames and receipts


def _fold(s: str) -> str:
    """Comparison folding: NFC + casefold, on every platform.

    Folding is uniformly the fail-closed direction: it denies more claims, authorizes
    fewer writes and flags more paths as contested. macOS realpath does not canonicalize
    case, so case identity has to live in the comparison key, not in normalization.
    """
    return unicodedata.normalize("NFC", s).casefold()


def _rel_to_root(abs_path: str, root: Path) -> str:
    """Relativize an absolute path against root, or raise.

    Path.resolve().relative_to(ROOT) compares exact strings, so a case-variant spelling of
    the workspace root raises ValueError — which the old bare `except: pass` swallowed,
    leaving the path absolute and therefore silently 'not contested'. Containment is tested
    case-folded; the slice is taken from the true-case realpath so real case is preserved.
    """
    a = os.path.realpath(abs_path)
    r = os.path.realpath(str(root))
    if _fold(a) == _fold(r):
        return ""
    if not _fold(a).startswith(_fold(r) + os.sep):
        raise CanonError("OUTSIDE_ROOT", f"resource outside workspace root: {abs_path}")
    return a[len(r) + 1 :].replace(os.sep, "/")


def canon_resource(raw: Any, *, root: Path | None = None) -> Res:
    """Canonicalize a workspace resource. Fail-closed.

    Lexical first, so resources naming paths that do not exist yet (a live lease on
    'scripts/evolution', for instance) stay valid. Symlinks are then dereferenced over the
    existing prefix only — a component that does not exist cannot be a symlink, so realpath
    passes the missing tail through untouched.
    """
    if not isinstance(raw, str):
        raise CanonError("NOT_A_STRING", f"resource must be a string, got {type(raw).__name__}")
    root = root or ROOT
    s = raw.replace("\\", "/").strip()
    if not s:
        raise CanonError("EMPTY", "empty resource")

    explicit_subtree = False
    if s.endswith("/**"):
        explicit_subtree, s = True, s[:-3]
    elif s.endswith("/"):
        explicit_subtree, s = True, s.rstrip("/")

    if any(c in s for c in "*?["):
        raise CanonError(
            "UNSUPPORTED_GLOB",
            f"unsupported glob in {raw!r}; only a trailing '/' or '/**' subtree marker is supported",
        )

    if s.startswith("/"):
        s = _rel_to_root(s, root)
    s = posixpath.normpath(s) if s else ""
    if s == ".." or s.startswith("../"):
        raise CanonError("ROOT_ESCAPE", f"resource escapes workspace root: {raw}")
    if s == ".":
        s = ""
    if s:
        s = _rel_to_root(os.path.realpath(str(Path(root) / s)), root)

    return Res(display=s, key=_fold(s), explicit_subtree=explicit_subtree, raw=raw)


def canon_or_none(raw: Any, *, root: Path | None = None) -> "Res | None":
    try:
        return canon_resource(raw, root=root)
    except CanonError:
        return None


def res_covers(outer: Res, inner: Res) -> bool:
    """DIRECTIONAL authorization using exact canonical path spelling.

    Case-folding is deliberately reserved for claim exclusion. Using it here would let
    a lease on ``reports/A.md`` authorize the distinct ``reports/a.md`` on a
    case-sensitive filesystem. Exact spelling may conservatively deny a case alias on a
    case-insensitive volume, which is the safe direction for write authorization.
    """
    if not outer.display:
        return True  # the workspace root covers everything
    return (inner.display == outer.display
            or inner.display.startswith(outer.display + "/"))


def _folded_covers(outer: Res, inner: Res) -> bool:
    """Conservative comparison used only to exclude potentially colliding claims."""
    if not outer.key:
        return True
    return inner.key == outer.key or inner.key.startswith(outer.key + "/")


def resources_overlap(a: Res, b: Res) -> bool:
    """SYMMETRIC: do these two resources share any concrete path? Claim exclusion only."""
    return _folded_covers(a, b) or _folded_covers(b, a)


def lease_authorizes(lease_res: Any, target: Any) -> bool:
    """DIRECTIONAL: does a lease on `lease_res` authorize writing `target`?

    A lease covers itself and everything beneath it, and never its own ancestor. Anything
    that will not canonicalize is not authorized.
    """
    lo = lease_res if isinstance(lease_res, Res) else canon_or_none(lease_res)
    ti = target if isinstance(target, Res) else canon_or_none(target)
    if lo is None or ti is None:
        return False
    return res_covers(lo, ti)


def resources_conflict(a: str, b: str) -> bool:
    """DEPRECATED symmetric overlap shim. Never use this for authorization."""
    ca, cb = canon_or_none(a), canon_or_none(b)
    if ca is None or cb is None:
        return False
    return resources_overlap(ca, cb)


# Deprecated lock shims retained so allocate_id and the chaos trail keep one code path.
# stale_s is ignored: the kernel owns liveness now, so there is nothing to time out.
_LEGACY_LOCKS: dict[str, FileLock] = {}


def _acquire_file_lock(lock: Path, timeout: float = 10.0, stale_s: float = 30.0) -> None:
    fl = FileLock(lock, timeout=timeout)
    fl.__enter__()
    _LEGACY_LOCKS[str(lock)] = fl


def _release_file_lock(lock: Path) -> None:
    fl = _LEGACY_LOCKS.pop(str(lock), None)
    if fl is not None:
        fl.__exit__(None, None, None)


RUN_TTL_HOURS_DEFAULT = 4


def runs_dir() -> Path:
    return COORD / "runs"


def load_run(run_id: str) -> dict | None:
    """A registered, unexpired run, or None. An arbitrary id string grants no authority."""
    if not isinstance(run_id, str) or not re.fullmatch(r"run-[A-Za-z0-9-]{1,100}", run_id):
        return None
    p = runs_dir() / f"{run_id}.json"
    data = load_json(p, None)
    if not data or data.get("run_id") != run_id or data.get("status") != "active":
        return None
    try:
        exp = datetime.fromisoformat(data["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=TZ)
        if now() >= exp:
            return None
    except Exception:
        return None
    return data


def resolve_run(args: argparse.Namespace) -> tuple[str | None, int, str | None]:
    """(run_id, fence, error).

    An explicit --run must resolve to a registered, unexpired run. Omitting it yields a
    legacy principal with fence 0 that behaves exactly as before — that is what keeps the
    five runtime wrappers, bin/avcoord and both hooks working unchanged.
    """
    rid = getattr(args, "run", None) or os.environ.get("AVCOORD_RUN_ID")
    if not rid:
        return None, 0, None
    run = load_run(rid)
    if not run:
        return None, 0, (
            f"unknown or expired run '{rid}'. Register one first:\n"
            f"  python3 scripts/coord/avcoord.py run start --role {getattr(args, 'agent', '<role>')} --runtime <runtime>"
        )
    agent = getattr(args, "agent", None)
    if agent and run.get("agent_id") != agent:
        return None, 0, f"run '{rid}' belongs to agent '{run.get('agent_id')}', not '{agent}'"
    if run.get("token_sha256"):
        token = getattr(args, "run_token", None) or os.environ.get("AVCOORD_RUN_TOKEN", "")
        if not isinstance(token, str) or not hmac.compare_digest(
                run["token_sha256"], hashlib.sha256(token.encode()).hexdigest()):
            return None, 0, "missing or invalid run token"
    return rid, int(run.get("fence", 0)), None


def cmd_run_start(args: argparse.Namespace) -> int:
    """Register a run instance. Capabilities bind to a run, not to a persona string."""
    if not validate_agent(args.role):
        return 1
    runs_dir().mkdir(parents=True, exist_ok=True)
    fence = int(allocate_id("fence"))
    run_id = f"run-{now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ttl_h = float(getattr(args, "ttl_hours", None) or RUN_TTL_HOURS_DEFAULT)
    agent = registry_agent(args.role) or {}
    run_token = secrets.token_urlsafe(32)
    run = {
        "schema_version": "2.0",
        "token_sha256": hashlib.sha256(run_token.encode()).hexdigest(),
        "run_id": run_id,
        "agent_id": args.role,
        "role": args.role,
        "runtime": args.runtime,
        "pid": os.getpid(),
        "started_at": iso(),
        "expires_at": iso(now() + timedelta(hours=ttl_h)),
        "fence": fence,
        "task_id": getattr(args, "task", "") or "",
        "capabilities": agent.get("write_capabilities") or agent.get("write_scopes") or [],
        "status": "active",
    }
    atomic_write_json(runs_dir() / f"{run_id}.json", run)
    fsync_dir(runs_dir())
    audit("run_start", run_id=run_id, agent=args.role, runtime=args.runtime, fence=fence)
    print(json.dumps({**run, "run_token": run_token}, indent=2))
    return 0


def cmd_run_list(args: argparse.Namespace) -> int:
    out = []
    if runs_dir().exists():
        for p in sorted(runs_dir().glob("*.json")):
            r = load_run(p.stem)
            if r:
                out.append({k: r[k] for k in ("run_id", "agent_id", "runtime", "fence", "expires_at")})
    print(json.dumps({"active_runs": out}, indent=2))
    return 0


def lease_resources(lease: dict) -> list[Res]:
    """Return resources still bound to the canonical objects captured at claim time.

    A requested symlink is re-resolved on every use. If it was removed or retargeted,
    its lease grants nothing. This prevents a claim on link->A from silently becoming a
    capability for link->B. Older leases are checked against their stored comparison
    keys; newly issued leases also carry the exact canonical display path.
    """
    resources = lease.get("resources") or []
    keys = lease.get("resource_keys") or []
    captured = lease.get("canonical_resources") or []
    requested = lease.get("requested_resources") or resources
    if not isinstance(resources, list) or not isinstance(keys, list) or len(resources) != len(keys):
        return []
    if captured and (not isinstance(captured, list) or len(captured) != len(resources)):
        return []
    if not isinstance(requested, list) or len(requested) != len(resources):
        return []
    out: list[Res] = []
    for index, stored in enumerate(resources):
        current = canon_or_none(requested[index])
        bound = canon_or_none(captured[index] if captured else stored)
        if current is None or bound is None or current.key != keys[index] or bound.key != keys[index]:
            continue
        if captured and (current.display != bound.display
                         or bound.display != canon_resource(stored).display):
            continue
        out.append(bound)
    return out


def _same_principal(lease: dict, agent: str, run_id: str | None) -> bool:
    """Is this lease held by the caller?

    Two registered runs of the same role are DIFFERENT principals, so they exclude each
    other. Legacy callers with no run identity fall back to the agent id, which preserves
    today's behavior (re-claiming your own resource stays idempotent).
    """
    other_run = lease.get("run_id") or None
    return lease.get("agent_id") == agent and other_run == (run_id or None)



def cmd_claim(args: argparse.Namespace) -> int:
    if not validate_agent(args.agent):
        return 1
    raw_resources = args.resource if isinstance(args.resource, list) else [args.resource]
    try:
        wanted = [(r, canon_resource(r)) for r in raw_resources]
    except CanonError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    for raw, c in wanted:
        if not c.display:
            print(f"FAIL: refusing to claim the workspace root ({raw!r})", file=sys.stderr)
            return 1
    run_id, fence, err = resolve_run(args)
    if err:
        print(f"FAIL: {err}", file=sys.stderr)
        return 1
    if not check_write_scopes(args.agent, [c for _, c in wanted], strict=getattr(args, "strict_scopes", False)):
        return 1
    ttl_min = parse_ttl(args.ttl)
    expires = now() + timedelta(minutes=ttl_min)
    LEASES.mkdir(parents=True, exist_ok=True)
    try:
        with coord_lock():
            live = read_leases(reap=True)
            for raw, c in wanted:
                for other in live:
                    if _same_principal(other, args.agent, run_id):
                        continue
                    for oc in lease_resources(other):
                        if resources_overlap(c, oc):
                            print(
                                f"FAIL: resource '{raw}' held by {other.get('agent_id')} until {other.get('expires_at')}",
                                file=sys.stderr,
                            )
                            audit("claim_denied", agent=args.agent, resource=raw, holder=other.get("agent_id"))
                            return 1
            for raw, c in wanted:
                modern = bool(run_id and load_run(run_id).get("token_sha256"))
                stored_resource = c.display + ("/**" if c.explicit_subtree else "")
                lease = {
                    "schema_version": "2.0" if modern else "1.1",
                    "agent_id": args.agent,
                    "run_id": run_id or "",
                    "fence": int(allocate_id("fence")) if modern else fence,
                    "run_fence": fence,
                    "resources": [stored_resource],
                    "resource_keys": [c.key],
                    "canonical_resources": [stored_resource],
                    "requested_resources": [raw],
                    "lease_token": uuid.uuid4().hex,
                    "reason": args.reason or "",
                    "task_id": args.task_id or "",
                    "expires_at": iso(expires),
                    "renewed_at": None,
                    "created_at": iso(),
                }
                # lease_path() still hashes the RAW string: changing it would orphan every
                # lease file already on disk. Alias safety comes from the scan above.
                lp = lease_path(raw)
                if lp.exists():
                    existing = load_json(lp)
                    if (
                        existing
                        and not lease_expired(existing)
                        and not _same_principal(existing, args.agent, run_id)
                    ):
                        print(f"FAIL: race lost on {raw}", file=sys.stderr)
                        return 1
                atomic_write_json(lp, lease)
                audit("claim", agent=args.agent, resource=raw, expires_at=lease["expires_at"])
                print(json.dumps(lease, indent=2))
            return 0
    except RuntimeError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1


def cmd_release(args: argparse.Namespace) -> int:
    if not validate_agent(args.agent):
        return 1
    if not args.all and not args.resource:
        print("FAIL: release requires --all or --resource", file=sys.stderr)
        return 1
    raw_targets = [] if args.all else (args.resource if isinstance(args.resource, list) else [args.resource])
    try:
        targets = [canon_resource(t) for t in raw_targets]
    except CanonError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    token = getattr(args, "token", None)
    run_id, fence, err = resolve_run(args)
    if err:
        print(f"FAIL: {err}", file=sys.stderr)
        return 1
    removed = 0
    fenced_out = 0
    token_denied = 0
    mine = 0
    with coord_lock():
        for p in sorted(LEASES.glob("*.json")):
            if p.name.startswith("."):
                continue
            lease = load_json(p)
            if not lease:
                continue
            if not _same_principal(lease, args.agent, run_id):
                continue
            mine += 1
            # A per-lease token authorizes ONE lease, so `--all` under a run cannot supply
            # one: every lease has a different token. Demanding it there matched nothing and
            # released nothing. An authenticated run is credential enough for --all — the
            # caller proved possession of the run token in resolve_run(), and _same_principal
            # plus the fence below still bind the sweep to leases this exact run holds.
            # A targeted --resource release stays fail-closed: it still requires --token.
            if (token or (run_id and not args.all)) and lease.get("lease_token") != token:
                token_denied += 1
                continue
            # Fencing: a run whose generation is older than the lease's cannot act on it.
            # This is what rejects a paused worker after its resource was reclaimed.
            if run_id and int(lease.get("run_fence", lease.get("fence", 0))) != fence:
                fenced_out += 1
                continue
            if args.all:
                p.unlink(missing_ok=True)
                removed += 1
                audit("release", agent=args.agent, resource=lease.get("resources"))
                continue
            # DIRECTIONAL: the named target must COVER the held resource. The old symmetric
            # test meant `release --resource scripts/coord/avcoord.py` deleted a whole
            # `scripts/coord/**` lease — a narrow release silently dropping a broad one.
            held = lease_resources(lease)
            if held and all(any(res_covers(t, h) for t in targets) for h in held):
                p.unlink(missing_ok=True)
                removed += 1
                audit("release", agent=args.agent, resource=lease.get("resources"))
    if token_denied:
        print(f"denied {token_denied} lease(s): lease token mismatch", file=sys.stderr)
    if fenced_out:
        print(f"denied {fenced_out} lease(s): stale fence", file=sys.stderr)
    if not removed:
        # "released 0" alone cannot be read: holding nothing, being fenced out and failing
        # token auth all printed the same line. Name which one it was.
        if fenced_out:
            why = f"{fenced_out} lease(s) held under a different fence generation"
        elif token_denied:
            why = f"{token_denied} lease(s) held, none matching --token"
        elif mine:
            why = f"{mine} lease(s) held, none covered by the named --resource"
        else:
            why = f"no lease is held by {args.agent}" + (f" in run {run_id}" if run_id else "")
        print(f"FAIL: released nothing: {why}", file=sys.stderr)
    print(f"released {removed} lease(s)")
    return 0 if removed else 1


def cmd_renew(args: argparse.Namespace) -> int:
    if not validate_agent(args.agent):
        return 1
    ttl_min = parse_ttl(args.ttl)
    expires = now() + timedelta(minutes=ttl_min)
    token = getattr(args, "token", None)
    run_id, fence, err = resolve_run(args)
    if err:
        print(f"FAIL: {err}", file=sys.stderr)
        return 1
    n = 0
    denied = 0
    with coord_lock():
        for p in sorted(LEASES.glob("*.json")):
            if p.name.startswith("."):
                continue
            lease = load_json(p)
            if not lease or lease_expired(lease):
                continue
            if not _same_principal(lease, args.agent, run_id):
                continue
            # A run may only renew a lease it actually holds. Without this, one run could
            # extend another's lease simply by sharing its role.
            if (run_id or token) and lease.get("lease_token") != token:
                denied += 1
                continue
            if run_id and int(lease.get("run_fence", lease.get("fence", 0))) != fence:
                denied += 1
                continue
            lease["expires_at"] = iso(expires)
            lease["renewed_at"] = iso()
            atomic_write_json(p, lease)
            n += 1
            audit("renew", agent=args.agent, resource=lease.get("resources"), expires_at=lease["expires_at"])
    if denied:
        print(f"denied {denied} lease(s): token mismatch or stale fence", file=sys.stderr)
    print(f"renewed {n} lease(s)")
    return 0 if n else 1


def ensure_mailboxes(agent: str) -> Path:
    base = MAIL / agent
    for sub in ("tmp", "new", "cur", "done", "receipts"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def allocate_id(kind: str) -> str:
    """Monotonic ID under exclusive lock on next_ids.json."""
    COORD.mkdir(parents=True, exist_ok=True)
    lock = COORD / "next_ids.lock"
    _acquire_file_lock(lock)
    try:
        data = load_json(NEXT_IDS, {"progress": 1, "episode": 1, "message": 1, "fence": 1})
        # 'fence' is additive: seed it on existing installs rather than failing.
        data.setdefault("fence", 1)
        if kind not in data:
            raise ValueError(f"unknown kind: {kind}")
        n = int(data[kind])
        data[kind] = n + 1
        data["last_updated"] = iso()
        # Auto-sync human notes to match NEXT counters
        data["notes"] = (
            f"Counters are the NEXT id to allocate. "
            f"progress→Entry #{data['progress']:03d}, episode→EP-{data['episode']:03d}, "
            f"message→MSG-{data['message']:06d}."
        )
        atomic_write_json(NEXT_IDS, data)
        if kind == "progress":
            return f"{n:03d}"
        if kind == "episode":
            return f"EP-{n:03d}"
        if kind == "message":
            return f"MSG-{n:06d}"
        return str(n)
    finally:
        _release_file_lock(lock)


def cmd_next_id(args: argparse.Namespace) -> int:
    try:
        val = allocate_id(args.kind)
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    audit("next_id", kind=args.kind, value=val)
    print(val)
    return 0


def iter_messages(agent: str | None = None):
    """Every message in every box, newest box last."""
    roots = [MAIL / agent] if agent else ([p for p in MAIL.iterdir() if p.is_dir()] if MAIL.exists() else [])
    for base in roots:
        for box in ("new", "cur", "done"):
            d = base / box
            if not d.exists():
                continue
            for p in sorted(d.glob("*.json")):
                data = load_json(p, None)
                if data:
                    yield data


def find_message(msg_id: str) -> dict | None:
    for m in iter_messages():
        if m.get("id") == msg_id:
            return m
    return None


def find_message_by_idempotency(agent: str, key: str) -> dict | None:
    for m in iter_messages(agent):
        if m.get("idempotency_key") == key:
            return m
    return None


def cmd_post(args: argparse.Namespace) -> int:
    if not validate_agent(args.from_agent) or not validate_agent(args.to_agent):
        return 1
    ensure_mailboxes(args.to_agent)
    ensure_mailboxes(args.from_agent)
    idem = getattr(args, "idempotency_key", None) or ""
    if idem:
        # At-least-once delivery with an idempotent local effect: a retried post returns the
        # message it already created instead of minting a second id.
        existing = find_message_by_idempotency(args.to_agent, idem)
        if existing:
            print(json.dumps(existing, indent=2))
            return 0

    parent_id = getattr(args, "parent", None) or None
    hop = int(getattr(args, "hop", 0) or 0)
    if parent_id:
        # The server derives the hop from the parent. A caller-supplied --hop cannot reset a
        # chain: previously `hop` was taken verbatim and nothing ever incremented it.
        parent = find_message(parent_id)
        if not parent:
            print(f"FAIL: parent message not found: {parent_id}", file=sys.stderr)
            return 1
        hop = int(parent.get("hop", 0)) + 1

    msg_id = allocate_id("message")
    refs = []
    if args.refs:
        for r in args.refs.split(","):
            r = r.strip()
            if not r:
                continue
            fp = ROOT / r
            refs.append(
                {"path": r, "sha256": hashlib.sha256(fp.read_bytes()).hexdigest()}
                if fp.is_file()
                else {"path": r, "sha256": None}
            )
    msg = {
        "schema_version": "1.1",
        "id": msg_id,
        "ts": iso(),
        "from": args.from_agent,
        "to": args.to_agent,
        "type": args.type,
        "intent": getattr(args, "intent", "") or "",
        "task_id": args.task_id or "",
        "run_id": getattr(args, "run", None) or "",
        "parent_id": parent_id,
        "idempotency_key": idem,
        "hop": hop,
        "max_hop": 8,
        "refs": refs,
        "summary": args.summary,
        "status": "pending",
    }
    if msg["hop"] >= msg["max_hop"] and msg["type"] not in ("block", "done", "ack"):
        print(f"FAIL: hop cap exceeded (hop={msg['hop']} >= {msg['max_hop']}); emit block", file=sys.stderr)
        return 1
    base = MAIL / args.to_agent
    tmp = base / "tmp" / f"{msg_id}.json"
    dest = base / "new" / f"{msg_id}.json"
    atomic_write_json(tmp, msg)
    os.replace(tmp, dest)
    fsync_dir(dest.parent)
    audit("post", **{k: msg[k] for k in ("id", "from", "to", "type")})
    print(json.dumps(msg, indent=2))
    return 0


def cmd_recv(args: argparse.Namespace) -> int:
    if not validate_agent(args.agent):
        return 1
    base = ensure_mailboxes(args.agent)
    moved = []
    for p in sorted((base / "new").glob("*.json")):
        dest = base / "cur" / p.name
        os.replace(p, dest)
        fsync_dir(dest.parent)
        fsync_dir(p.parent)
        moved.append(dest.name)
        audit("recv", agent=args.agent, file=dest.name)
    # --resume also surfaces messages already in cur/. Without it, a crash between recv and
    # acting on a message hid it from every view: recv only ever globbed new/.
    resumed = []
    if getattr(args, "resume", False):
        resumed = [p.name for p in sorted((base / "cur").glob("*.json")) if p.name not in moved]
    listing = moved + resumed
    summaries = []
    for name in listing:
        data = load_json(base / "cur" / name, {})
        summaries.append(
            {
                "id": data.get("id", name),
                "from": data.get("from"),
                "type": data.get("type"),
                "summary": data.get("summary"),
                "refs": data.get("refs"),
            }
        )
    print(json.dumps({"moved_to_cur": moved, "resumed_from_cur": resumed, "messages": summaries}, indent=2))
    if getattr(args, "full", False):
        for name in listing:
            print((base / "cur" / name).read_text(encoding="utf-8"))
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    if not validate_agent(args.agent):
        return 1
    base = ensure_mailboxes(args.agent)
    if not isinstance(args.msg, str) or not re.fullmatch(r"MSG-[A-Za-z0-9_.-]+", args.msg):
        print("FAIL: invalid message id/prefix", file=sys.stderr)
        return 1
    with coord_lock():
        exact_cur = base / "cur" / f"{args.msg}.json"
        exact_done = base / "done" / f"{args.msg}.json"
        candidates = ([exact_cur] if exact_cur.is_file() else []) + ([exact_done] if exact_done.is_file() else [])
        if not candidates:
            candidates = sorted((base / "cur").glob(f"{args.msg}*.json"))
            candidates += sorted((base / "done").glob(f"{args.msg}*.json"))
        unique = {path.name: path for path in candidates}
        if not unique:
            print(f"FAIL: message not in cur or done: {args.msg}", file=sys.stderr)
            return 1
        if len(unique) > 1:
            print(f"FAIL: '{args.msg}' is ambiguous: {', '.join(sorted(unique))}", file=sys.stderr)
            return 1
        src = next(iter(unique.values()))
        dest = base / "done" / src.name
        duplicate = src.parent == dest.parent
        data = load_json(src)
        if (not isinstance(data, dict) or data.get("id") != src.stem
                or data.get("to") not in (None, args.agent)):
            print("FAIL: invalid or misaddressed message", file=sys.stderr)
            return 1
        if data.get("status") != "acked":
            data["status"] = "acked"
            data["acked_at"] = data.get("acked_at") or iso()
            atomic_write_json(src, data)
        if not duplicate:
            os.replace(src, dest)
            fsync_dir(dest.parent)
            fsync_dir(src.parent)
        done_raw = dest.read_bytes()
        receipt = {
            "schema_version": "1.0", "effect": "mail.ack", "message_id": data["id"],
            "agent_id": args.agent, "acked_at": data["acked_at"],
            "message_sha256": hashlib.sha256(done_raw).hexdigest(),
        }
        receipt_raw = (json.dumps(receipt, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        receipt_path = base / "receipts" / f"{data['id']}.json"
        if receipt_path.exists():
            if receipt_path.is_symlink() or receipt_path.read_bytes() != receipt_raw:
                print("FAIL: immutable ack receipt conflicts with completed message", file=sys.stderr)
                return 1
        else:
            tmp = base / "tmp" / f".{data['id']}.{uuid.uuid4().hex}.receipt"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, receipt_raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, receipt_path)
                fsync_dir(receipt_path.parent)
            finally:
                tmp.unlink(missing_ok=True)
        audit("ack", agent=args.agent, msg=data["id"], duplicate=duplicate,
              receipt_sha256=hashlib.sha256(receipt_raw).hexdigest())
    print(json.dumps({"status": "duplicate" if duplicate else "acked", "receipt": receipt}, indent=2))
    return 0


def frontmatter_field(text: str, key: str) -> str | None:
    """Read a key from the leading --- frontmatter block only.

    The previous implementation matched the first `key: value` line anywhere in the
    document despite its name, so staleness and session identity were spoofable by body
    prose. Scoping it is a no-op on today's CURRENT.md, where both keys sit in frontmatter.
    """
    m = re.match(r"^---\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", text, re.S)
    if not m:
        return None
    block = m.group(1)
    fm = re.search(rf"^{re.escape(key)}:\s*(.+)$", block, re.M)
    return fm.group(1).strip() if fm else None


def extract_frontmatter_field(text: str, key: str) -> str | None:
    """Back-compat alias."""
    return frontmatter_field(text, key)


KNOWN_AGENTS_FALLBACK = {
    "orchestrator",
    "research",
    "code_generator",
    "security_auditor",
    "peer_reviewer",
    "doc_writer",
    "test_engineer",
    "test_a",
    "test_b",
}

DEFAULT_CONTESTED_PREFIXES = (
    "MemoryBank/CURRENT.md",
    "MemoryBank/coord/next_ids.json",
    "MemoryBank/coord/contested.json",
    "MemoryBank/coord/EXECUTION_GATES.md",
    "EpisodicTracker/state_tracker.json",
    "GraphRAG/schema.json",
    "VectorRAG/index.json",
    "OpenViking/",
    "AGENTS.md",
)

DEFAULT_CONTESTED_BARE_NAMES = (
    "CURRENT.md",
    "next_ids.json",
    "contested.json",
    "EXECUTION_GATES.md",
    "state_tracker.json",
    "schema.json",
    "index.json",
    "AGENTS.md",
)


def contested_config() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Load project-local contested paths from MemoryBank/coord/contested.json."""
    cfg_path = COORD / "contested.json"
    data = load_json(cfg_path, {}) or {}
    prefixes = data.get("prefixes") or list(DEFAULT_CONTESTED_PREFIXES)
    bare = data.get("bare_names") or list(DEFAULT_CONTESTED_BARE_NAMES)
    return tuple(prefixes), tuple(bare)


# Back-compat alias for tests/docs that reference the name
CONTESTED_PREFIXES = DEFAULT_CONTESTED_PREFIXES


def registry_agent_ids() -> set[str]:
    data = load_json(REGISTRY, {}) or {}
    ids = {a.get("id") for a in data.get("agents", []) if a.get("id")}
    return ids or set(KNOWN_AGENTS_FALLBACK)


def registry_agent(agent_id: str) -> dict | None:
    data = load_json(REGISTRY, {}) or {}
    for a in data.get("agents", []):
        if a.get("id") == agent_id:
            return a
    return None


def validate_agent(agent_id: str, *, allow_unknown: bool = False) -> bool:
    # The fallback set used to be unioned unconditionally, so the fixture ids test_a/test_b
    # were valid production agents. Use it only when the registry is missing or unreadable,
    # and admit fixture ids only outside the real repo root.
    data = load_json(REGISTRY, {}) or {}
    registered = {a.get("id") for a in data.get("agents", []) if a.get("id")}
    known = set(registered) if registered else set(KNOWN_AGENTS_FALLBACK)
    if ROOT != REPO_ROOT:
        known |= set(KNOWN_AGENTS_FALLBACK)
    if agent_id in known:
        return True
    if allow_unknown:
        print(f"WARN: unknown agent_id '{agent_id}' (not in registry)", file=sys.stderr)
        return True
    print(f"FAIL: unknown agent_id '{agent_id}'. Known: {', '.join(sorted(known))}", file=sys.stderr)
    return False


def check_write_scopes(agent_id: str, resources: list[Any], *, strict: bool = False) -> bool:
    """Is every resource inside the agent's registry write_scopes?

    Warning-only by default. Turn it on with --strict-scopes once the registry's
    write_scopes actually cover every tree your agents write to; enforcing before that
    would block legitimate writes.
    """
    agent = registry_agent(agent_id)
    if not agent:
        return True
    scopes = [c for c in (canon_or_none(s) for s in (agent.get("write_scopes") or [])) if c]
    if not scopes:
        return True
    ok_all = True
    for res in resources:
        c = res if isinstance(res, Res) else canon_or_none(res)
        if c is None or not any(res_covers(s, c) for s in scopes):
            ok_all = False
            label = c.display if c else res
            print(f"{'FAIL' if strict else 'WARN'}: resource '{label}' outside write_scopes for {agent_id}", file=sys.stderr)
    return ok_all or not strict


def warn_write_scopes(agent_id: str, resources: list[str]) -> None:
    """Back-compat alias."""
    check_write_scopes(agent_id, list(resources))


def read_protocol() -> dict:
    """Active protocol epoch and authority. Missing file means legacy."""
    return load_json(COORD / "protocol.json",
                     {"schema_version": "1.0", "epoch": 1, "authority": "legacy"}) or {}


def authority() -> str:
    return str(read_protocol().get("authority", "legacy"))


def journal_worker():
    spec = importlib.util.spec_from_file_location("_coord_reader", Path(__file__).with_name("commit_worker.py"))
    cw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cw)
    cw.av.configure_paths(ROOT)
    return cw


def journal_meta() -> dict:
    cw = journal_worker()
    seq, digest, problems = cw.verify_chain()
    return {"journal_seq": seq, "journal_hash": digest, "problems": problems}


def journal_state() -> dict:
    cw = journal_worker()
    seq, _, problems = cw.verify_chain()
    if problems:
        raise RuntimeError(f"journal is not clean at valid prefix {seq}: {problems[0]}")
    return cw.fold(cw.read_prefix())


def _journal_proxy() -> Any:
    """Minimal capability object shared with the Ambient Kernel writer adapter."""
    return types.SimpleNamespace(
        ROOT=ROOT, COORD=COORD, iso=iso, atomic_write_json=atomic_write_json,
        atomic_write_text=atomic_write_text, fsync_dir=fsync_dir, FileLock=FileLock,
        audit=audit, load_json=load_json, canon_resource=canon_resource,
        CanonError=CanonError, __file__=__file__,
    )


def _journal_credentials(args: argparse.Namespace) -> tuple[str, str, list[dict]]:
    run_id, _, error = resolve_run(args)
    if error:
        raise PermissionError(error)
    if not run_id:
        raise PermissionError("journal writes require --run and its run token")
    token = getattr(args, "run_token", None) or os.environ.get("AVCOORD_RUN_TOKEN", "")
    proofs = [lease for lease in read_leases()
              if _same_principal(lease, getattr(args, "agent", ""), run_id)]
    return run_id, token, proofs


def _commit_journal(args: argparse.Namespace, event_type: str, body: dict,
                    *, resources: list[str] | None = None,
                    idempotency_key: str = "") -> dict:
    run_id, token, proofs = _journal_credentials(args)
    ak = _load_avkernel()
    return ak.commit_rebased.commit_with_rebase(
        _journal_proxy(), event_type, body, agent_id=args.agent, run_id=run_id,
        run_token=token, lease_proofs=proofs, resources=resources,
        idempotency_key=idempotency_key,
    )


def load_threads() -> list[dict]:
    if authority() == "journal":
        cw = journal_worker()
        state = cw.fold(cw.read_prefix())
        threads = list(state["threads"].values())
        known = {thread.get("id") for thread in threads}
        for task in state["tasks"].values():
            if task.get("task_id") in known:
                continue
            threads.append({
                "id": task.get("task_id"), "title": task.get("objective", "managed task"),
                "owner": task.get("owner_run_id") or task.get("owner") or "",
                "status": task.get("status", ""),
                "notes": f"journal task revision {task.get('revision', 0)}",
            })
        return threads
    data = load_json(COORD / "threads.json", {"threads": []})
    return list(data.get("threads") or []) if isinstance(data, dict) else []


# sync_threads_from_current() was deleted in RFC-WORKSPACE-AEAP-20260906 P02.
# It inferred session completion from an UNANCHORED `re.search(r"`COMPLETE`", text)`, so any
# backticked occurrence of the word anywhere in CURRENT.md — prose, a checklist, a quoted
# example — flipped the session to complete and cascaded that onto thread COORD-001.
# Thread state is now only ever set explicitly; refresh reads threads.json read-only.


def path_is_contested(rel: str) -> bool:
    """Is this path under coordination control?

    Deliberately SYMMETRIC and permissive: a path matches at, below, *and above* a
    contested prefix. Do not "fix" this to be directional for symmetry with
    lease_authorizes — a write or rename targeting `MemoryBank/` would stop being flagged
    even though it swallows `MemoryBank/CURRENT.md`. Authorization is restrictive;
    contested detection is permissive. An uncanonicalizable path is treated as contested.
    """
    c = canon_or_none(rel)
    if c is None:
        return True
    prefixes, bare = contested_config()
    for p in prefixes:
        pc = canon_or_none(p)
        if pc is not None and resources_overlap(c, pc):
            return True
    # Basename match, not endswith: 'reports/notindex.json' must not match 'index.json'.
    leaf = posixpath.basename(c.display)
    for name in bare:
        if _fold(leaf) == _fold(name.strip("/")):
            return True
    return False

def cmd_refresh(args: argparse.Namespace) -> int:
    session_id = args.session or "sess-unknown"
    if CURRENT.exists():
        cur = CURRENT.read_text(encoding="utf-8")
        sid = extract_frontmatter_field(cur, "session_id")
        if sid:
            session_id = sid
    leases = read_leases()
    lease_rows = []
    for L in leases:
        lease_rows.append(
            f"| {L.get('agent_id')} | {', '.join(L.get('resources', []))} | {L.get('expires_at')} |"
        )
    lease_table = "\n".join(lease_rows) if lease_rows else "| _(none)_ | | |"

    mail_counts = []
    if MAIL.exists():
        for agent_dir in sorted(MAIL.iterdir()):
            if not agent_dir.is_dir():
                continue
            n_new = len(list((agent_dir / "new").glob("*.json"))) if (agent_dir / "new").exists() else 0
            n_cur = len(list((agent_dir / "cur").glob("*.json"))) if (agent_dir / "cur").exists() else 0
            if n_new or n_cur:
                mail_counts.append(f"- `{agent_dir.name}`: new={n_new} cur={n_cur}")
    mail_block = "\n".join(mail_counts) if mail_counts else "_No pending mail._"

    # Rendered from contested.json, never a hardcoded literal: a hardcoded list drifts
    # from the real configuration and bakes one workspace's private paths into the code.
    _prefixes, _bare = contested_config()
    contested_block = ("\n".join(f"- `{x}`" for x in _prefixes)
                       if _prefixes else "_None configured._")

    threads = load_threads()  # read-only: refresh never mutates thread state
    # Managed task lifecycle states are open until integrated/cancelled.
    open_states = {"active", "proposed", "ready", "claimed", "in_progress",
                   "review", "verified", "blocked"}
    active = [t for t in threads if t.get("status") in open_states]
    show = active if active else threads
    if show:
        thread_rows = "\n".join(
            f"| {t.get('id')} {t.get('title', '')} | {t.get('owner', '')} | {t.get('status', '')} | {t.get('notes', '')} |"
            for t in show
        )
    else:
        thread_rows = "| _(none)_ | | | |"

    meta = journal_meta()
    stamp = (f"journal_seq: {meta.get('journal_seq')}\njournal_hash: {meta.get('journal_hash')}\n"
             if authority() == "journal" else "")
    board = f"""---
version: 1.0
priority: P0
generated_at: {iso()}
authority: {authority()}
{stamp}type: blackboard
---

# Coordination Board

> Regenerated by `avcoord refresh`. Glance: `avcoord status`.

## Open Threads
| Thread | Owner | Status | Notes |
|--------|-------|--------|-------|
{thread_rows}

## Path Owners (live leases)
| Agent | Resources | Expires |
|-------|-----------|---------|
{lease_table}

## Contested Paths (always claim first)
{contested_block}

## Mail Snapshot
{mail_block}

## Protocol
`MemoryBank/coord/PROTOCOL.quick.md` · full: `PROTOCOL.md`
"""
    atomic_write_text(BOARD, board)

    # CURRENT is a journal projection after cutover. A refresh may redraw it only under a
    # live lease; it never changes the journal context or verification timestamp.
    views_only = getattr(args, "views_only", False)
    agent = getattr(args, "agent", None) or "orchestrator"
    run_id = getattr(args, "run", None) or os.environ.get("AVCOORD_RUN_ID")
    may_write_current = False
    if not views_only:
        for L in read_leases():
            if _same_principal(L, agent, run_id) and any(
                lease_authorizes(r, "MemoryBank/CURRENT.md") for r in L.get("resources") or []
            ):
                may_write_current = True
                break

    if views_only:
        atomic_write_text(ACTIVE, _active_context(session_id))
        audit("refresh_views_only", session_id=session_id, agent=agent, leases=len(leases))
        print("refreshed board + activeContext (views only)")
        return 0

    resolved_run, _, run_error = resolve_run(args)
    if run_error:
        atomic_write_text(ACTIVE, _active_context(session_id))
        print(f"FAIL: {run_error}", file=sys.stderr)
        return 2
    run_id = resolved_run

    if CURRENT.exists() and not may_write_current:
        atomic_write_text(ACTIVE, _active_context(session_id))
        audit("refresh_views_only", session_id=session_id, agent=agent, leases=len(leases))
        print("refreshed board + activeContext (views only)")
        print(
            f"NOTE: CURRENT.md not touched — no live lease for '{agent}'.\n"
            f"  claim it:  python3 scripts/coord/avcoord.py claim --agent {agent} "
            f"--resource MemoryBank/CURRENT.md --ttl 30m --reason '<why>'\n"
            f"  then mark it verified:  python3 scripts/coord/avcoord.py verify --agent {agent} --note '<what you checked>'",
            file=sys.stderr,
        )
        return 2

    if authority() == "journal":
        if not run_id:
            print("FAIL: journal CURRENT refresh requires --run and its token", file=sys.stderr)
            return 2
        try:
            checked_run, run_token, proofs = _journal_credentials(args)
            cw = journal_worker()
            state = cw.fold(cw.read_prefix())
            if not state.get("workspace", {}).get("context"):
                raise RuntimeError("no workspace.context event exists; use `avcoord workspace set`")
            cw.guarded_rebuild_projections(
                agent_id=agent, run_id=checked_run, run_token=run_token,
                lease_proofs=proofs, write_current=True,
            )
        except (ValueError, RuntimeError, PermissionError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 2
        session_id = state["workspace"]["context"]["session_id"]
        atomic_write_text(ACTIVE, _active_context(session_id))
        audit("refresh", session_id=session_id, leases=len(leases), authority="journal")
        print("refreshed board + journal CURRENT projection + activeContext")
        return 0

    if CURRENT.exists():
        text = CURRENT.read_text(encoding="utf-8")
        text2 = re.sub(
            r"^last_updated:.*$",
            f"last_updated: {iso()}",
            text,
            count=1,
            flags=re.M,
        )
        atomic_write_text(CURRENT, text2)
    else:
        atomic_write_text(
            CURRENT,
            f"""---
version: 1.0
last_updated: {iso()}
session_id: {session_id}
stale_after_hours: {STALE_HOURS_DEFAULT}
---

# CURRENT
Session `{session_id}`. Run refresh after major handoffs.
""",
        )

    active = _active_context(session_id)
    atomic_write_text(ACTIVE, active)
    audit("refresh", session_id=session_id, leases=len(leases))
    print("refreshed board + legacy CURRENT + activeContext")
    return 0


def _active_context(session_id: str) -> str:
    meta = journal_meta()
    stamp = (f"journal_seq: {meta.get('journal_seq')}\n"
             if authority() == "journal" else "")
    return f"""---
version: 1.0
priority: P0
generated_at: {iso()}
authority: {authority()}
{stamp}type: volatile_state
derived_from: MemoryBank/CURRENT.md
---

# Active Context (Derived Compat Summary)

> **Derived file.** Prefer `MemoryBank/CURRENT.md` + `board.md` + session detail.
> Regenerated by `python3 scripts/coord/avcoord.py refresh`.

## Current Session
- **session_id:** {session_id}
- **Agent:** orchestrator
- **Refreshed:** {iso()}

## Immediate Next Steps
See `MemoryBank/CURRENT.md`.

## Board
See `MemoryBank/board.md` (leases live; mail summary above).

## Protocol
`MemoryBank/coord/PROTOCOL.md`
"""


def current_age_hours() -> float | None:
    if not CURRENT.exists():
        return None
    text = CURRENT.read_text(encoding="utf-8")
    ts = extract_frontmatter_field(text, "last_updated")
    if not ts:
        return (now() - datetime.fromtimestamp(CURRENT.stat().st_mtime, TZ)).total_seconds() / 3600
    try:
        dt = datetime.fromisoformat(ts.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return (now() - dt).total_seconds() / 3600
    except Exception:
        return None


def cmd_status(args: argparse.Namespace) -> int:
    """Glance dashboard: CURRENT, leases, mail, threads, next IDs, doctor summary."""
    age = current_age_hours()
    stale_h = STALE_HOURS_DEFAULT
    sid = None
    if CURRENT.exists():
        text = CURRENT.read_text(encoding="utf-8")
        sid = extract_frontmatter_field(text, "session_id")
        raw = extract_frontmatter_field(text, "stale_after_hours")
        if raw and raw.isdigit():
            stale_h = int(raw)
    leases = read_live_leases()
    mail = {}
    if MAIL.exists():
        for agent_dir in sorted(MAIL.iterdir()):
            if not agent_dir.is_dir():
                continue
            n_new = len(list((agent_dir / "new").glob("*.json"))) if (agent_dir / "new").exists() else 0
            n_cur = len(list((agent_dir / "cur").glob("*.json"))) if (agent_dir / "cur").exists() else 0
            if n_new or n_cur:
                mail[agent_dir.name] = {"new": n_new, "cur": n_cur}
    threads = load_threads()
    ids = load_json(NEXT_IDS, {})
    doctor_issues = []
    if age is not None and age > stale_h:
        doctor_issues.append(f"STALE CURRENT age {age:.1f}h")
    if (ROOT / "workspace" / "SESSION_STATE.json").exists():
        doctor_issues.append("DUAL_SSOT workspace/SESSION_STATE.json")
    payload = {
        "ts": iso(),
        "session_id": sid,
        "current_age_hours": None if age is None else round(age, 2),
        "stale_after_hours": stale_h,
        "stale": bool(age is not None and age > stale_h),
        "leases": [{"agent": L.get("agent_id"), "resources": L.get("resources"), "expires_at": L.get("expires_at")} for L in leases],
        "mail": mail,
        "threads": threads,
        "next_ids": {k: ids.get(k) for k in ("progress", "episode", "message")},
        "doctor": "FAIL" if doctor_issues else "PASS",
        "doctor_issues": doctor_issues,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(f"session: {sid}  age: {payload['current_age_hours']}h  stale: {payload['stale']}  doctor: {payload['doctor']}")
        if doctor_issues:
            for i in doctor_issues:
                print(f"  ! {i}")
        print(f"next_ids: progress={ids.get('progress')} episode={ids.get('episode')} message={ids.get('message')}")
        print(f"leases: {len(leases)}")
        for L in leases:
            print(f"  - {L.get('agent_id')}: {', '.join(L.get('resources') or [])} until {L.get('expires_at')}")
        print(f"mail: {mail or '{}'}")
        active = [t for t in threads if t.get("status") == "active"]
        print(f"threads active: {len(active)} / total {len(threads)}")
        for t in (active or threads)[:8]:
            print(f"  - {t.get('id')} [{t.get('status')}] {t.get('title')}")
    return 1 if doctor_issues else 0


def _redact_secrets(text: str) -> str:
    """Strip common secret patterns before persisting handoff notes."""
    patterns = [
        (re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S+"), r"\1=<REDACTED>"),
        (re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"), "sk-<REDACTED>"),
        (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+\b"), "Bearer <REDACTED>"),
        (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<GITHUB_TOKEN_REDACTED>"),
        (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<AWS_KEY_REDACTED>"),
        (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "<SLACK_TOKEN_REDACTED>"),
        (re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"), "<GOOGLE_API_KEY_REDACTED>"),
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
         "<PEM_REDACTED>"),
        (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
         "<JWT_REDACTED>"),
        (re.compile(r"/Users/[A-Za-z0-9_.-]+"), "/Users/<REDACTED>"),
        (re.compile(r"/home/[A-Za-z0-9_.-]+"), "/home/<REDACTED>"),
        (re.compile(r"(?i)C:\\Users\\[A-Za-z0-9_.-]+"), r"C:\\Users\\<REDACTED>"),
    ]
    out = text
    for pat, repl in patterns:
        out = pat.sub(repl, out)
    return out


def cmd_checkpoint(args: argparse.Namespace) -> int:
    """Sanitize and flush a 4-vector handoff into MemoryBank/sessions/.

    Does not replace CURRENT.md. Vectors: Active Goal, Ground-Truth Status,
    Exact File & Line, Exact Next Shell/Edit.
    """
    if not validate_agent(args.agent):
        return 1
    sessions = MB / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%Y%m%dT%H%M%S%z")
    sid = extract_frontmatter_field(CURRENT.read_text(encoding="utf-8"), "session_id") if CURRENT.exists() else None
    sid = sid or "unknown"
    goal = _redact_secrets(getattr(args, "goal", None) or args.notes or "")
    status = _redact_secrets(getattr(args, "status", None) or "")
    file_path = _redact_secrets(args.file or "")
    line = args.line
    next_action = _redact_secrets(
        getattr(args, "next", None) or args.remaining or args.blockers or ""
    )
    blockers = _redact_secrets(args.blockers or "")
    body = (
        f"---\n"
        f"type: handoff_checkpoint\n"
        f"schema: four_vector_v1\n"
        f"created_at: {iso()}\n"
        f"agent: {args.agent}\n"
        f"session_id: {sid}\n"
        f"authority: none\n"
        f"---\n\n"
        f"# Handoff checkpoint\n\n"
        f"**Session:** `{sid}` · **Agent:** `{args.agent}`\n\n"
        f"## 1. Active Goal\n\n{goal or '_empty_'}\n\n"
        f"## 2. Ground-Truth Status\n\n"
        f"{status or '_unknown — run bin/avcoord gate_'}\n\n"
        f"## 3. Exact File & Line\n\n"
        f"`{file_path or '(none)'}`"
        f"{f':{line}' if line is not None else ''}\n\n"
        f"## 4. Exact Next Shell/Edit\n\n{next_action or '_none_'}\n\n"
        f"## Blockers\n\n{blockers or '_none_'}\n\n"
        f"## Boot for next model\n\n"
        f"1. `AGENTS.md` → `CURRENT.md` → `board.md`\n"
        f"2. This handoff (4-vector)\n"
        f"3. Chat is not SSOT\n"
    )
    out = sessions / f"handoff-{stamp}.md"
    atomic_write_text(out, body)
    audit("checkpoint", agent=args.agent, path=str(out.relative_to(ROOT)), session_id=sid)
    print(json.dumps({"ok": True, "path": str(out.relative_to(ROOT)), "session_id": sid}, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    issues: list[str] = []
    warns: list[str] = []
    if (ROOT / "AGENTVAULT_INSTALL_PENDING.json").exists():
        issues.append("INSTALL_RECOVERY_REQUIRED: complete `avcoord init --target . --recover resume|rollback` before using this software version")

    for p in (COORD / "PROTOCOL.md", CURRENT, BOARD, NEXT_IDS, REGISTRY):
        if not p.exists():
            issues.append(f"MISSING {p.relative_to(ROOT)}")

    hook = ROOT / "workspace" / "SESSION_STATE.json"
    if hook.exists():
        issues.append(
            "DUAL_SSOT: workspace/SESSION_STATE.json exists — AgentVault MemoryBank is the only SSOT; "
            "do not treat AGENT HOOK state as co-equal"
        )
    nested = ROOT / ".agentvault" / "MemoryBank"
    if nested.exists():
        issues.append(
            "DUAL_SSOT: .agentvault/MemoryBank exists — nest rejected (ADR-003); "
            "MemoryBank at project root is the only SSOT"
        )
    active_ssot = MB / "active"
    if active_ssot.exists():
        issues.append(
            "DUAL_SSOT: MemoryBank/active/ exists — rejected (ADR-003/005); "
            "use coord/specs/ for optional elevated notes only"
        )

    gates = COORD / "EXECUTION_GATES.md"
    if not gates.exists():
        issues.append("MISSING MemoryBank/coord/EXECUTION_GATES.md")
    else:
        gtext = gates.read_text(encoding="utf-8")
        for heading in ("Elevate", "Done means exit 0", "Handoff", "Monotonic rigor"):
            if heading not in gtext:
                issues.append(f"GATES_SECTION_MISSING: {heading}")

    if (COORD / "COGNITIVE_KERNEL.md").exists():
        warns.append(
            "legacy MemoryBank/coord/COGNITIVE_KERNEL.md present — prefer EXECUTION_GATES.md (ADR-005)"
        )

    reviews_readme = COORD / "reviews" / "README.md"
    if not reviews_readme.exists():
        warns.append("MemoryBank/coord/reviews/README.md missing (non-fatal)")

    age = current_age_hours()
    stale_h = STALE_HOURS_DEFAULT
    if CURRENT.exists():
        raw = extract_frontmatter_field(CURRENT.read_text(encoding="utf-8"), "stale_after_hours")
        if raw and raw.isdigit():
            stale_h = int(raw)
    if age is None:
        issues.append("CURRENT.md missing or unreadable timestamp")
    elif age > stale_h:
        issues.append(f"STALE: CURRENT.md age {age:.1f}h > stale_after_hours {stale_h}")
    else:
        warns.append(f"CURRENT age {age:.1f}h (ok, threshold {stale_h}h)")

    if authority() == "journal":
        try:
            cw = journal_worker()
            events = cw.read_prefix()
            seq, digest, journal_problems = cw.verify_chain()
            if journal_problems:
                issues.append(f"JOURNAL_INVALID: {journal_problems[0]}")
            state = cw.fold(events)
            proto = read_protocol()
            committed = state.get("authority") or {}
            if (committed.get("current") != "journal"
                    or committed.get("epoch") != int(proto.get("epoch", 1))):
                issues.append("JOURNAL_AUTHORITY_MISMATCH: protocol and committed authority disagree")
            workspace = state.get("workspace") or {}
            context = workspace.get("context")
            if not context:
                issues.append("JOURNAL_CONTEXT_MISSING: no workspace.context event")
            if not workspace.get("last_verified_at"):
                issues.append("JOURNAL_UNVERIFIED: no workspace.verified event")
            current_text = CURRENT.read_text(encoding="utf-8") if CURRENT.is_file() else ""
            raw_seq = extract_frontmatter_field(current_text, "journal_seq")
            current_hash = extract_frontmatter_field(current_text, "journal_hash")
            if not raw_seq or not raw_seq.isdigit() or not current_hash:
                issues.append("JOURNAL_CURRENT_INVALID: CURRENT lacks journal sequence/hash metadata")
            else:
                current_seq = int(raw_seq)
                by_seq = {event["seq"]: event["hash"] for event in events}
                if current_seq < 1 or current_seq > seq or by_seq.get(current_seq) != current_hash:
                    issues.append("JOURNAL_CURRENT_INVALID: CURRENT references no valid journal prefix")
                required_seq = max(
                    int((context or {}).get("journal_seq", 0)),
                    int(workspace.get("verification_journal_seq", 0)),
                )
                if current_seq < required_seq:
                    issues.append("JOURNAL_CURRENT_STALE: CURRENT predates workspace state")
            expected_verified = workspace.get("last_verified_at") or "null"
            if extract_frontmatter_field(current_text, "last_verified_at") != expected_verified:
                issues.append("JOURNAL_CURRENT_INVALID: verification timestamp is not journal-derived")
            meta_path = COORD / "projections" / "_meta.json"
            meta = load_json(meta_path) if meta_path.is_file() else {}
            if (meta.get("journal_seq") != seq or meta.get("journal_hash") != digest
                    or meta.get("authority") != "journal"):
                issues.append("JOURNAL_PROJECTION_STALE: projection metadata does not match journal head")
            actual_current_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
            if meta.get("current_sha256") != actual_current_hash:
                issues.append("JOURNAL_CURRENT_TAMPERED: CURRENT hash differs from projection metadata")
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            issues.append(f"JOURNAL_HEALTH_ERROR: {error}")

    for jp in (NEXT_IDS, REGISTRY):
        if jp.exists():
            try:
                load_json(jp)
            except Exception as e:
                issues.append(f"BROKEN_JSON {jp.relative_to(ROOT)}: {e}")
    schema = ROOT / "GraphRAG" / "schema.json"
    if schema.exists():
        try:
            load_json(schema)
        except Exception as e:
            issues.append(f"BROKEN_JSON {schema.relative_to(ROOT)}: {e}")

    ids = load_json(NEXT_IDS, {})
    for k in ("progress", "episode", "message"):
        if k not in ids:
            issues.append(f"next_ids missing key {k}")

    # Health checks are read-only: reaping moved to `avcoord reap` and cmd_claim.

    # Ghost leases: live lease on a missing concrete path → FAIL (ADR-007)
    for L in read_live_leases():
        for res in L.get("resources") or []:
            if not isinstance(res, str):
                continue
            raw = res.strip()
            if not raw or "*" in raw:
                continue  # globs may name future trees
            check = raw.rstrip("/")
            if not (ROOT / check).exists():
                issues.append(
                    f"GHOST_LEASE: {raw} (missing path; agent={L.get('agent_id')})"
                )

    # CURRENT pointer budget (WARN only — ADR-007)
    CURRENT_WORD_BUDGET = 112
    if CURRENT.exists():
        try:
            cur_words = len(CURRENT.read_text(encoding="utf-8").split())
            if cur_words > CURRENT_WORD_BUDGET:
                warns.append(
                    f"CURRENT_WORD_BUDGET: {cur_words} words > {CURRENT_WORD_BUDGET} (ADR-007 pointer)"
                )
        except OSError:
            pass

    # Mail backlog warning (hygiene, non-fatal)
    if MAIL.exists():
        cur_total = 0
        for agent_dir in MAIL.iterdir():
            cur = agent_dir / "cur"
            if cur.is_dir():
                cur_total += len(list(cur.glob("*.json")))
        if cur_total > MAIL_CUR_WARN:
            warns.append(f"mail cur backlog={cur_total} (threshold {MAIL_CUR_WARN})")

    # Two-hop navigation (lab contract). Failures are issues when WORKSPACE_INDEX exists.
    nav_script = ROOT / "scripts" / "nav" / "check_links.py"
    if (ROOT / "WORKSPACE_INDEX.md").exists() and nav_script.exists():
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location("_av_nav_check", nav_script)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            nav = mod.check(ROOT)
            h1b = len(nav.get("hop1", {}).get("broken") or [])
            h2b = len(nav.get("hop2", {}).get("broken") or [])
            miss = nav.get("missing_mentions") or []
            if not nav.get("ok"):
                issues.append(
                    f"NAV_LINKS: hop1_broken={h1b} hop2_broken={h2b} "
                    f"missing_mentions={miss or []}"
                )
            else:
                warns.append(
                    f"nav links ok (hop1={nav['hop1']['total']} hop2={nav['hop2']['total']})"
                )
            for w in (nav.get("warnings") or [])[:5]:
                warns.append(f"nav: {w}")
        except Exception as e:
            warns.append(f"nav check skipped: {e}")

    status = "FAIL" if issues else "PASS"
    report = {"status": status, "issues": issues, "warnings": warns, "ts": iso()}
    print(json.dumps(report, indent=2))
    return 1 if issues else 0


def cmd_gate(args: argparse.Namespace) -> int:
    """Deterministic DONE gate: doctor + pytest + nav + compileall + import-resolve.

    This is not `verify` (CURRENT freshness stamp). Conversational COMPLETED is invalid
    unless this command exits 0.
    """
    paths = [str(p) for p in (getattr(args, "paths", None) or [])]
    full = bool(getattr(args, "full", False))
    force_nav = bool(getattr(args, "nav", False))
    results: list[dict[str, Any]] = []

    drc = cmd_doctor(args)
    results.append({"step": "doctor", "rc": drc})
    if drc != 0:
        print(json.dumps({"ok": False, "results": results}, indent=2))
        audit("gate", ok=False, results=results)
        return 1

    normalized_paths = [p.replace("\\", "/") for p in paths]
    suites = [
        ("scripts/coord", ROOT / "scripts" / "coord" / "tests"),
        ("aeap", ROOT / "aeap" / "tests"),
        ("scripts/catalog", ROOT / "scripts" / "catalog" / "tests"),
        ("scripts/eval", ROOT / "scripts" / "eval" / "tests"),
        ("scripts/release", ROOT / "scripts" / "release" / "tests"),
    ]
    test_roots = [test_root for prefix, test_root in suites
                  if full or not paths or any(prefix in path for path in normalized_paths)]

    for tr in test_roots:
        if not tr.is_dir():
            continue
        r = subprocess.run(
            [sys.executable, "-m", "pytest", str(tr), "-q", "--tb=line"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-1:] or [""]
        results.append({"step": f"pytest:{tr.relative_to(ROOT)}", "rc": r.returncode, "tail": tail[0]})
        if r.returncode != 0:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
            print(json.dumps({"ok": False, "results": results}, indent=2))
            audit("gate", ok=False, results=results)
            return 1

    nav = ROOT / "scripts" / "nav" / "check_links.py"
    if (force_nav or (ROOT / "WORKSPACE_INDEX.md").exists()) and nav.exists() and (ROOT / "WORKSPACE_INDEX.md").exists():
        nr = subprocess.run(
            [sys.executable, str(nav)], cwd=str(ROOT), capture_output=True, text=True
        )
        results.append({"step": "nav", "rc": nr.returncode})
        if nr.returncode != 0:
            print(nr.stdout)
            print(nr.stderr, file=sys.stderr)
            print(json.dumps({"ok": False, "results": results}, indent=2))
            audit("gate", ok=False, results=results)
            return 1

    compile_roots: list[Path] = []
    if paths:
        for p in paths:
            if "aeap" in p.replace("\\", "/"):
                compile_roots.append(ROOT / "aeap")
            if "scripts/coord" in p.replace("\\", "/"):
                compile_roots.append(ROOT / "scripts" / "coord")
            for prefix in ("scripts/catalog", "scripts/eval", "scripts/release"):
                if prefix in p.replace("\\", "/"):
                    compile_roots.append(ROOT / prefix)
            pp = ROOT / p
            if pp.suffix == ".py" and pp.exists():
                compile_roots.append(pp.parent)
    else:
        compile_roots.extend([ROOT / "scripts" / "coord", ROOT / "aeap",
                              ROOT / "scripts" / "catalog", ROOT / "scripts" / "eval",
                              ROOT / "scripts" / "release"])
    seen: set[Path] = set()
    for cr in compile_roots:
        rp = cr.resolve()
        if rp in seen or not rp.exists():
            continue
        seen.add(rp)
        with tempfile.TemporaryDirectory(prefix="avcoord-pycache-") as pycache:
            r = subprocess.run(
                [sys.executable, "-m", "compileall", "-q", str(rp)],
                cwd=str(ROOT), capture_output=True, text=True,
                env={**os.environ, "PYTHONPYCACHEPREFIX": pycache},
            )
        rel = str(rp.relative_to(ROOT)) if ROOT in rp.parents or rp == ROOT.resolve() else rp.name
        results.append({"step": f"compileall:{rel}", "rc": r.returncode})
        if r.returncode != 0:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
            print(json.dumps({"ok": False, "results": results}, indent=2))
            audit("gate", ok=False, results=results)
            return 1

    # AST import resolve (compileall ≠ resolve — catch hallucinated top-level imports)
    import_resolve = ROOT / "scripts" / "eval" / "import_resolve.py"
    if import_resolve.is_file() and seen:
        ir_args = [sys.executable, str(import_resolve)]
        for rp in seen:
            try:
                ir_args.append(str(rp.relative_to(ROOT)))
            except ValueError:
                ir_args.append(str(rp))
        ir = subprocess.run(ir_args, cwd=str(ROOT), capture_output=True, text=True)
        results.append({"step": "import_resolve", "rc": ir.returncode})
        if ir.returncode != 0:
            print(ir.stdout)
            print(ir.stderr, file=sys.stderr)
            print(json.dumps({"ok": False, "results": results}, indent=2))
            audit("gate", ok=False, results=results)
            return 1

    print(json.dumps({"ok": True, "results": results}, indent=2))
    audit("gate", ok=True, results=results)
    return 0


# ---------------------------------------------------------------------------
# Sandbox + tests
# ---------------------------------------------------------------------------

def _assert_disposable(path: Path) -> None:
    """Refuse to recursively delete anything that is not a throwaway temp root."""
    rp = Path(os.path.realpath(str(path)))
    if rp == Path(os.path.realpath(str(REPO_ROOT))):
        raise RuntimeError(f"refusing to rmtree the repo root: {rp}")
    if (rp / ".git").exists():
        raise RuntimeError(f"refusing to rmtree a git working tree: {rp}")
    tmp = Path(os.path.realpath(tempfile.gettempdir()))
    if tmp not in rp.parents:
        raise RuntimeError(f"refusing to rmtree outside {tmp}: {rp}")


def _init_sandbox(sandbox: Path) -> None:
    """Create a minimal AgentVault layout under sandbox and bind paths to it."""
    _assert_disposable(sandbox)
    if sandbox.exists():
        shutil.rmtree(sandbox)
    for sub in (
        "MemoryBank/coord/leases",
        "MemoryBank/coord/mail",
        "MemoryBank/agents",
        "MemoryBank/sessions",
        "EpisodicTracker",
        "GraphRAG",
        ".cursor/rules",
        ".github",
    ):
        (sandbox / sub).mkdir(parents=True, exist_ok=True)

    # PROTOCOL stub (doctor requires it)
    (sandbox / "MemoryBank/coord/PROTOCOL.md").write_text(
        "# PROTOCOL\nMemoryBank/coord/PROTOCOL.md\n", encoding="utf-8"
    )
    (sandbox / "MemoryBank/coord/EXECUTION_GATES.md").write_text(
        "# EXECUTION GATES\n"
        "## Elevate\n"
        "## Done means exit 0\n"
        "## Review without theater\n"
        "## Handoff (4-vector)\n"
        "## Monotonic rigor\n",
        encoding="utf-8",
    )
    (sandbox / "MemoryBank/coord/specs").mkdir(parents=True, exist_ok=True)
    (sandbox / "MemoryBank/coord/reviews").mkdir(parents=True, exist_ok=True)
    (sandbox / "MemoryBank/coord/reviews/README.md").write_text(
        "# reviews\n", encoding="utf-8"
    )
    atomic_write_json(
        sandbox / "MemoryBank/coord/next_ids.json",
        {"schema_version": "1.0", "progress": 1, "episode": 1, "message": 1, "last_updated": iso()},
    )
    atomic_write_json(
        sandbox / "MemoryBank/coord/contested.json",
        {
            "schema_version": "1.0",
            "prefixes": list(DEFAULT_CONTESTED_PREFIXES) + ["sandbox/", "race/", "perf/", "contested/"],
            "bare_names": list(DEFAULT_CONTESTED_BARE_NAMES),
        },
    )
    (sandbox / "MemoryBank/coord/audit.jsonl").write_text("", encoding="utf-8")
    atomic_write_json(
        sandbox / "MemoryBank/agents/registry.json",
        {
            "schema_version": "1.0",
            "agents": [
                {"id": "orchestrator", "write_scopes": ["MemoryBank/**", "scripts/**"]},
                {"id": "research", "write_scopes": ["MemoryBank/agents/research/**", "VectorRAG/**"]},
                {"id": "code_generator", "write_scopes": ["MemoryBank/agents/code_generator/**", "scripts/**", "contested/**"]},
                {"id": "test_a", "write_scopes": ["sandbox/**", "race/**", "perf/**"]},
                {"id": "test_b", "write_scopes": ["sandbox/**", "race/**"]},
            ],
        },
    )
    for aid in ("orchestrator", "research", "code_generator", "test_a", "test_b"):
        d = sandbox / "MemoryBank/agents" / aid
        d.mkdir(parents=True, exist_ok=True)
        (d / "context.md").write_text(f"agent_id: {aid}\n", encoding="utf-8")
        for box in ("tmp", "new", "cur", "done"):
            (sandbox / "MemoryBank/coord/mail" / aid / box).mkdir(parents=True, exist_ok=True)

    ts = iso()
    (sandbox / "MemoryBank/CURRENT.md").write_text(
        f"""---
last_updated: {ts}
session_id: sess-sandbox
stale_after_hours: 48
---
# CURRENT sandbox
""",
        encoding="utf-8",
    )
    (sandbox / "MemoryBank/board.md").write_text("# board\n", encoding="utf-8")
    (sandbox / "MemoryBank/activeContext.md").write_text("# active\n", encoding="utf-8")
    atomic_write_json(sandbox / "GraphRAG/schema.json", {"nodes": [], "edges": []})

    # Thin wrappers for D1
    for rel, body in [
        ("AGENTS.md", "Boot MemoryBank/coord/PROTOCOL.md\n"),
        ("AGENTS.codex.md", "Follow AGENTS.md Boot exactly.\n"),
        (".cursorrules", "MemoryBank/coord/PROTOCOL.md\n"),
        (".cursor/rules/agentvault.mdc", "MemoryBank/coord/PROTOCOL.md\n"),
        (".github/copilot-instructions.md", "MemoryBank/coord/PROTOCOL.md\n"),
        (".windsurfrules", "MemoryBank/coord/PROTOCOL.md\n"),
        ("GEMINI.md", "MemoryBank/coord/PROTOCOL.md\n"),
    ]:
        p = sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    configure_paths(sandbox)
    os.environ["AVCOORD_ROOT"] = str(sandbox)


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _run_smoke(results: list[tuple[str, bool, str]]) -> None:
    def ok(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, cond, detail))
        print(("PASS" if cond else "FAIL"), name, detail)

    r1 = f"sandbox/A/{uuid.uuid4().hex}/file.md"
    rc = cmd_claim(_ns(agent="test_a", resource=[r1], ttl="1m", reason="A1", task_id="T-A"))
    ok("A1_claim", rc == 0)
    rc2 = cmd_claim(_ns(agent="test_b", resource=[r1], ttl="1m", reason="conflict", task_id="T-B"))
    ok("A2_conflict_denied", rc2 == 1)
    lp = lease_path(r1)
    lease = load_json(lp)
    lease["expires_at"] = iso(now() - timedelta(seconds=5))
    atomic_write_json(lp, lease)
    rc3 = cmd_claim(_ns(agent="test_b", resource=[r1], ttl="1m", reason="reap", task_id="T-B"))
    ok("A3_expired_reclaim", rc3 == 0)
    cmd_release(_ns(agent="test_b", resource=[r1], all=False))

    ids = [allocate_id("message") for _ in range(5)]
    nums = [int(x.split("-")[1]) for x in ids]
    ok("B1_monotonic", nums == sorted(nums) and len(set(nums)) == 5, str(ids))

    cmd_post(
        _ns(
            from_agent="orchestrator",
            to_agent="research",
            type="assign",
            summary="test assign",
            refs="MemoryBank/coord/PROTOCOL.md",
            task_id="T-C",
            hop=0,
        )
    )
    cmd_recv(_ns(agent="research"))
    cur_files = list((MAIL / "research" / "cur").glob("MSG-*.json"))
    ok("C1_mail_recv", len(cur_files) >= 1)
    msg = load_json(cur_files[-1])
    cmd_ack(_ns(agent="research", msg=msg["id"]))
    ok("C2_mail_ack", (MAIL / "research" / "done" / f"{msg['id']}.json").exists())
    peer_ctx = MB / "agents" / "research" / "context.md"
    before = peer_ctx.read_text(encoding="utf-8") if peer_ctx.exists() else ""
    ok("C3_private_context_exists", peer_ctx.exists() and "research" in before)

    wrappers = [
        ROOT / "AGENTS.md",
        ROOT / "AGENTS.codex.md",
        ROOT / ".cursorrules",
        ROOT / ".cursor" / "rules" / "agentvault.mdc",
        ROOT / ".github" / "copilot-instructions.md",
        ROOT / ".windsurfrules",
        ROOT / "GEMINI.md",
    ]
    present = [p for p in wrappers if p.exists()]

    def _points_boot(text: str) -> bool:
        return (
            "AGENTS.md" in text
            or "MemoryBank/coord/PROTOCOL" in text
            or "PROTOCOL.quick" in text
        )

    all_point = all(_points_boot(p.read_text(encoding="utf-8")) for p in present)
    ok("D1_wrappers_point_protocol", all_point and len(present) == 7, f"{len(present)} files")

    tpath = COORD / "_chaos.json"
    atomic_write_json(tpath, {"ok": True})
    ok("E1_atomic_write", tpath.exists() and load_json(tpath).get("ok") is True)
    junk = COORD / ".chaos.json.partial.tmp"
    junk.write_text("{broken", encoding="utf-8")
    ok("E2_tmp_not_canonical", load_json(tpath).get("ok") is True)

    backup = CURRENT.read_text(encoding="utf-8")
    try:
        stale_body = f"""---
last_updated: {(now() - timedelta(hours=72)).isoformat(timespec='seconds')}
stale_after_hours: 48
session_id: sess-stale-test
---
# STALE TEST
"""
        atomic_write_text(CURRENT, stale_body)
        hook_dir = ROOT / "workspace"
        hook_dir.mkdir(exist_ok=True)
        hook = hook_dir / "SESSION_STATE.json"
        hook.write_text('{"schema_version":"1.0","note":"fixture"}', encoding="utf-8")
        rc = cmd_doctor(_ns())
        ok("F1_doctor_fail_stale_or_dual", rc == 1)
        hook.unlink(missing_ok=True)
        try:
            hook_dir.rmdir()
        except OSError:
            pass
    finally:
        atomic_write_text(CURRENT, backup)

    rc_ok = cmd_doctor(_ns())
    ok("F2_doctor_pass_restored", rc_ok == 0)


def _run_effectiveness(results: list[tuple[str, bool, str]], timings: dict[str, float]) -> None:
    def ok(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, cond, detail))
        print(("PASS" if cond else "FAIL"), name, detail)

    t0 = time.perf_counter()
    path = "contested/manuscript/vol1.md"
    ok("EFF1_claim", cmd_claim(_ns(agent="code_generator", resource=[path], ttl="15m", reason="edit", task_id="E2E")) == 0)
    ok(
        "EFF2_assign",
        cmd_post(
            _ns(
                from_agent="orchestrator",
                to_agent="research",
                type="assign",
                summary="review vol1",
                refs=path,
                task_id="E2E",
                hop=0,
            )
        )
        == 0,
    )
    cmd_recv(_ns(agent="research"))
    cur = list((MAIL / "research" / "cur").glob("MSG-*.json"))
    ok("EFF3_recv", len(cur) >= 1)
    mid = load_json(cur[-1])["id"]
    ok("EFF4_ack", cmd_ack(_ns(agent="research", msg=mid)) == 0)
    ok(
        "EFF5_done",
        cmd_post(
            _ns(
                from_agent="research",
                to_agent="orchestrator",
                type="done",
                summary="reviewed",
                refs=path,
                task_id="E2E",
                hop=1,
            )
        )
        == 0,
    )
    before_exp = load_json(lease_path(path))["expires_at"]
    time.sleep(0.05)
    ok("EFF6_renew", cmd_renew(_ns(agent="code_generator", ttl="30m")) == 0)
    after_exp = load_json(lease_path(path))["expires_at"]
    ok("EFF7_renew_extends", after_exp > before_exp, f"{before_exp} -> {after_exp}")
    # Refresh is views-only unless the caller holds a lease on CURRENT.md: exit 2 and
    # leave CURRENT untouched, exit 0 once the lease exists.
    cur_before = CURRENT.read_text(encoding="utf-8") if CURRENT.exists() else ""
    rc_unleased = cmd_refresh(_ns(session="", agent="code_generator", views_only=False, run=None))
    ok(
        "EFF8_refresh_unleased_is_views_only",
        rc_unleased == 2 and (CURRENT.read_text(encoding="utf-8") if CURRENT.exists() else "") == cur_before,
        f"rc={rc_unleased}",
    )
    cmd_claim(
        _ns(
            agent="orchestrator",
            resource=["MemoryBank/CURRENT.md"],
            ttl="10m",
            reason="refresh",
            task_id="T-REFRESH",
            run=None,
            strict_scopes=False,
        )
    )
    ok(
        "EFF8b_refresh_leased",
        cmd_refresh(_ns(session="", agent="orchestrator", views_only=False, run=None)) == 0,
    )
    cmd_release(_ns(agent="orchestrator", resource=["MemoryBank/CURRENT.md"], all=False, run=None, token=None))
    board = BOARD.read_text(encoding="utf-8")
    ok("EFF9_board_lease", "code_generator" in board and path in board)
    ok("EFF10_doctor", cmd_doctor(_ns()) == 0)
    cmd_release(_ns(agent="code_generator", resource=[path], all=False))
    timings["effectiveness_s"] = time.perf_counter() - t0


def _run_accuracy(results: list[tuple[str, bool, str]]) -> None:
    def ok(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, cond, detail))
        print(("PASS" if cond else "FAIL"), name, detail)

    # Conflict matrix (pure function)
    ok("ACC1_exact", resources_conflict("a/b", "a/b"))
    ok("ACC2_glob_child", resources_conflict("dir/**", "dir/child.md"))
    ok("ACC3_siblings", not resources_conflict("vol1", "vol2"))
    ok("ACC4_no_false_prefix", not resources_conflict("foo", "foobar"))
    ok("ACC5_parent_child", resources_conflict("path", "path/child"))

    # Release validation
    rc = cmd_release(_ns(agent="test_a", resource=None, all=False))
    ok("ACC6_release_requires_args", rc == 1)

    # Hop cap
    rc = cmd_post(
        _ns(
            from_agent="orchestrator",
            to_agent="research",
            type="assign",
            summary="too many hops",
            refs="",
            task_id="HOP",
            hop=8,
        )
    )
    ok("ACC7_hop_cap", rc == 1)

    # Missing ack
    rc = cmd_ack(_ns(agent="research", msg="MSG-DOES-NOT-EXIST"))
    ok("ACC8_ack_missing", rc == 1)

    # Concurrent overlapping claims via subprocess (separate processes, shared sandbox)
    base = f"race/{uuid.uuid4().hex}"
    env = os.environ.copy()
    env["AVCOORD_ROOT"] = str(ROOT)
    script = str(Path(__file__).resolve())

    def claim_proc(agent: str, resource: str) -> int:
        return subprocess.run(
            [sys.executable, script, "claim", "--agent", agent, "--resource", resource, "--ttl", "5m"],
            env=env,
            capture_output=True,
            text=True,
        ).returncode

    # path vs path/child in parallel
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(claim_proc, "test_a", base)
        f2 = ex.submit(claim_proc, "test_b", f"{base}/child")
        codes = [f1.result(), f2.result()]
    winners = sum(1 for c in codes if c == 0)
    ok("ACC9_overlap_one_winner", winners == 1, f"codes={codes}")
    cmd_release(_ns(agent="test_a", all=True, resource=None))
    cmd_release(_ns(agent="test_b", all=True, resource=None))

    # Multiprocess next-id uniqueness
    n_workers, per = 20, 5
    env = os.environ.copy()
    env["AVCOORD_ROOT"] = str(ROOT)

    def worker(_: int) -> list[str]:
        out = []
        for _i in range(per):
            r = subprocess.run(
                [sys.executable, script, "next-id", "message"],
                env=env,
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                out.append(r.stdout.strip().splitlines()[-1])
        return out

    got: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = [ex.submit(worker, i) for i in range(n_workers)]
        for f in as_completed(futs):
            got.extend(f.result())
    ok("ACC10_id_unique", len(got) == n_workers * per and len(set(got)) == len(got), f"n={len(got)} uniq={len(set(got))}")


def _run_performance(results: list[tuple[str, bool, str]], timings: dict[str, float]) -> None:
    def ok(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, cond, detail))
        print(("PASS" if cond else "FAIL"), name, detail)

    # next-id latency
    samples = []
    for _ in range(200):
        t0 = time.perf_counter()
        allocate_id("message")
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = sorted(samples)[int(0.95 * len(samples)) - 1]
    timings["next_id_p95_ms"] = p95
    timings["next_id_mean_ms"] = statistics.mean(samples)
    ok("PERF1_next_id_p95", p95 < 50.0, f"p95={p95:.2f}ms mean={timings['next_id_mean_ms']:.2f}ms")

    t0 = time.perf_counter()
    for i in range(50):
        r = f"perf/claim/{i}.md"
        cmd_claim(_ns(agent="test_a", resource=[r], ttl="5m", reason="perf", task_id="P"))
        cmd_release(_ns(agent="test_a", resource=[r], all=False))
    elapsed = time.perf_counter() - t0
    timings["claim_release_50_s"] = elapsed
    ok("PERF2_claim_release_50", elapsed < 2.0, f"{elapsed:.3f}s")

    t0 = time.perf_counter()
    for i in range(100):
        cmd_post(
            _ns(
                from_agent="orchestrator",
                to_agent="research",
                type="inform",
                summary=f"perf {i}",
                refs="",
                task_id="PERF",
                hop=0,
            )
        )
        cmd_recv(_ns(agent="research"))
        cur = sorted((MAIL / "research" / "cur").glob("MSG-*.json"))
        if cur:
            cmd_ack(_ns(agent="research", msg=cur[-1].stem))
    elapsed = time.perf_counter() - t0
    timings["mail_100_s"] = elapsed
    ok("PERF3_mail_100", elapsed < 3.0, f"{elapsed:.3f}s")


def _run_chaos(results: list[tuple[str, bool, str]]) -> None:
    def ok(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, cond, detail))
        print(("PASS" if cond else "FAIL"), name, detail)

    lock = COORD / "next_ids.lock"
    lock.write_text("stale\n", encoding="utf-8")
    # Force stale mtime
    old = time.time() - 60
    os.utime(lock, (old, old))
    try:
        v = allocate_id("message")
        ok("CHAOS1_stale_lock_recover", v.startswith("MSG-"), v)
    except Exception as e:
        ok("CHAOS1_stale_lock_recover", False, str(e))

    tpath = COORD / "chaos_canon.json"
    atomic_write_json(tpath, {"v": 1})
    (COORD / ".chaos_canon.json.partial.tmp").write_text("{nope", encoding="utf-8")
    ok("CHAOS2_tmp_ignored", load_json(tpath) == {"v": 1})


def _production_fingerprint(root: Path) -> dict:
    """Everything `avcoord test` must leave untouched.

    The old check compared only next_ids.json's `message` counter, which is why the suite
    could append to the production audit ledger and still report no pollution.
    """
    fp: dict[str, Any] = {}
    for rel in (
        "MemoryBank/coord/next_ids.json",
        "MemoryBank/coord/threads.json",
        "MemoryBank/coord/contested.json",
        "MemoryBank/CURRENT.md",
    ):
        f = root / rel
        fp[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.exists() else None
    audit_f = root / "MemoryBank/coord/audit.jsonl"
    fp["audit_bytes"] = audit_f.stat().st_size if audit_f.exists() else None
    leases = root / "MemoryBank/coord/leases"
    fp["leases"] = sorted(x.name for x in leases.glob("*.json")) if leases.exists() else []
    mail = root / "MemoryBank/coord/mail"
    fp["mail_open"] = (
        sorted(str(x.relative_to(mail)) for box in ("new", "cur") for x in mail.glob(f"*/{box}/*.json"))
        if mail.exists()
        else []
    )
    return fp


def cmd_test(args: argparse.Namespace) -> int:
    """Sandboxed smoke and optional trail suites. Never mutates production AVCOORD_ROOT."""
    production_root = REPO_ROOT
    prod_before = _production_fingerprint(production_root)

    sandbox = Path(tempfile.mkdtemp(prefix="avcoord_trail_"))
    results: list[tuple[str, bool, str]] = []
    timings: dict[str, float] = {}
    report_lines: list[str] = []

    try:
        _init_sandbox(sandbox)
        print(f"SANDBOX {sandbox}")

        # Always sandboxed smoke; --trail adds deeper suites
        print("=== SMOKE A–F ===")
        _run_smoke(results)

        if args.trail:
            print("=== EFFECTIVENESS ===")
            _run_effectiveness(results, timings)
            print("=== ACCURACY ===")
            _run_accuracy(results)
            print("=== PERFORMANCE ===")
            _run_performance(results, timings)
            print("=== CHAOS ===")
            _run_chaos(results)

        failed = [r for r in results if not r[1]]
        print("---")
        print(f"{len(results) - len(failed)}/{len(results)} passed")
        for name, cond, detail in results:
            report_lines.append(f"| {name} | {'PASS' if cond else 'FAIL'} | {detail} |")

        # Audit the run into the SANDBOX ledger. This call used to sit after the
        # restore below, so `avcoord test` appended to the production audit.jsonl.
        audit(
            "test_matrix",
            passed=len(results) - len(failed),
            total=len(results),
            failed=[f[0] for f in failed],
            timings=timings,
            sandbox=str(sandbox),
        )

        # Restore process to production paths for pollution check
        configure_paths(production_root)
        os.environ.pop("AVCOORD_ROOT", None)
        if "AVCOORD_ROOT" in os.environ:
            del os.environ["AVCOORD_ROOT"]
        configure_paths(production_root)

        prod_after = _production_fingerprint(production_root)
        drift = sorted(k for k in prod_before if prod_before[k] != prod_after.get(k))
        no_pollute = not drift
        detail = "clean" if no_pollute else f"drifted={drift}"
        results.append(("REGRESSION_no_prod_pollution", no_pollute, detail))
        print(("PASS" if no_pollute else "FAIL"), "REGRESSION_no_prod_pollution", detail)
        if not no_pollute:
            failed.append(("REGRESSION_no_prod_pollution", False, detail))

        # Persist report payload for caller (stdout JSON trailer)
        summary = {
            "passed": len(results) - len(failed),
            "total": len(results),
            "failed": [f[0] for f in failed],
            "timings": timings,
            "rows": report_lines,
        }
        print("TRAIL_SUMMARY_JSON=" + json.dumps(summary))
        return 1 if failed else 0
    finally:
        configure_paths(production_root)
        os.environ.pop("AVCOORD_ROOT", None)
        shutil.rmtree(sandbox, ignore_errors=True)


# --------------------------------------------------------------------------- AK-SPWS (ADR-009)


def _load_avkernel():
    """Import scripts/coord/avkernel as a package without requiring install."""
    pkg_dir = Path(__file__).resolve().parent / "avkernel"
    pkg_init = pkg_dir / "__init__.py"
    name = "avkernel"
    if name in sys.modules and getattr(sys.modules[name], "__file__", None) == str(pkg_init):
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        pkg_init,
        submodule_search_locations=[str(pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def cmd_hydrate(args: argparse.Namespace) -> int:
    """JIT working-set digest — prefer this over dumping PROTOCOL + full commits."""
    ak = _load_avkernel()

    class _AvProxy:
        ROOT = ROOT
        COORD = COORD
        iso = staticmethod(iso)
        atomic_write_json = staticmethod(atomic_write_json)
        __file__ = __file__

    result = ak.hydrate.hydrate(
        _AvProxy,
        task_id=args.task or "",
        budget=args.budget,
        tags=args.tag or [],
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result["digest"])
        print(f"\n# token_estimate={result['token_estimate']} budget={result['budget']} "
              f"head_seq={result['head_seq']}")
    return 0


def cmd_intent(args: argparse.Namespace) -> int:
    ak = _load_avkernel()
    try:
        run_id, token, proofs = _journal_credentials(args)
        receipt = ak.commit_rebased.intent_commit(
            _journal_proxy(), agent_id=args.agent, note=args.note,
            task_id=args.task_id or "", expect_slot=args.expect_slot,
            idempotency_key=args.idempotency_key or "", run_id=run_id,
            run_token=token, lease_proofs=proofs,
        )
    except (ValueError, RuntimeError, PermissionError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    return 0 if receipt.get("status") in ("committed", "duplicate") else 1


def cmd_query(args: argparse.Namespace) -> int:
    ak = _load_avkernel()

    class _AvProxy:
        ROOT = ROOT
        COORD = COORD
        iso = staticmethod(iso)
        atomic_write_json = staticmethod(atomic_write_json)
        __file__ = __file__

    cw = journal_worker()
    slot = next((v for v in ak.commit_rebased.journal_slots(cw) if v.get("id") == args.slot), None)
    if slot is None:
        print(json.dumps({"status": "missing", "slot": args.slot}))
        return 1
    print(json.dumps({"status": "ok", "slot": slot}, indent=2, ensure_ascii=False))
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    """Run gate; optionally compact if threshold met. Ambient post-task hook."""
    gate_rc = cmd_gate(_ns(full=False, nav=False, paths=[]))
    ak = _load_avkernel()

    class _AvProxy:
        ROOT = ROOT
        COORD = COORD
        iso = staticmethod(iso)
        atomic_write_json = staticmethod(atomic_write_json)
        atomic_write_text = staticmethod(atomic_write_text)
        fsync_dir = staticmethod(fsync_dir)
        FileLock = FileLock
        audit = staticmethod(audit)
        load_json = staticmethod(load_json)
        canon_resource = staticmethod(canon_resource)
        CanonError = CanonError
        __file__ = __file__

    check = ak.compact.should_compact(_AvProxy, force=False)
    compact_receipt = None
    if check.get("due"):
        try:
            run_id, token, proofs = _journal_credentials(args)
            compact_receipt = ak.compact.compact(
                _AvProxy, force=False, agent_id=args.agent, run_id=run_id,
                run_token=token, lease_proofs=proofs)
        except PermissionError as error:
            compact_receipt = {"status": "error", "reason": str(error)}
    out = {
        "gate": gate_rc,
        "task_id": args.task,
        "compact_check": {k: check[k] for k in check if k != "problems"},
        "compact": compact_receipt,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    compact_failed = bool(compact_receipt and compact_receipt.get("status") not in {
        "compacted", "skipped", "duplicate",
    })
    return 1 if gate_rc or compact_failed else 0


def cmd_compact(args: argparse.Namespace) -> int:
    ak = _load_avkernel()
    try:
        run_id, token, proofs = _journal_credentials(args)
        receipt = ak.compact.compact(
            _journal_proxy(), force=args.force, agent_id=args.agent, run_id=run_id,
            run_token=token, lease_proofs=proofs)
    except PermissionError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    return 0 if receipt.get("status") in ("compacted", "skipped") else 1


def cmd_fingerprint(args: argparse.Namespace) -> int:
    ak = _load_avkernel()

    class _AvProxy:
        ROOT = ROOT
        COORD = COORD
        iso = staticmethod(iso)
        atomic_write_json = staticmethod(atomic_write_json)
        atomic_write_text = staticmethod(atomic_write_text)
        fsync_dir = staticmethod(fsync_dir)
        FileLock = FileLock
        audit = staticmethod(audit)
        load_json = staticmethod(load_json)
        canon_resource = staticmethod(canon_resource)
        CanonError = CanonError
        __file__ = __file__

    if args.fp_cmd == "status":
        print(json.dumps(ak.fingerprint_eval.status(_AvProxy), indent=2))
        return 0
    if args.fp_cmd == "eval":
        path = Path(args.path)
        if not path.is_absolute():
            path = ROOT / path
        try:
            run_id, token, proofs = _journal_credentials(args)
            receipt = ak.fingerprint_eval.promote_or_reject(
                _AvProxy, path, agent_id=args.agent, run_id=run_id,
                run_token=token, lease_proofs=proofs,
            )
        except (ValueError, RuntimeError, PermissionError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 1
        print(json.dumps(receipt, indent=2, ensure_ascii=False, default=str))
        return 0 if receipt.get("status") == "approved" else 1
    print(json.dumps({"error": f"unknown fingerprint subcommand {args.fp_cmd}"}))
    return 1


def _read_bounded_json(path_value: str, *, max_bytes: int = 1024 * 1024) -> dict:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    if path.is_symlink() or not path.is_file():
        raise ValueError("JSON input must be one regular file")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"JSON input exceeds {max_bytes} bytes")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("JSON input must contain one object")
    return value


def cmd_task(args: argparse.Namespace) -> int:
    """Create, transition, or inspect journal-authoritative managed tasks."""
    try:
        state = journal_state()
        if args.task_cmd == "list":
            print(json.dumps({"tasks": list(state["tasks"].values())}, indent=2, ensure_ascii=False))
            return 0
        if args.task_cmd == "show":
            task = state["tasks"].get(args.task_id)
            if task is None:
                print(json.dumps({"status": "missing", "task_id": args.task_id}))
                return 1
            print(json.dumps(task, indent=2, ensure_ascii=False))
            return 0
        if authority() != "journal":
            raise RuntimeError("managed task writes require journal authority")
        if args.task_cmd == "create":
            body = _read_bounded_json(args.file)
            receipt = _commit_journal(
                args, "task.created", body,
                idempotency_key=args.idempotency_key or f"task-created-{body.get('task_id', '')}",
            )
        elif args.task_cmd == "transition":
            body = {"task_id": args.task_id, "status": args.status}
            if args.note:
                body["note"] = args.note
            if args.evidence_ref:
                body["evidence_refs"] = args.evidence_ref
            if args.reviewer_receipt:
                body["reviewer_receipt"] = args.reviewer_receipt
            if args.integration_evidence:
                body["integration_evidence"] = args.integration_evidence
            if args.blocker:
                body["blockers"] = args.blocker
            receipt = _commit_journal(
                args, "task.transition", body, idempotency_key=args.idempotency_key or "",
            )
        elif args.task_cmd == "review":
            task = state["tasks"].get(args.task_id)
            if task is None:
                raise ValueError(f"unknown managed task {args.task_id}")
            body = {
                "task_id": args.task_id,
                "task_revision": task.get("revision"),
                "task_state_sha256": journal_worker().task_state_sha256(task),
                "decision": args.decision,
                "evidence_refs": args.evidence_ref,
            }
            receipt = _commit_journal(
                args, "review.receipt", body, idempotency_key=args.idempotency_key or "",
            )
        else:
            raise ValueError(f"unknown task command {args.task_cmd}")
    except (OSError, ValueError, RuntimeError, PermissionError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    return 0 if receipt.get("status") in {"committed", "duplicate"} else 1


def cmd_workspace(args: argparse.Namespace) -> int:
    """Set or inspect the journal context projected as CURRENT.md."""
    try:
        if args.workspace_cmd == "show":
            print(json.dumps(journal_state()["workspace"], indent=2, ensure_ascii=False))
            return 0
        if args.workspace_cmd != "set":
            raise ValueError(f"unknown workspace command {args.workspace_cmd}")
        body = _read_bounded_json(args.file)
        receipt = _commit_journal(
            args, "workspace.context", body,
            idempotency_key=args.idempotency_key or "",
        )
    except (OSError, ValueError, RuntimeError, PermissionError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    return 0 if receipt.get("status") in {"committed", "duplicate"} else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="avcoord",
        description="AgentVault coordination CLI",
        epilog="Examples: avcoord status | avcoord claim --agent research --resource VectorRAG/index.json | avcoord test --trail",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("claim", help="TTL lease on contested path(s)")
    c.add_argument("--agent", required=True)
    c.add_argument("--resource", action="append", required=True)
    c.add_argument("--ttl", default="15m")
    c.add_argument("--reason", default="")
    c.add_argument("--task-id", default="", dest="task_id")
    c.add_argument("--run", default=None, help="Registered run id (see `run start`)")
    c.add_argument("--strict-scopes", action="store_true", dest="strict_scopes")

    r = sub.add_parser("release", help="Release lease(s); requires --all or --resource")
    r.add_argument("--agent", required=True)
    r.add_argument("--resource", action="append")
    r.add_argument("--all", action="store_true")
    r.add_argument("--run", default=None)
    r.add_argument("--token", default=None, help="Lease token; must match if the lease has one")

    n = sub.add_parser("renew", help="Extend all leases for agent")
    n.add_argument("--agent", required=True)
    n.add_argument("--ttl", default="15m")
    n.add_argument("--run", default=None)
    n.add_argument("--token", default=None, help="Lease token; must match if the lease has one")

    run = sub.add_parser("run", help="Register and list run instances")
    runsub = run.add_subparsers(dest="run_cmd", required=True)
    rs = runsub.add_parser("start", help="Register a run; capabilities bind to it, not to a role name")
    rs.add_argument("--role", required=True)
    rs.add_argument("--runtime", required=True, help="cursor|claude-code|codex|windsurf|gemini|...")
    rs.add_argument("--task", default="")
    rs.add_argument("--ttl-hours", type=float, default=None, dest="ttl_hours")
    runsub.add_parser("list", help="List active runs")

    co = sub.add_parser("cutover", help="P07 — move operational authority (journal <-> legacy)")
    co.add_argument("--agent", default="orchestrator")
    co.add_argument("--to", choices=["journal", "legacy"], required=True)
    co.add_argument("--rfc", default="RFC-WORKSPACE-AEAP-20260906")
    co.add_argument("--task-id", default="TASK-AUTHORITY-CUTOVER", dest="task_id",
                    help="Task id recorded on the transition event")
    co.add_argument("--dry-run", action="store_true", dest="dry_run")
    co.add_argument("--force", action="store_true", help="cut over despite other live leases")
    co.add_argument("--run", required=True, help="Registered maintenance run")

    rp = sub.add_parser("reap", help="Remove expired leases (the only command that does)")
    rp.add_argument("--agent", default="orchestrator")

    post = sub.add_parser("post", help="Send maildir message")
    post.add_argument("--from", dest="from_agent", required=True)
    post.add_argument("--to", dest="to_agent", required=True)
    post.add_argument(
        "--type",
        required=True,
        choices=["assign", "handoff", "done", "block", "review_request", "review_feedback", "ack", "inform"],
    )
    post.add_argument("--summary", required=True)
    post.add_argument("--refs", default="")
    post.add_argument("--task-id", default="", dest="task_id")
    post.add_argument("--hop", type=int, default=0,
                      help="Ignored when --parent is given: the server derives the hop from the parent")
    post.add_argument("--parent", default=None, help="Parent message id; hop is derived from it")
    post.add_argument("--intent", default="", help="Routing intent, e.g. aeap.evaluate")
    post.add_argument("--idempotency-key", default="", dest="idempotency_key")
    post.add_argument("--run", default=None)

    recv = sub.add_parser("recv", help="Move new→cur; summaries by default")
    recv.add_argument("--agent", required=True)
    recv.add_argument("--full", action="store_true", help="Print full message JSON bodies")
    recv.add_argument("--resume", action="store_true", help="Also list owned unfinished messages in cur/")

    ack = sub.add_parser("ack", help="Move cur→done after side effects")
    ack.add_argument("--agent", required=True)
    ack.add_argument("--msg", required=True)

    nid = sub.add_parser("next-id", help="Allocate monotonic progress|episode|message id")
    nid.add_argument("kind", choices=["progress", "episode", "message"])

    ref = sub.add_parser("refresh", help="Rebuild board + derived activeContext from CURRENT/leases/mail")
    ref.add_argument("--views-only", action="store_true", dest="views_only",
                     help="Regenerate board/activeContext only; never touch CURRENT.md")
    ref.add_argument("--agent", default="orchestrator")
    ref.add_argument("--run", default=None)

    ver = sub.add_parser(
        "verify",
        help="Stamp CURRENT last_verified_at (requires lease). NOT a test runner — use `gate`.",
    )
    ver.add_argument("--agent", required=True)
    ver.add_argument("--run", default=None)
    ver.add_argument("--note", required=True, help="What was actually checked")
    ref.add_argument("--session", default="")

    cp = sub.add_parser(
        "checkpoint",
        help="Sanitize 4-vector handoff under MemoryBank/sessions/",
    )
    cp.add_argument("--agent", required=True)
    cp.add_argument("--notes", default="", help="Alias for --goal (compat)")
    cp.add_argument("--goal", default="", help="1. Active Goal")
    cp.add_argument("--status", default="", help="2. Ground-Truth Status (e.g. gate PASS)")
    cp.add_argument("--file", default="", help="3. Exact file path")
    cp.add_argument("--line", type=int, default=None, help="3. Exact line")
    cp.add_argument("--next", dest="next", default="", help="4. Exact next shell/edit")
    cp.add_argument("--blockers", default="", help="Open blockers")
    cp.add_argument("--remaining", default="", help="Alias for --next (compat)")

    gate = sub.add_parser(
        "gate",
        help="Deterministic DONE check: doctor + pytest + nav/compileall/import-resolve (verify ≠ gate)",
    )
    gate.add_argument("--full", action="store_true", help="Always run aeap/tests too")
    gate.add_argument("--nav", action="store_true", help="Force nav link check")
    gate.add_argument(
        "--paths",
        action="append",
        default=[],
        help="Touched paths (repeatable); aeap/ prefix pulls aeap tests; used for compileall",
    )

    st = sub.add_parser("status", help="Glance dashboard (CURRENT, leases, mail, threads)")
    st.add_argument("--json", action="store_true", dest="json")

    sub.add_parser("doctor", help="Health check (STALE, dual-SSOT, gates, JSON, nav links)")

    t = sub.add_parser("test", help="Sandboxed smoke; --trail adds full matrix")
    t.add_argument("--smoke", action="store_true", help="Run A–F smoke (always run)")
    t.add_argument("--trail", action="store_true", help="Run effectiveness/accuracy/performance/chaos trails")

    chk = sub.add_parser("check-lease", help="Exit 0 if path covered by live lease for agent (enforcement helper)")
    chk.add_argument("--run", default=None)
    chk.add_argument("--agent", required=True)
    chk.add_argument("--path", required=True)

    ini = sub.add_parser("init", help="Scaffold AgentVault into a target directory (from portable template)")
    ini.add_argument("--target", default=".", help="Destination project root (default: cwd)")
    ini.add_argument("--force", action="store_true", help="Deprecated: unsafe overwrites are rejected; use --upgrade")
    ini.add_argument("--full", action="store_true", help="Add OpenViking + GraphRAG + VectorRAG to the default core")
    ini.add_argument("--with-aeap", action="store_true", help="Add the optional AEAP development extension (admission remains disabled)")
    ini.add_argument("--upgrade", action="store_true", help="Upgrade unchanged managed software; preserve user state and local overrides")
    ini.add_argument("--rollback", metavar="RECEIPT", help="Restore 'latest' or a retained receipt digest; preserve all user state")
    ini.add_argument("--recover", choices=("resume", "rollback"), help="Resume or undo a retained interrupted software transaction")
    ini.add_argument("--override", action="append", default=[], metavar="PATH", help="Declare a path project-owned; never overwrite it on upgrades")
    ini.add_argument("--dry-run", "--preview", dest="dry_run", action="store_true", help="Print a write-free plan; conflicts exit nonzero")

    rot = sub.add_parser("rotate-events", help="HOT→WARM: archive old events.jsonl lines; keep last N")
    rot.add_argument("--keep", type=int, default=50, help="HOT retention (default 50)")

    # AK-SPWS verbs (ADR-009) — minimal cognitive surface.
    hy = sub.add_parser("hydrate", help="JIT working-set digest (snapshot+δ+rules); prefer over PROTOCOL dump")
    hy.add_argument("--task", default="", help="Focus task id")
    hy.add_argument("--budget", type=int, default=None, help="Token budget (default from kernel config)")
    hy.add_argument("--tag", action="append", default=[], help="Rule tags to prefer (repeatable)")
    hy.add_argument("--json", action="store_true", help="Print full JSON envelope")

    intent = sub.add_parser("intent", help="Record intent with optional slot CAS + journal rebase")
    intent.add_argument("--agent", required=True)
    intent.add_argument("--note", required=True)
    intent.add_argument("--task-id", default="", dest="task_id")
    intent.add_argument("--expect-slot", default=None, dest="expect_slot",
                        help="kind:id=vN — CAS before journal")
    intent.add_argument("--idempotency-key", default="", dest="idempotency_key")
    intent.add_argument("--run", required=True)

    task = sub.add_parser("task", help="Journal-authoritative managed task lifecycle")
    task_sub = task.add_subparsers(dest="task_cmd", required=True)
    task_create = task_sub.add_parser("create", help="Create from a complete task contract JSON")
    task_create.add_argument("--file", required=True)
    task_create.add_argument("--agent", required=True)
    task_create.add_argument("--run", required=True)
    task_create.add_argument("--idempotency-key", default="", dest="idempotency_key")
    task_transition = task_sub.add_parser("transition", help="Advance one managed task")
    task_transition.add_argument("--task-id", required=True, dest="task_id")
    task_transition.add_argument("--status", required=True, choices=[
        "ready", "claimed", "in_progress", "review", "verified", "integrated",
        "blocked", "cancelled",
    ])
    task_transition.add_argument("--agent", required=True)
    task_transition.add_argument("--run", required=True)
    task_transition.add_argument("--note", default="")
    task_transition.add_argument("--evidence-ref", action="append", default=[])
    task_transition.add_argument("--reviewer-receipt", default="")
    task_transition.add_argument("--integration-evidence", action="append", default=[])
    task_transition.add_argument("--blocker", action="append", default=[])
    task_transition.add_argument("--idempotency-key", default="", dest="idempotency_key")
    task_review = task_sub.add_parser("review", help="Commit an independent review receipt")
    task_review.add_argument("--task-id", required=True, dest="task_id")
    task_review.add_argument("--decision", required=True, choices=["ACK", "BLOCK"])
    task_review.add_argument("--evidence-ref", action="append", required=True)
    task_review.add_argument("--agent", required=True)
    task_review.add_argument("--run", required=True)
    task_review.add_argument("--idempotency-key", default="", dest="idempotency_key")
    task_show = task_sub.add_parser("show", help="Read one managed task projection")
    task_show.add_argument("--task-id", required=True, dest="task_id")
    task_sub.add_parser("list", help="List managed task projections")

    workspace = sub.add_parser("workspace", help="Journal context projected as CURRENT.md")
    workspace_sub = workspace.add_subparsers(dest="workspace_cmd", required=True)
    workspace_set = workspace_sub.add_parser("set", help="Commit a workspace.context JSON object")
    workspace_set.add_argument("--file", required=True)
    workspace_set.add_argument("--agent", required=True)
    workspace_set.add_argument("--run", required=True)
    workspace_set.add_argument("--idempotency-key", default="", dest="idempotency_key")
    workspace_sub.add_parser("show", help="Read the folded workspace context")

    q = sub.add_parser("query", help="Read one kernel slot")
    q.add_argument("--slot", required=True, help="kind:id")

    done = sub.add_parser("done", help="Run gate; compact if threshold met (post-task)")
    done.add_argument("--task", required=True)
    done.add_argument("--agent", default="orchestrator")
    done.add_argument("--run", required=True)

    comp = sub.add_parser("compact", help="Snapshot journal if due (or --force)")
    comp.add_argument("--force", action="store_true")
    comp.add_argument("--agent", default="orchestrator")
    comp.add_argument("--run", required=True)

    fp = sub.add_parser("fingerprint", help="Hypothesis/Candidate/Approved promotion eval")
    fps = fp.add_subparsers(dest="fp_cmd", required=True)
    fe = fps.add_parser("eval", help="Evaluate Candidate file; promote or reject")
    fe.add_argument("path", help="Path to candidate JSON")
    fe.add_argument("--agent", default="orchestrator")
    fe.add_argument("--run", required=True)
    fps.add_parser("status", help="Count fingerprint stages")
    def credentials(parser):
        if any(a.dest == "run" for a in parser._actions):
            parser.add_argument("--run-token", help="run secret (or AVCOORD_RUN_TOKEN)")
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    credentials(child)
    credentials(p)
    return p


def cmd_verify(args: argparse.Namespace) -> int:
    """Stamp last_verified_at on CURRENT.md. Requires a live lease on it.

    Separating this from refresh is the point: `generated_at` measures view regeneration,
    `last_verified_at` measures that a human or agent actually checked the state. Refresh
    can no longer rejuvenate a stale pointer as a side effect of redrawing the board.
    """
    if not validate_agent(args.agent):
        return 1
    if not CURRENT.exists():
        print("FAIL: MemoryBank/CURRENT.md does not exist", file=sys.stderr)
        return 1
    run_id, _, error = resolve_run(args)
    if error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    holds = any(
        _same_principal(L, args.agent, run_id)
        and any(lease_authorizes(r, "MemoryBank/CURRENT.md") for r in L.get("resources") or [])
        for L in read_leases()
    )
    if not holds:
        print(
            f"FAIL: '{args.agent}' holds no live lease authorizing MemoryBank/CURRENT.md",
            file=sys.stderr,
        )
        return 1
    if authority() == "journal":
        try:
            if not journal_state().get("workspace", {}).get("context"):
                raise RuntimeError("no workspace.context event exists; use `avcoord workspace set` first")
            receipt = _commit_journal(
                args, "workspace.verified", {"note": args.note},
                idempotency_key="",
            )
        except (ValueError, RuntimeError, PermissionError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 1
        if receipt.get("status") not in {"committed", "duplicate"}:
            print(json.dumps(receipt, indent=2, ensure_ascii=False))
            return 1
        workspace = journal_state()["workspace"]
        print(json.dumps({"ok": True, "last_verified_at": workspace["last_verified_at"],
                          "note": args.note, "journal": receipt}, indent=2))
        return 0

    # Before journal cutover, CURRENT itself remains the selected legacy authority.
    stamp = iso()
    text = CURRENT.read_text(encoding="utf-8")
    m = re.match(r"^(---\r?\n)(.*?)(\r?\n---\s*\r?\n)", text, re.S)
    if not m:
        print("FAIL: CURRENT.md has no frontmatter block", file=sys.stderr)
        return 1
    block = m.group(2)
    if re.search(r"^last_verified_at:", block, re.M):
        block = re.sub(r"^last_verified_at:.*$", f"last_verified_at: {stamp}", block, count=1, flags=re.M)
    else:
        block = block + f"\nlast_verified_at: {stamp}"
    block = (
        re.sub(r"^last_updated:.*$", f"last_updated: {stamp}", block, count=1, flags=re.M)
        if re.search(r"^last_updated:", block, re.M)
        else block + f"\nlast_updated: {stamp}"
    )
    atomic_write_text(CURRENT, m.group(1) + block + m.group(3) + text[m.end():])
    audit("verify", agent=args.agent, note=args.note, last_verified_at=stamp)
    print(json.dumps({"ok": True, "last_verified_at": stamp, "note": args.note}, indent=2))
    return 0


def cmd_cutover(args: argparse.Namespace) -> int:
    """Change authority in one fenced event and preserve complete recovery evidence."""
    if args.force:
        print("FAIL: --force is disabled; authority changes require a fully drained workspace",
              file=sys.stderr)
        return 1
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.rfc):
        print("FAIL: invalid RFC identifier", file=sys.stderr)
        return 1
    try:
        run_id, run_token, proofs = _journal_credentials(args)
    except PermissionError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    cw = journal_worker()
    proto = read_protocol()
    target = args.to
    current = proto.get("authority", "legacy")
    valid_seq, head_hash, problems = cw.verify_chain()
    if problems:
        print(f"FAIL: journal is not clean: {problems[0]}", file=sys.stderr)
        return 1
    committed_authority = cw.fold(cw.read_prefix())["authority"]
    if current == target:
        if (committed_authority["current"] != target
                or committed_authority["epoch"] != int(proto.get("epoch", 1))):
            print("FAIL: protocol and committed authority disagree", file=sys.stderr)
            return 1
        if target == "journal":
            state = cw.fold(cw.read_prefix())
            if not state.get("workspace", {}).get("context"):
                print("FAIL: journal authority has no workspace.context event", file=sys.stderr)
                return 1
            if args.dry_run:
                print(json.dumps({"status": "dry_run", "authority": target,
                                  "would_rebuild": ["MemoryBank/coord/projections/**",
                                                    "MemoryBank/CURRENT.md"]}, indent=2))
                return 0
            try:
                cw.guarded_rebuild_projections(
                    agent_id=args.agent, run_id=run_id, run_token=run_token,
                    lease_proofs=proofs, write_current=True,
                )
            except (ValueError, RuntimeError, PermissionError) as error:
                print(f"FAIL: {error}", file=sys.stderr)
                return 1
            print(f"authority is already {target!r}; journal projections repaired and verified")
            return 0
        print(f"authority is already {target!r}; journal and epoch agree")
        return 0
    if target == "journal" and valid_seq == 0:
        print("FAIL: journal is empty; import legacy state before cutting over", file=sys.stderr)
        return 1
    if target == "journal" and not cw.fold(cw.read_prefix()).get("workspace", {}).get("context"):
        print("FAIL: stage workspace context with `avcoord workspace set` before cutover",
              file=sys.stderr)
        return 1

    others = [lease for lease in read_leases()
              if not _same_principal(lease, args.agent, run_id)]
    if others:
        for lease in others:
            print(f"FAIL: lease held by {lease.get('agent_id')} on {lease.get('resources')} "
                  f"until {lease.get('expires_at')}", file=sys.stderr)
        return 1

    epoch_from = int(proto.get("epoch", 1))
    epoch_to = epoch_from + 1
    kind = "pre-cutover-snapshot" if target == "journal" else "post-cutover-export"
    recovery_rel = (f"MemoryBank/coord/migrations/{args.rfc}/cutover/"
                    f"{kind}-epoch-{epoch_from}-to-{epoch_to}.json")
    recovery_path = ROOT / recovery_rel
    if target == "journal":
        files = {}
        for rel in ("MemoryBank/coord/threads.json", "MemoryBank/CURRENT.md",
                    "MemoryBank/board.md", "MemoryBank/activeContext.md"):
            source = ROOT / rel
            if source.is_file() and not source.is_symlink():
                raw = source.read_bytes()
                files[rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                              "content_base64": base64.b64encode(raw).decode("ascii")}
        recovery_doc = {"schema_version": "2.0", "kind": kind, "captured_at": iso(),
                        "from_authority": current, "to_authority": target,
                        "epoch_from": epoch_from, "epoch_to": epoch_to,
                        "journal_seq": valid_seq, "journal_hash": head_hash, "files": files,
                        "note": "Recovery evidence only; never a live boot target."}
    else:
        payloads = {}
        for path in sorted(cw.payloads_dir().glob("*.json")):
            if path.is_symlink() or not path.is_file():
                print(f"FAIL: invalid journal payload path {path}", file=sys.stderr)
                return 1
            raw = path.read_bytes()
            payloads[path.name] = {"sha256": hashlib.sha256(raw).hexdigest(),
                                   "content_base64": base64.b64encode(raw).decode("ascii")}
        events = cw.read_prefix()
        recovery_doc = {"schema_version": "2.0", "kind": kind, "captured_at": iso(),
                        "from_authority": current, "to_authority": target,
                        "epoch_from": epoch_from, "epoch_to": epoch_to,
                        "journal_seq": valid_seq, "journal_hash": head_hash,
                        "events": events, "payloads": payloads, "folded_state": cw.fold(events),
                        "note": "Complete legacy handoff; journal history remains preserved."}

    if recovery_path.exists():
        if recovery_path.is_symlink() or not recovery_path.is_file():
            print("FAIL: recovery artifact path is not a regular file", file=sys.stderr)
            return 1
        recovery_raw = recovery_path.read_bytes()
        try:
            existing_doc = json.loads(recovery_raw)
        except json.JSONDecodeError:
            print("FAIL: existing recovery artifact is invalid JSON", file=sys.stderr)
            return 1
        expected_shape = (existing_doc.get("from_authority"), existing_doc.get("to_authority"),
                          existing_doc.get("epoch_from"), existing_doc.get("epoch_to"))
        if expected_shape != (current, target, epoch_from, epoch_to):
            print("FAIL: existing recovery artifact belongs to a different transition", file=sys.stderr)
            return 1
        recovery_doc = existing_doc
    else:
        recovery_raw = (json.dumps(recovery_doc, indent=2, ensure_ascii=False) + "\n").encode()
    recovery_hash = "sha256:" + hashlib.sha256(recovery_raw).hexdigest()
    body = {"from_authority": current, "to_authority": target,
            "epoch_from": epoch_from, "epoch_to": epoch_to,
            "recovery_artifact": recovery_rel,
            "recovery_artifact_sha256": recovery_hash}
    idempotency_key = f"authority-e{epoch_from}-{current}-to-{target}"

    if args.dry_run:
        print(json.dumps({"status": "dry_run", **body, "journal_seq": valid_seq,
                          "journal_hash": head_hash, "would_write": [recovery_rel]}, indent=2))
        return 0

    if not any(any(lease_authorizes(resource, recovery_rel)
                       for resource in lease.get("resources") or []) for lease in proofs):
        print(f"FAIL: missing live lease authorizing {recovery_rel}", file=sys.stderr)
        return 1

    with coord_lock():
        latest = read_protocol()
        if (latest.get("authority", "legacy"), int(latest.get("epoch", 1))) != (current, epoch_from):
            print("FAIL: protocol changed after preflight", file=sys.stderr)
            return 1
        # Claims serialize on this same lock. Repeating the drain here closes the window
        # in which another worker could start after preflight and be cut over underneath.
        locked_others = [lease for lease in read_leases()
                         if not _same_principal(lease, args.agent, run_id)]
        if locked_others:
            for lease in locked_others:
                print(f"FAIL: lease acquired during cutover by {lease.get('agent_id')} on "
                      f"{lease.get('resources')} until {lease.get('expires_at')}", file=sys.stderr)
            return 1
        try:
            cw.authorize(cw.required_resources("authority.changed", body) + [recovery_rel],
                         args.agent, run_id, run_token, proofs)
        except (ValueError, RuntimeError, PermissionError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 1
        if recovery_path.exists():
            if recovery_path.read_bytes() != recovery_raw:
                print("FAIL: recovery artifact changed after preflight", file=sys.stderr)
                return 1
        else:
            recovery_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(recovery_path, recovery_raw.decode("utf-8"))

        existing = next((event for event in cw.read_prefix()
                         if event.get("idempotency_key") == idempotency_key), None)
        if existing is not None:
            if existing.get("body") != body:
                print("FAIL: prior authority event conflicts with recovery artifact", file=sys.stderr)
                return 1
            receipt = {"status": "duplicate", "seq": existing["seq"], "hash": existing["hash"]}
        else:
            receipt = _commit_journal(args, "authority.changed", body,
                                      resources=[recovery_rel], idempotency_key=idempotency_key)
        if receipt.get("status") not in {"committed", "duplicate"}:
            print(json.dumps(receipt, indent=2), file=sys.stderr)
            return 1
        proto.update({"epoch": epoch_to, "authority": target, "last_updated": iso(),
                      "cutover_commit_seq": receipt.get("seq"),
                      "cutover_recovery_artifact": recovery_rel})
        atomic_write_json(COORD / "protocol.json", proto)
        cw.rebuild_projections(write_current=target == "journal")

    audit("authority_cutover", **{"from": current, "to": target, "epoch": epoch_to,
                                  "seq": receipt.get("seq"), "recovery": recovery_rel})
    print(json.dumps({"status": "cutover_complete", **body,
                      "journal_seq": receipt.get("seq")}, indent=2))
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    """Remove expired leases. The only command with that side effect.

    Reaping used to happen inside read_live_leases(), so every status / doctor /
    check-lease call silently deleted lease files, concurrently with a lock-holding claim.
    """
    with coord_lock():
        before = len(list(LEASES.glob("*.json")))
        live = read_leases(reap=True)
    print(json.dumps({"live": len(live), "reaped": before - len(list(LEASES.glob("*.json")))}, indent=2))
    return 0


def cmd_check_lease(args: argparse.Namespace) -> int:
    """Fail-closed helper for hooks: contested paths need a live lease for --agent."""
    if not validate_agent(args.agent):
        return 1
    # Fail closed. The previous bare `except: pass` left an unrelativizable path absolute,
    # which path_is_contested then reported as not contested — an exit-0 bypass reachable
    # from any case-variant or symlinked spelling of the workspace root.
    try:
        target = canon_resource(args.path)
    except CanonError as e:
        # A path outside the workspace is simply not this coordinator's jurisdiction — the
        # hook must let it through, or every edit elsewhere on the machine gets blocked.
        # A traversal escape or a malformed/globbed resource IS an attack surface: deny.
        if e.code == "OUTSIDE_ROOT":
            print(json.dumps({"ok": True, "contested": False, "path": args.path,
                              "reason": "outside workspace root"}))
            return 0
        print(
            json.dumps({"ok": False, "contested": True, "path": args.path, "error": f"{e.code}: {e}"}),
            file=sys.stderr,
        )
        return 1
    rel = target.display
    if not path_is_contested(rel):
        print(json.dumps({"ok": True, "contested": False, "path": rel}))
        return 0
    run_id, _, error = resolve_run(args)
    if error:
        print(json.dumps({"ok": False, "error": error}), file=sys.stderr)
        return 1
    for L in read_leases():
        if not _same_principal(L, args.agent, run_id):
            continue
        for ores in L.get("resources") or []:
            # DIRECTIONAL: the lease must cover the target. It never authorizes the
            # target's ancestor, nor a sibling that merely shares a string prefix.
            if lease_authorizes(ores, target):
                print(json.dumps({"ok": True, "contested": True, "path": rel, "lease": ores}))
                return 0
    print(json.dumps({"ok": False, "contested": True, "path": rel, "error": "no live lease"}), file=sys.stderr)
    return 1


def _portable_template_root() -> Path:
    """A release works as an exported root; the private development tree keeps it nested."""
    nested = REPO_ROOT / "templates" / "agentvault-portable"
    if nested.is_dir():
        return nested
    if (REPO_ROOT / "RELEASE.json").is_file():
        return REPO_ROOT
    raise ValueError("no portable RELEASE.json found; run init from an unpacked AgentVault release")


_INSTALL_RECEIPT = "AGENTVAULT_INSTALL.json"
_INSTALL_HISTORY = "AGENTVAULT_INSTALL_HISTORY"
_INSTALL_PENDING = "AGENTVAULT_INSTALL_PENDING.json"
_INSTALL_CACHE_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}
_INSTALL_USER_PREFIXES = ("MemoryBank/", "EpisodicTracker/", "OpenViking/", "GraphRAG/",
                          "VectorRAG/", "docs/", "reports/", "aeap/policies/")


def _install_rel(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or Path(value).is_absolute() or Path(value).as_posix() != value
            or any(part in {".", ".."} for part in value.split("/"))
            or re.match(r"^[A-Za-z]:", value)):
        raise ValueError(f"unsafe release path: {value!r}")
    return value


def _install_cache(rel: str) -> bool:
    p = Path(rel)
    return bool(set(p.parts) & _INSTALL_CACHE_NAMES or p.name in {".DS_Store"}
                or p.suffix in {".pyc", ".pyo"})


def _install_destination(target: Path, rel: str) -> Path:
    """Reject destination symlinks, including an intermediate user-controlled directory."""
    out = target / _install_rel(rel)
    for part in [out, *out.parents]:
        if part == target.parent:
            break
        if part.is_symlink():
            raise ValueError(f"symlink destination is not installable: {part}")
        if part != out and part.exists() and not part.is_dir():
            raise ValueError(f"destination parent is not a directory: {part}")
    if out.exists() and not out.is_file():
        raise ValueError(f"destination is not a regular file: {out}")
    return out


def _install_release(src: Path) -> tuple[dict, str, dict]:
    manifest_path = src / "RELEASE.json"
    if manifest_path.is_symlink():
        raise ValueError("release manifest must be a regular file")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") not in {"1.0", "2.0"}:
        raise ValueError("unsupported release manifest schema")
    if manifest.get("schema_version") == "2.0" and (
            manifest.get("state_schema") != "agentvault-v1"
            or not isinstance(manifest.get("version"), str) or not manifest["version"].strip()):
        raise ValueError("schema 2 releases require explicit version and supported state_schema pins")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or manifest.get("file_count", len(files)) != len(files):
        raise ValueError("release requires a complete files/hash manifest")
    result, aliases = {}, set()
    for rel, expected in sorted(files.items()):
        rel = _install_rel(rel)
        alias = unicodedata.normalize("NFC", rel).casefold()
        if alias in aliases:
            raise ValueError(f"release contains colliding path aliases: {rel}")
        aliases.add(alias)
        if _install_cache(rel):
            raise ValueError(f"release manifest must not contain runtime debris: {rel}")
        if not isinstance(expected, str) or not re.fullmatch("[a-f0-9]{64}", expected):
            raise ValueError(f"invalid release hash: {rel}")
        path = src / rel
        for parent in path.parents:
            if parent == src:
                break
            if parent.is_symlink():
                raise ValueError(f"release path traverses a symlink: {rel}")
        if path.is_symlink() and not (rel == "CLAUDE.md" and os.readlink(path) == "AGENTS.md"):
            raise ValueError(f"unexpected release symlink: {rel}")
        if not path.is_file():
            raise ValueError(f"release file is missing: {rel}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"release hash mismatch: {rel}")
        mode = manifest.get("file_modes", {}).get(rel)
        if mode is None:
            mode = "0755" if path.stat().st_mode & 0o111 else "0644"
        if mode not in {"0644", "0755"}:
            raise ValueError(f"unsupported release mode for {rel}: {mode!r}")
        result[rel] = {"data": data, "sha256": expected, "mode": mode}
    required = {"scripts/coord/avcoord.py", "bin/avcoord", "MemoryBank/CURRENT.md",
                "MemoryBank/coord/PROTOCOL.md", "MemoryBank/agents/registry.json"}
    if not required <= files.keys():
        raise ValueError(f"release lacks mandatory core files: {sorted(required - files.keys())}")
    if manifest.get("payload_sha256"):
        # Schema 2 hashes the sorted path/hash/mode records, independently of release time.
        payload = [{"path": p, "sha256": x["sha256"], "mode": x["mode"]} for p, x in result.items()]
        actual = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if actual != manifest["payload_sha256"]:
            raise ValueError("release payload digest mismatch")
    return manifest, hashlib.sha256(raw).hexdigest(), result


def _install_profile(rel: str, profiles: set[str]) -> bool:
    if rel.startswith("aeap/") or rel == "requirements-aeap.txt":
        return "aeap" in profiles
    if rel.startswith(("OpenViking/", "GraphRAG/", "VectorRAG/")):
        return "full" in profiles
    root_files = {"AGENTS.md", "AGENTS.codex.md", "ADOPT.md", "GEMINI.md", "CLAUDE.md", "README.md",
                  ".cursorrules", ".windsurfrules", ".gitignore", "requirements-core.txt", "pyproject.toml"}
    return rel in root_files or rel.startswith((".agentvault/", ".config/", ".cursor/", ".githooks/",
                                               ".github/", "bin/", "scripts/",
                                               "MemoryBank/", "EpisodicTracker/"))


def _install_seeds(stamp: str) -> dict[str, bytes]:
    current = f'''---
version: 1.0
priority: P0
last_updated: {stamp}
last_verified_at: {stamp}
session_id: sess-init
stale_after_hours: 48
type: current_pointer
---
# CURRENT

## Session
- **session_id:** `sess-init`
- **Detail:** `MemoryBank/sessions/sess-init.md`

## Active ETS
- Initialize this project's brief and technical context.

## Status
`ACTIVE` — fresh AgentVault instance; AEAP admission disabled.

## Next
Edit `MemoryBank/projectbrief.md` and `MemoryBank/techContext.md`; run `bin/avcoord doctor`.
'''
    session = f'''---
session_id: sess-init
created: {stamp}
agent: orchestrator
status: active
---
# Session: initialization

Installed the selected AgentVault release. Fill projectbrief and techContext before work.
'''
    seed = {"MemoryBank/CURRENT.md": current.encode(), "MemoryBank/sessions/sess-init.md": session.encode(),
            "MemoryBank/board.md": f"---\ngenerated_at: {stamp}\ntype: blackboard\n---\n# Coordination Board\n\nNo open threads. Run `bin/avcoord refresh --views-only`.\n".encode(),
            "MemoryBank/activeContext.md": f"---\ngenerated_at: {stamp}\ntype: derived_context\n---\n# Active Context\n\nFresh instance; no active task transitions.\n".encode(),
            "MemoryBank/coord/audit.jsonl": b"", "EpisodicTracker/events.jsonl": b""}
    values = {"MemoryBank/coord/next_ids.json": {"progress": 1, "episode": 1, "message": 1},
              "MemoryBank/coord/threads.json": {"schema_version": "1.0", "last_updated": stamp, "threads": []},
              "MemoryBank/coord/protocol.json": {"schema_version": "1.0", "epoch": 1, "authority": "legacy",
                "notes": "Fresh instance; journal cutover requires explicit maintenance and independent review."}}
    seed.update({p: (json.dumps(v, indent=2) + "\n").encode() for p, v in values.items()})
    return seed


def _install_software_digest(managed: dict) -> str:
    payload = [{"path": p, **meta} for p, meta in sorted(managed.items())]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _install_validate_receipt(receipt: dict) -> None:
    if (receipt.get("schema_version") != "1.0" or not isinstance(receipt.get("managed_files"), dict)
            or receipt.get("state_schema") != "agentvault-v1"):
        raise ValueError("unrecognized installed-software receipt/state schema; explicit migration required")
    for rel, meta in receipt["managed_files"].items():
        _install_rel(rel)
        if rel.startswith(_INSTALL_USER_PREFIXES) or not _install_profile(rel, {"core", "full", "aeap"}):
            raise ValueError(f"receipt incorrectly claims user-owned state as managed: {rel}")
        if (not isinstance(meta, dict) or not re.fullmatch("[a-f0-9]{64}", meta.get("sha256", ""))
                or meta.get("mode") not in {"0644", "0755"}):
            raise ValueError(f"invalid managed-file metadata: {rel}")
    if _install_software_digest(receipt["managed_files"]) != receipt.get("installed_artifact_sha256"):
        raise ValueError("installed-software receipt digest mismatch")
    if set(receipt["managed_files"]) & set(receipt.get("user_owned_files", [])):
        raise ValueError("a file cannot be both managed and user-owned")


def _install_drift(target: Path, receipt: dict, *, overrides=()) -> list[dict]:
    conflicts = []
    for rel, meta in receipt["managed_files"].items():
        if rel in overrides:
            continue
        p = _install_destination(target, rel)
        observed = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        mode = ("0755" if p.stat().st_mode & 0o111 else "0644") if p.exists() else None
        if observed != meta["sha256"] or mode != meta["mode"]:
            conflicts.append({"path": rel, "reason": "managed file changed locally",
                              "installed_sha256": meta["sha256"], "observed_sha256": observed,
                              "installed_mode": meta["mode"], "observed_mode": mode})
    return conflicts


def _install_write_bytes(dest: Path, data: bytes, mode: str = "0644") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=".agentvault-install-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staging, int(mode, 8))
        os.replace(staging, dest)
        fsync_dir(dest.parent)
    finally:
        Path(staging).unlink(missing_ok=True)


def _install_save_history(target: Path, raw_receipt: bytes, plan: dict, *, overrides=()) -> str:
    """Archive a complete verified software image; never back up project memory as code."""
    rec = json.loads(raw_receipt)
    history_id = hashlib.sha256(raw_receipt).hexdigest()
    base = f"{_INSTALL_HISTORY}/{history_id}"
    artifacts = {f"{base}/receipt.json": raw_receipt,
                 f"{base}/plan.json": (json.dumps(plan, indent=2) + "\n").encode()}
    for rel, meta in rec["managed_files"].items():
        if rel in overrides:
            continue
        p = _install_destination(target, rel)
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise ValueError(f"target changed before backup: {rel}")
        artifacts[f"{base}/before/{rel}"] = data
    for rel, data in artifacts.items():
        p = _install_destination(target, rel)
        if p.exists():
            # Plan descriptions may differ on a repeated failed attempt; the receipt and
            # before-images are immutable. The first retained plan remains authoritative.
            if not rel.endswith("/plan.json") and p.read_bytes() != data:
                raise ValueError(f"software recovery history changed: {rel}")
        else:
            _install_write_bytes(p, data)
    return history_id


def _install_json_bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def _install_start_transaction(target: Path, old_raw: bytes | None, receipt: dict,
                               puts: dict, retire: list[str], operation: str) -> None:
    """Durably stage all byte images before publishing an interrupted-work marker.

    The marker is a software recovery intent, not a workspace/session authority. No
    installed file changes until both the intent and its hash-addressed images are durable.
    """
    pending = _install_destination(target, _INSTALL_PENDING)
    if pending.exists():
        raise ValueError("unfinished software transaction; use --recover resume|rollback")
    new_raw = _install_json_bytes(receipt)
    blobs = {hashlib.sha256(new_raw).hexdigest(): new_raw}
    if old_raw is not None:
        blobs[hashlib.sha256(old_raw).hexdigest()] = old_raw
    transitions = {}
    for rel in sorted(set(puts) | set(retire)):
        path = _install_destination(target, rel)
        before = path.read_bytes() if path.exists() else None
        before_meta = None
        if before is not None:
            before_sha = hashlib.sha256(before).hexdigest()
            blobs[before_sha] = before
            before_meta = {"sha256": before_sha, "mode": "0755" if path.stat().st_mode & 0o111 else "0644"}
        after_meta = None
        if rel in puts:
            data, mode = puts[rel]
            after_sha = hashlib.sha256(data).hexdigest()
            blobs[after_sha] = data
            after_meta = {"sha256": after_sha, "mode": mode}
        user_owned = rel.startswith(_INSTALL_USER_PREFIXES) or rel in receipt.get("user_owned_files", [])
        if user_owned and before is not None:
            raise ValueError(f"software transaction cannot overwrite existing user state: {rel}")
        transitions[rel] = {"before": before_meta, "after": after_meta, "user_owned": user_owned}
    intent = {"schema_version": "1.0", "operation": operation,
              "before_receipt": hashlib.sha256(old_raw).hexdigest() if old_raw is not None else None,
              "after_receipt": hashlib.sha256(new_raw).hexdigest(), "files": transitions}
    txid = hashlib.sha256(_install_json_bytes(intent)).hexdigest()
    base = f"{_INSTALL_HISTORY}/transactions/{txid}"
    for content_sha, content in blobs.items():
        dest = _install_destination(target, f"{base}/blobs/{content_sha}")
        if dest.exists() and dest.read_bytes() != content:
            raise ValueError("software transaction blob collision")
        if not dest.exists():
            _install_write_bytes(dest, content)
    envelope = {**intent, "transaction_sha256": txid}
    _install_write_bytes(_install_destination(target, f"{base}/intent.json"), _install_json_bytes(envelope))
    _install_write_bytes(pending, _install_json_bytes(envelope))


def _install_recover(target: Path, direction: str, *, preview: bool = False) -> int:
    pending = _install_destination(target, _INSTALL_PENDING)
    if not pending.exists():
        print(json.dumps({"status": "no_pending_transaction", "target": str(target)}))
        return 0
    raw_intent = pending.read_bytes()
    envelope = json.loads(raw_intent)
    txid = envelope.get("transaction_sha256")
    intent = {k: v for k, v in envelope.items() if k != "transaction_sha256"}
    if (not isinstance(txid, str) or hashlib.sha256(_install_json_bytes(intent)).hexdigest() != txid
            or intent.get("schema_version") != "1.0" or not isinstance(intent.get("files"), dict)
            or intent.get("operation") not in {"install", "upgrade", "rollback"}):
        raise ValueError("pending software transaction hash/schema mismatch")
    base = f"{_INSTALL_HISTORY}/transactions/{txid}"
    if _install_destination(target, f"{base}/intent.json").read_bytes() != raw_intent:
        raise ValueError("pending software intent differs from its retained evidence")

    def blob(content_sha):
        if not isinstance(content_sha, str) or not re.fullmatch("[a-f0-9]{64}", content_sha):
            raise ValueError("invalid software transaction blob reference")
        data = _install_destination(target, f"{base}/blobs/{content_sha}").read_bytes()
        if hashlib.sha256(data).hexdigest() != content_sha:
            raise ValueError("software transaction blob hash mismatch")
        return data

    after_raw = blob(intent["after_receipt"])
    after = json.loads(after_raw)
    _install_validate_receipt(after)
    before_raw = blob(intent["before_receipt"]) if intent.get("before_receipt") else None
    before = json.loads(before_raw) if before_raw is not None else None
    if before:
        _install_validate_receipt(before)
        if before["state_schema"] != after["state_schema"]:
            raise ValueError("software recovery state schema is incompatible")
    preserved = set(after.get("user_owned_files", [])) | set(before.get("user_owned_files", []) if before else [])
    rollback_raw = before_raw
    if before:
        restored = {p: m for p, m in before["managed_files"].items() if p not in preserved}
        if restored != before["managed_files"]:
            adjusted = {**before, "managed_files": restored, "user_owned_files": sorted(preserved),
                        "declared_overrides": sorted(set(before.get("declared_overrides", [])) | set(after.get("declared_overrides", []))),
                        "installed_artifact_sha256": _install_software_digest(restored)}
            rollback_raw = _install_json_bytes(adjusted)
    current_path = _install_destination(target, _INSTALL_RECEIPT)
    current_raw = current_path.read_bytes() if current_path.exists() else None
    if current_raw not in (before_raw, after_raw, rollback_raw):
        raise ValueError("installed receipt changed outside the pending software transaction")
    result_path = _install_destination(target, f"{base}/result.json")
    if result_path.exists():
        done = json.loads(result_path.read_bytes())
        if done.get("direction") not in {"resume", "rollback"}:
            raise ValueError("invalid software transaction completion record")
        # A crash after the completion record but before marker cleanup must not turn a
        # retry into an opposite-direction operation. A completed rollback uses --rollback.
        direction = done["direction"]
    desired_receipt = after_raw if direction == "resume" else rollback_raw
    desired = "after" if direction == "resume" else "before"
    conflicts, changes = [], []
    checked = set()
    for rel, transition in sorted(intent["files"].items()):
        _install_rel(rel)
        path = _install_destination(target, rel)
        user_owned = rel.startswith(_INSTALL_USER_PREFIXES) or rel in preserved
        if transition.get("user_owned") != user_owned:
            raise ValueError(f"software transaction ownership mismatch: {rel}")
        if user_owned and transition["before"] is not None:
            raise ValueError(f"software recovery cannot overwrite user state: {rel}")
        if user_owned and rel not in after.get("user_owned_files", []):
            raise ValueError(f"software recovery seed lacks an ownership declaration: {rel}")
        for meta in (transition["before"], transition["after"]):
            if meta is not None:
                if meta.get("mode") not in {"0644", "0755"}:
                    raise ValueError("invalid recovery file mode")
                blob(meta["sha256"])
        if user_owned:
            # Seeds become user-owned the moment created. Even an edited seed survives
            # rollback, and resume never replaces it with an older staged copy.
            if direction == "resume" and not path.exists() and transition["after"]:
                changes.append((rel, transition["after"]))
            continue
        if not _install_profile(rel, {"core", "full", "aeap"}):
            raise ValueError(f"software recovery path is outside managed profiles: {rel}")
        if (transition["before"] != (before["managed_files"].get(rel) if before else None)
                or transition["after"] != after["managed_files"].get(rel)):
            raise ValueError(f"software transition differs from its pinned receipts: {rel}")
        checked.add(rel)
        observed = ({"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                     "mode": "0755" if path.stat().st_mode & 0o111 else "0644"} if path.exists() else None)
        if observed not in (transition["before"], transition["after"]):
            conflicts.append({"path": rel, "reason": "file is neither the recorded before nor after image"})
        elif observed != transition[desired]:
            changes.append((rel, transition[desired]))
    # Verify unchanged managed files as well; otherwise unrelated local drift could be
    # certified by the recovered software receipt.
    all_managed = {**(before["managed_files"] if before else {}), **after["managed_files"]}
    for rel, meta in all_managed.items():
        if rel in checked or rel in preserved:
            continue
        path = _install_destination(target, rel)
        if (not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]
                or ("0755" if path.stat().st_mode & 0o111 else "0644") != meta["mode"]):
            conflicts.append({"path": rel, "reason": "unchanged managed file drifted during transaction"})
    plan = {"status": "conflict" if conflicts else "ready", "operation": "recover",
            "direction": direction, "transaction_sha256": txid,
            "changes": [{"path": p, "action": "restore" if meta else "archive"} for p, meta in changes],
            "conflicts": conflicts, "preserved_user_files": sorted(preserved)}
    if preview or conflicts:
        print(json.dumps(plan, indent=2))
        return 1 if conflicts else 0
    for rel, meta in changes:
        path = _install_destination(target, rel)
        if meta is not None:
            _install_write_bytes(path, blob(meta["sha256"]), meta["mode"])
        elif path.exists():
            archive_base = (f"{_INSTALL_HISTORY}/{intent['before_receipt']}/retired"
                            if intent["operation"] == "rollback" and direction == "resume"
                            else f"{base}/removed/{direction}")
            archived = _install_destination(target, f"{archive_base}/{rel}")
            archived.parent.mkdir(parents=True, exist_ok=True)
            if archived.exists() and archived.read_bytes() != path.read_bytes():
                raise ValueError("software recovery archive conflict")
            os.replace(path, archived)
            fsync_dir(path.parent)
            fsync_dir(archived.parent)
            parent = path.parent
            while parent != target:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
    if desired_receipt is not None:
        if not current_path.exists() or current_path.read_bytes() != desired_receipt:
            _install_write_bytes(current_path, desired_receipt)
    elif current_path.exists():
        archived = _install_destination(target, f"{base}/removed/installation-receipt.json")
        archived.parent.mkdir(parents=True, exist_ok=True)
        os.replace(current_path, archived)
        fsync_dir(target)
    _install_write_bytes(result_path, _install_json_bytes({"transaction_sha256": txid, "direction": direction, "status": "complete"}))
    pending.unlink()
    fsync_dir(target)
    print(json.dumps({**plan, "status": "ok", "wrote": len(changes)}))
    return 0


def _install_rollback(args: argparse.Namespace, target: Path, current: dict, raw_current: bytes) -> int:
    _install_validate_receipt(current)
    wanted = args.rollback
    history_id = current.get("rollback_receipt") if wanted == "latest" else wanted
    if not isinstance(history_id, str) or not re.fullmatch("[a-f0-9]{64}", history_id):
        raise ValueError("rollback requires 'latest' or a retained 64-character receipt digest")
    prior_path = _install_destination(target, f"{_INSTALL_HISTORY}/{history_id}/receipt.json")
    raw_prior = prior_path.read_bytes()
    if hashlib.sha256(raw_prior).hexdigest() != history_id:
        raise ValueError("rollback receipt hash mismatch")
    prior = json.loads(raw_prior)
    _install_validate_receipt(prior)
    if prior["state_schema"] != current["state_schema"]:
        raise ValueError("rollback state schema is incompatible; explicit state migration required")
    preserved = set(current.get("user_owned_files", [])) | set(prior.get("user_owned_files", []))
    restored = {p: v for p, v in prior["managed_files"].items() if p not in preserved}
    conflicts = _install_drift(target, current)
    replacements = {}
    changes = []
    for rel, meta in restored.items():
        backup = _install_destination(target, f"{_INSTALL_HISTORY}/{history_id}/before/{rel}")
        if not backup.exists():
            raise ValueError(f"rollback before-image missing: {rel}")
        data = backup.read_bytes()
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise ValueError(f"rollback before-image hash mismatch: {rel}")
        dest = _install_destination(target, rel)
        if rel not in current["managed_files"] and dest.exists():
            conflicts.append({"path": rel, "reason": "rollback would overwrite an unowned file"})
        old_meta = current["managed_files"].get(rel)
        if old_meta != meta:
            replacements[rel] = (data, meta["mode"])
            changes.append({"path": rel, "action": "restore", "before_sha256": old_meta["sha256"] if old_meta else None,
                            "after_sha256": meta["sha256"]})
    retire = sorted(set(current["managed_files"]) - set(restored) - preserved)
    changes.extend({"path": p, "action": "archive_added_software",
                    "before_sha256": current["managed_files"][p]["sha256"]} for p in retire)
    preview = {"status": "conflict" if conflicts else "ready", "operation": "rollback",
               "target": str(target), "rollback_receipt": history_id,
               "release_version": prior["release_version"], "changes": changes, "conflicts": conflicts,
               "preserved_user_files": sorted(preserved)}
    if conflicts or getattr(args, "dry_run", False):
        print(json.dumps(preview, indent=2))
        return 1 if conflicts else 0
    receipt_path = _install_destination(target, _INSTALL_RECEIPT)
    if receipt_path.read_bytes() != raw_current or _install_drift(target, current):
        raise ValueError("installed code changed after rollback preflight")
    reverse_id = hashlib.sha256(raw_current).hexdigest()
    for rel in retire:
        archived = _install_destination(target, f"{_INSTALL_HISTORY}/{reverse_id}/retired/{rel}")
        if archived.exists():
            raise ValueError(f"retired rollback path already exists: {rel}")
    _install_save_history(target, raw_current, preview)
    receipt = {**prior, "managed_files": restored, "user_owned_files": sorted(preserved),
               "declared_overrides": sorted(set(current.get("declared_overrides", [])) | set(prior.get("declared_overrides", []))),
               "installed_at": iso(), "rollback_receipt": reverse_id,
               "previous_release_sha256": current["source_manifest_sha256"], "operation": "rollback",
               "installed_artifact_sha256": _install_software_digest(restored)}
    _install_start_transaction(target, raw_current, receipt, replacements, retire, "rollback")
    if _install_recover(target, "resume") != 0:
        return 1
    print(json.dumps({**preview, "status": "ok", "wrote": len(changes), "receipt": _INSTALL_RECEIPT}))
    return 0


def _cmd_init_plan(args: argparse.Namespace) -> int:
    """Install a checksummed release; upgrade only unchanged, previously managed code.

    MemoryBank and research are user-owned from their first installation. The receipt pins
    SOFTWARE only, never task/session authority. Preview makes no directories or lock files.
    """
    try:
        if getattr(args, "force", False):
            raise ValueError("--force cannot overwrite user state; use --upgrade --preview, resolve conflicts, then --upgrade")
        raw_target = Path(args.target).expanduser().absolute()
        if raw_target.is_symlink():
            raise ValueError("target root is a symlink")
        target = raw_target.resolve()
        if target.exists() and not target.is_dir():
            raise ValueError("installation target must be a directory")
        if getattr(args, "recover", None):
            if any(getattr(args, flag, False) for flag in ("rollback", "upgrade", "full", "with_aeap", "override")):
                raise ValueError("recovery cannot be combined with install/profile/override options")
            return _install_recover(target, args.recover, preview=bool(getattr(args, "dry_run", False)))
        if _install_destination(target, _INSTALL_PENDING).exists():
            raise ValueError("unfinished software transaction; use --recover resume|rollback")
        receipt_path = _install_destination(target, _INSTALL_RECEIPT)
        raw_old = receipt_path.read_bytes() if receipt_path.exists() else None
        old = json.loads(raw_old) if raw_old is not None else None
        if old:
            _install_validate_receipt(old)
        if getattr(args, "rollback", None):
            if old is None:
                raise ValueError("rollback requires an installed-software receipt")
            if any(getattr(args, flag, False) for flag in ("upgrade", "full", "with_aeap", "override")):
                raise ValueError("rollback cannot be combined with install/profile/override options")
            return _install_rollback(args, target, old, raw_old)
        src = _portable_template_root().resolve()
        manifest, release_sha, source_files = _install_release(src)
        if target == src or src in target.parents or target in src.parents:
            raise ValueError("installation target must be separate from the release source")
        state_schema = manifest.get("state_schema", "agentvault-v1")
        if state_schema != "agentvault-v1" or (old and old["state_schema"] != state_schema):
            raise ValueError("release state schema is incompatible; explicit migration required")
        upgrade = bool(getattr(args, "upgrade", False))
        if upgrade and old is None:
            raise ValueError("--upgrade requires an existing AGENTVAULT_INSTALL.json ownership receipt")
        profiles = set(old.get("profiles", [])) if old else set()
        profiles.add("core")
        if getattr(args, "full", False):
            profiles.add("full")
        if getattr(args, "with_aeap", False):
            profiles.add("aeap")
        if "aeap" in profiles and not any(p.startswith("aeap/engine/") for p in source_files):
            raise ValueError("this release does not contain the requested AEAP extension")
        selected = {p: dict(v) for p, v in source_files.items() if _install_profile(p, profiles)}
        stamp = now().isoformat(timespec="seconds")
        for rel, data in _install_seeds(stamp).items():
            selected[rel] = {"data": data, "sha256": hashlib.sha256(data).hexdigest(), "mode": "0644"}
        managed = dict(old["managed_files"]) if old else {}
        preserved = set(old.get("user_owned_files", [])) if old else set()
        overrides = set(old.get("declared_overrides", [])) if old else set()
        for rel in getattr(args, "override", []) or []:
            _install_rel(rel)
            if rel not in selected and rel not in managed:
                raise ValueError(f"override path is not part of this software profile: {rel}")
            overrides.add(rel)
            preserved.add(rel)
            managed.pop(rel, None)
        for p in managed:
            _install_rel(p)
            if p.startswith(_INSTALL_USER_PREFIXES):
                raise ValueError(f"receipt incorrectly claims user-owned state as managed: {p}")
        changes, conflicts, retained = [], _install_drift(target, old, overrides=overrides) if old else [], []
        for rel, item in sorted(selected.items()):
            dest = _install_destination(target, rel)
            existing = dest.read_bytes() if dest.exists() else None
            before = hashlib.sha256(existing).hexdigest() if existing is not None else None
            user_owned = rel.startswith(_INSTALL_USER_PREFIXES) or rel in preserved
            if existing is not None and (user_owned or rel not in managed):
                preserved.add(rel)
                if not rel.startswith(_INSTALL_USER_PREFIXES):
                    overrides.add(rel)
                retained.append(rel)
                continue
            previous = managed.get(rel)
            if previous and before is not None and before != previous["sha256"]:
                conflicts.append({"path": rel, "reason": "managed file changed locally", "installed_sha256": previous["sha256"], "observed_sha256": before, "incoming_sha256": item["sha256"]})
                continue
            if previous and (before != item["sha256"] or previous["mode"] != item["mode"]) and not upgrade:
                conflicts.append({"path": rel, "reason": "release changed; explicit --upgrade required", "observed_sha256": before, "incoming_sha256": item["sha256"]})
                continue
            if before != item["sha256"] or (previous and previous["mode"] != item["mode"]):
                changes.append({"path": rel, "action": "create" if existing is None else "replace",
                                "before_sha256": before, "after_sha256": item["sha256"], "mode": item["mode"]})
            if user_owned:
                preserved.add(rel)
            else:
                managed[rel] = {"sha256": item["sha256"], "mode": item["mode"]}
        preview = {"status": "conflict" if conflicts else "ready", "target": str(target),
                   "release_version": manifest.get("version", "unversioned"), "profiles": sorted(profiles),
                   "source_manifest_sha256": release_sha, "changes": changes, "conflicts": conflicts,
                   "preserved_user_files": sorted(preserved),
                   "retained_obsolete_managed_files": sorted(set(managed) - set(selected))}
        if conflicts or getattr(args, "dry_run", False) or getattr(args, "preview", False):
            print(json.dumps(preview, indent=2))
            return 1 if conflicts else 0
        installed_sha = _install_software_digest(managed)
        if (old and not changes and old.get("source_manifest_sha256") == release_sha
                and old.get("profiles") == sorted(profiles) and old.get("user_owned_files") == sorted(preserved)):
            print(json.dumps({**preview, "status": "unchanged", "wrote": 0}))
            return 0
        # Complete preflight before the first write. Recheck every planned existing file;
        # a caller must serialize install/upgrade with other writers in this target.
        for change in changes:
            dest = _install_destination(target, change["path"])
            actual = hashlib.sha256(dest.read_bytes()).hexdigest() if dest.exists() else None
            if actual != change["before_sha256"]:
                raise ValueError(f"target changed after preview: {change['path']}")
        receipt = {"schema_version": "1.0", "release_version": manifest.get("version", "unversioned"),
                   "profiles": sorted(profiles), "source_manifest_sha256": release_sha,
                   "installed_artifact_sha256": installed_sha, "state_schema": state_schema,
                   "installed_at": stamp, "managed_files": managed, "user_owned_files": sorted(preserved),
                   "declared_overrides": sorted(overrides),
                   "rollback_receipt": hashlib.sha256(raw_old).hexdigest() if raw_old else None,
                   "previous_release_sha256": old.get("source_manifest_sha256") if old else None}
        target.mkdir(parents=True, exist_ok=True)
        if receipt_path.exists() and receipt_path.read_bytes() != raw_old:
            raise ValueError("installed-software receipt changed after preflight")
        if old:
            _install_save_history(target, raw_old, preview, overrides=overrides)
        puts = {c["path"]: (selected[c["path"]]["data"], selected[c["path"]]["mode"]) for c in changes}
        _install_start_transaction(target, raw_old, receipt, puts, [], "upgrade" if old else "install")
        if _install_recover(target, "resume") != 0:
            return 1
        print(json.dumps({**preview, "status": "ok", "wrote": len(changes), "receipt": _INSTALL_RECEIPT}))
        print("Next: cd", target, "&& bin/avcoord doctor && bin/avcoord status")
        return 0
    except (ValueError, OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}), file=sys.stderr)
        return 1

def cmd_init(args: argparse.Namespace) -> int:
    """Preview first, then serialize and recompute the plan under one installer lock."""
    if getattr(args, "dry_run", False):
        return _cmd_init_plan(args)
    preview_args = argparse.Namespace(**vars(args))
    preview_args.dry_run = True
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = _cmd_init_plan(preview_args)
    if rc:
        print(stdout.getvalue(), end="")
        print(stderr.getvalue(), end="", file=sys.stderr)
        return rc
    if '"status": "no_pending_transaction"' in stdout.getvalue():
        print(stdout.getvalue(), end="")
        return 0
    try:
        raw_target = Path(args.target).expanduser().absolute()
        if raw_target.is_symlink():
            raise ValueError("target root is a symlink")
        target = raw_target.resolve()
        target.mkdir(parents=True, exist_ok=True)
        lock = _install_destination(target, ".agentvault-install.lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                # Another installer may have completed after the read-only preview.
                # Re-reading the receipt and every managed byte prevents mixed versions.
                return _cmd_init_plan(args)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "error", "error": str(error)}), file=sys.stderr)
        return 1


def cmd_rotate_events(args: argparse.Namespace) -> int:
    """Move older events.jsonl lines to EpisodicTracker/archive/; keep last --keep (HOT)."""
    keep = max(1, int(args.keep))
    events_path = EVENTS
    if not events_path.exists():
        print("FAIL: events.jsonl missing", file=sys.stderr)
        return 1
    lines = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) <= keep:
        print(json.dumps({"status": "noop", "lines": len(lines), "keep": keep}))
        return 0
    warm = lines[:-keep]
    hot = lines[-keep:]
    archive_dir = events_path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%Y%m%dT%H%M%S")
    out = archive_dir / f"events-{stamp}.jsonl"
    if out.exists():
        print(f"FAIL: archive already exists, refusing to overwrite: {out}", file=sys.stderr)
        return 1
    # Archive durably BEFORE truncating the source. Two bare write_text calls meant a crash
    # between them lost the entire event ledger.
    atomic_write_text(out, "\n".join(warm) + "\n")
    atomic_write_text(events_path, "\n".join(hot) + "\n")
    audit("rotate_events", archived=str(out), moved=len(warm), kept=len(hot))
    print(json.dumps({"status": "ok", "archived": str(out), "moved": len(warm), "kept": len(hot)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_paths()
    parser = build_parser()
    args = parser.parse_args(argv)
    # init does not require MemoryBank yet
    if args.cmd != "init":
        LEASES.mkdir(parents=True, exist_ok=True)
        COORD.mkdir(parents=True, exist_ok=True)
    dispatch = {
        "claim": cmd_claim,
        "release": cmd_release,
        "renew": cmd_renew,
        "post": cmd_post,
        "recv": cmd_recv,
        "ack": cmd_ack,
        "next-id": cmd_next_id,
        "refresh": cmd_refresh,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "test": cmd_test,
        "check-lease": cmd_check_lease,
        "reap": cmd_reap,
        "cutover": cmd_cutover,
        "run": lambda a: cmd_run_start(a) if a.run_cmd == "start" else cmd_run_list(a),
        "verify": cmd_verify,
        "checkpoint": cmd_checkpoint,
        "gate": cmd_gate,
        "init": cmd_init,
        "rotate-events": cmd_rotate_events,
        "hydrate": cmd_hydrate,
        "intent": cmd_intent,
        "task": cmd_task,
        "workspace": cmd_workspace,
        "query": cmd_query,
        "done": cmd_done,
        "compact": cmd_compact,
        "fingerprint": cmd_fingerprint,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
