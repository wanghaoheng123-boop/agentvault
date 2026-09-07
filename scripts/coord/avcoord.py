#!/usr/bin/env python3
"""AgentVault coordination CLI — leases, maildir, IDs, board refresh, doctor, tests.

Usage:
  python3 scripts/coord/avcoord.py <command> [options]

Environment:
  AVCOORD_ROOT  Optional workspace root (default: repo root containing this script).
                Trail/smoke tests set this to an isolated temp sandbox.

Commands:
  claim | release | renew | post | recv | ack | next-id | refresh | doctor | test
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
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


class FileLock:
    """Exclusive advisory lock, released by the kernel when the holder dies.

    This replaces the previous mtime-based scheme, which stole a lock purely because it
    was 30s old — handing it to a second writer while the first was still inside its
    critical section — and whose release unlinked whatever lock file happened to be
    there, including one another process had just taken.
    """

    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise RuntimeError(f"could not acquire {self.path.name} within {self.timeout}s")
                time.sleep(0.01)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        self._fd = fd
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


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
    """DIRECTIONAL: does `outer` contain `inner`? Boundary-safe at segment edges."""
    if not outer.key:
        return True  # the workspace root covers everything
    return inner.key == outer.key or inner.key.startswith(outer.key + "/")


def resources_overlap(a: Res, b: Res) -> bool:
    """SYMMETRIC: do these two resources share any concrete path? Claim exclusion only."""
    return res_covers(a, b) or res_covers(b, a)


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
    if not run_id:
        return None
    p = runs_dir() / f"{run_id}.json"
    data = load_json(p, None)
    if not data:
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
    rid = getattr(args, "run", None)
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
    run = {
        "schema_version": "1.0",
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
    print(json.dumps(run, indent=2))
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
    """Canonical resources of a stored lease, skipping any that will not canonicalize."""
    out = []
    for r in lease.get("resources", []):
        c = canon_or_none(r)
        if c is not None:
            out.append(c)
    return out


def _same_principal(lease: dict, agent: str, run_id: str | None) -> bool:
    """Is this lease held by the caller?

    Two registered runs of the same role are DIFFERENT principals, so they exclude each
    other. Legacy callers with no run identity fall back to the agent id, which preserves
    today's behavior (re-claiming your own resource stays idempotent).
    """
    other_run = lease.get("run_id")
    if run_id and other_run:
        return other_run == run_id
    return lease.get("agent_id") == agent


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
    check_write_scopes(args.agent, [c for _, c in wanted], strict=getattr(args, "strict_scopes", False))
    ttl_min = parse_ttl(args.ttl)
    expires = now() + timedelta(minutes=ttl_min)
    LEASES.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(LEASES / ".claim.lock"):
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
                lease = {
                    "schema_version": "1.1",
                    "agent_id": args.agent,
                    "run_id": run_id or "",
                    "fence": fence,
                    "resources": [raw],
                    "resource_keys": [c.key],
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
    with FileLock(LEASES / ".claim.lock"):
        for p in sorted(LEASES.glob("*.json")):
            if p.name.startswith("."):
                continue
            lease = load_json(p)
            if not lease:
                continue
            if not _same_principal(lease, args.agent, run_id):
                continue
            if token and lease.get("lease_token") and lease["lease_token"] != token:
                continue
            # Fencing: a run whose generation is older than the lease's cannot act on it.
            # This is what rejects a paused worker after its resource was reclaimed.
            if run_id and int(lease.get("fence", 0)) > fence:
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
    if fenced_out:
        print(f"denied {fenced_out} lease(s): stale fence", file=sys.stderr)
    print(f"released {removed} lease(s)")
    return 0


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
    with FileLock(LEASES / ".claim.lock"):
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
            if token and lease.get("lease_token") and lease["lease_token"] != token:
                denied += 1
                continue
            if run_id and int(lease.get("fence", 0)) > fence:
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
    for sub in ("tmp", "new", "cur", "done"):
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
    src = base / "cur" / f"{args.msg}.json"
    if not src.exists():
        # Prefix fallback only when it resolves to exactly one candidate. The old code took
        # an unsorted glob's [0], so `--msg MSG-1` could ack MSG-100001 nondeterministically.
        alt = sorted((base / "cur").glob(f"{args.msg}*"))
        if not alt:
            print(f"FAIL: message not in cur: {args.msg}", file=sys.stderr)
            return 1
        if len(alt) > 1:
            print(
                f"FAIL: '{args.msg}' is ambiguous: {', '.join(p.stem for p in alt)}",
                file=sys.stderr,
            )
            return 1
        src = alt[0]
    dest = base / "done" / src.name
    data = load_json(src)
    if data:
        data["status"] = "acked"
        data["acked_at"] = iso()
        atomic_write_json(src, data)
    os.replace(src, dest)
    fsync_dir(dest.parent)
    fsync_dir(src.parent)
    audit("ack", agent=args.agent, msg=src.stem)
    print(f"acked {src.name}")
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


def journal_meta() -> dict:
    """Projection metadata written by the commit worker. Empty before cutover."""
    return load_json(COORD / "projections" / "_meta.json", {}) or {}


def load_threads() -> list[dict]:
    """Thread list from whichever store currently holds authority.

    Before cutover this is coord/threads.json exactly as before. After cutover the same
    call reads the generated projection, so every consumer follows the flip without
    knowing that it happened.
    """
    if authority() == "journal":
        proj = load_json(COORD / "projections" / "threads.json", {"threads": []}) or {}
        return list(proj.get("threads") or [])
    data = load_json(COORD / "threads.json", {"threads": []})
    if isinstance(data, dict):
        return list(data.get("threads") or [])
    return []


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
    # Prefer active threads; show all if none active
    active = [t for t in threads if t.get("status") == "active"]
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

    # CURRENT.md is contested and is NOT a view. Refresh may only bump its timestamp when
    # the caller actually holds a lease on it; otherwise refresh is views-only. Bumping it
    # unleased was how a stale pointer got rejuvenated without anyone verifying anything.
    views_only = getattr(args, "views_only", False)
    agent = getattr(args, "agent", None) or "orchestrator"
    run_id = getattr(args, "run", None)
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
    print("refreshed board + CURRENT + activeContext")
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
    audit("status", doctor=payload["doctor"], leases=len(leases))
    return 1 if doctor_issues else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    issues: list[str] = []
    warns: list[str] = []

    for p in (COORD / "PROTOCOL.md", CURRENT, BOARD, NEXT_IDS, REGISTRY):
        if not p.exists():
            issues.append(f"MISSING {p.relative_to(ROOT)}")

    hook = ROOT / "workspace" / "SESSION_STATE.json"
    if hook.exists():
        issues.append(
            "DUAL_SSOT: workspace/SESSION_STATE.json exists — AgentVault MemoryBank is the only SSOT; "
            "do not treat AGENT HOOK state as co-equal"
        )

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

    # Mail backlog warning (hygiene, non-fatal)
    if MAIL.exists():
        cur_total = 0
        for agent_dir in MAIL.iterdir():
            cur = agent_dir / "cur"
            if cur.is_dir():
                cur_total += len(list(cur.glob("*.json")))
        if cur_total > MAIL_CUR_WARN:
            warns.append(f"mail cur backlog={cur_total} (threshold {MAIL_CUR_WARN})")

    status = "FAIL" if issues else "PASS"
    report = {"status": status, "issues": issues, "warnings": warns, "ts": iso()}
    print(json.dumps(report, indent=2))
    audit("doctor", status=status, issues=issues)
    return 1 if issues else 0


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

    ver = sub.add_parser("verify", help="Record substantive verification of CURRENT (requires a lease)")
    ver.add_argument("--agent", required=True)
    ver.add_argument("--run", default=None)
    ver.add_argument("--note", required=True, help="What was actually checked")
    ref.add_argument("--session", default="")

    st = sub.add_parser("status", help="Glance dashboard (CURRENT, leases, mail, threads)")
    st.add_argument("--json", action="store_true", dest="json")

    sub.add_parser("doctor", help="Health check (STALE, dual-SSOT, JSON)")

    t = sub.add_parser("test", help="Sandboxed smoke; --trail adds full matrix")
    t.add_argument("--smoke", action="store_true", help="Run A–F smoke (always run)")
    t.add_argument("--trail", action="store_true", help="Run effectiveness/accuracy/performance/chaos trails")

    chk = sub.add_parser("check-lease", help="Exit 0 if path covered by live lease for agent (enforcement helper)")
    chk.add_argument("--run", default=None)
    chk.add_argument("--agent", required=True)
    chk.add_argument("--path", required=True)

    ini = sub.add_parser("init", help="Scaffold AgentVault into a target directory (from portable template)")
    ini.add_argument("--target", default=".", help="Destination project root (default: cwd)")
    ini.add_argument("--force", action="store_true", help="Overwrite existing files")
    ini.add_argument("--full", action="store_true", help="Include OpenViking + GraphRAG + VectorRAG stubs")

    rot = sub.add_parser("rotate-events", help="HOT→WARM: archive old events.jsonl lines; keep last N")
    rot.add_argument("--keep", type=int, default=50, help="HOT retention (default 50)")
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
    run_id = getattr(args, "run", None)
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
    """P07 — move operational authority from the legacy files to the commit journal.

    One field flips. Because P03 never overwrote a legacy file, rolling back is a config
    change plus a regenerate, not a restore from backup, and every post-cutover commit
    survives the rollback.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_cw", Path(__file__).resolve().parent / "commit_worker.py")
    cw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cw)

    proto = read_protocol()
    target = args.to
    current = proto.get("authority", "legacy")
    if current == target:
        print(f"authority is already {target!r}; nothing to do")
        return 0

    # --- preflight: refuse to cut over onto a journal that is not clean.
    valid_seq, head_hash, problems = cw.verify_chain()
    if problems:
        print(f"FAIL: journal is not clean: {problems[0]}", file=sys.stderr)
        return 1
    if target == "journal" and valid_seq == 0:
        print("FAIL: journal is empty; import legacy state before cutting over", file=sys.stderr)
        return 1

    # --- drain: no live lease may be held by anyone but the cutover operator.
    others = [L for L in read_leases() if L.get("agent_id") != args.agent]
    if others and not args.force:
        for L in others:
            print(f"FAIL: lease held by {L.get('agent_id')} on {L.get('resources')} "
                  f"until {L.get('expires_at')}", file=sys.stderr)
        print("drain the workers or pass --force", file=sys.stderr)
        return 1

    # --- snapshot legacy mutable state read-only before it becomes derived.
    mig = COORD / "migrations" / args.rfc / "cutover"
    mig.mkdir(parents=True, exist_ok=True)
    snapshot = {}
    for rel in ("MemoryBank/coord/threads.json", "MemoryBank/CURRENT.md", "MemoryBank/board.md",
                "MemoryBank/activeContext.md"):
        src = ROOT / rel
        if src.exists():
            body = src.read_bytes()
            h = hashlib.sha256(body).hexdigest()
            (mig / f"{Path(rel).name}.{h[:12]}").write_bytes(body)
            snapshot[rel] = h
    atomic_write_json(mig / "pre-cutover-snapshot.json", {
        "captured_at": iso(), "from_authority": current, "to_authority": target,
        "journal_seq": valid_seq, "journal_head_hash": head_hash, "files": snapshot,
        "note": "Read-only recovery material. Never a live boot target.",
    })

    if args.dry_run:
        print(json.dumps({"status": "dry_run", "from": current, "to": target,
                          "journal_seq": valid_seq, "snapshot": snapshot}, indent=2))
        return 0

    # --- commit the transition, then flip the epoch.
    receipt = cw.commit("task.transition", {
        "task_id": args.task_id,
        "status": "authority_cutover",
        "from_authority": current, "to_authority": target,
        "epoch_from": proto.get("epoch", 1), "epoch_to": int(proto.get("epoch", 1)) + 1,
        "pre_cutover_snapshot": str((mig / "pre-cutover-snapshot.json").relative_to(ROOT)),
        "legacy_file_hashes": snapshot,
    }, agent_id=args.agent, task_id=args.task_id,
       # The epoch is part of the key: a later, legitimate cutover in the same direction
       # is a DIFFERENT event. Without it the second flip silently returned the first
       # flip's receipt and recorded nothing.
       idempotency_key=f"cutover-e{proto.get('epoch', 1)}-{current}-to-{target}")

    proto.update({
        "epoch": int(proto.get("epoch", 1)) + 1,
        "authority": target,
        "last_updated": iso(),
        "cutover_commit_seq": receipt.get("seq"),
        "cutover_snapshot": str((mig / "pre-cutover-snapshot.json").relative_to(ROOT)),
    })
    atomic_write_json(COORD / "protocol.json", proto)
    cw.rebuild_projections()
    audit("authority_cutover", **{"from": current, "to": target, "epoch": proto["epoch"],
                                  "seq": receipt.get("seq")})
    print(json.dumps({"status": "cutover_complete", "from": current, "to": target,
                      "epoch": proto["epoch"], "journal_seq": receipt.get("seq"),
                      "snapshot": str((mig / "pre-cutover-snapshot.json").relative_to(ROOT))},
                     indent=2))
    print("\nNow regenerate views:  python3 scripts/coord/avcoord.py refresh --views-only",
          file=sys.stderr)
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    """Remove expired leases. The only command with that side effect.

    Reaping used to happen inside read_live_leases(), so every status / doctor /
    check-lease call silently deleted lease files, concurrently with a lock-holding claim.
    """
    with FileLock(LEASES / ".claim.lock"):
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
    run_id = getattr(args, "run", None)
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
    return REPO_ROOT / "templates" / "agentvault-portable"


def _copy_file(src: Path, dest: Path, *, force: bool) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        return "skip"
    shutil.copy2(src, dest)
    return "wrote"


def cmd_init(args: argparse.Namespace) -> int:
    """Copy portable template into --target. Idempotent unless --force."""
    src = _portable_template_root()
    if not src.is_dir():
        print(
            f"FAIL: portable template missing at {src}. "
            "Clone agentvault and ensure templates/agentvault-portable/ exists.",
            file=sys.stderr,
        )
        return 1
    target = Path(args.target).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    # Always copy Core paths; --full also copies OpenViking/GraphRAG/VectorRAG
    core_prefixes = (
        "AGENTS.md",
        "AGENTS.codex.md",
        "ADOPT.md",
        "GEMINI.md",
        ".cursorrules",
        ".windsurfrules",
        ".cursor/",
        ".githooks/",
        ".github/",
        "bin/",
        "scripts/",
        "MemoryBank/",
        "EpisodicTracker/",
    )
    full_prefixes = core_prefixes + ("OpenViking/", "GraphRAG/", "VectorRAG/", "CLAUDE.md", "README.md", ".gitignore")
    prefixes = full_prefixes if args.full else core_prefixes + ("CLAUDE.md", "README.md", ".gitignore")

    wrote = skipped = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        if path.name == ".DS_Store":
            continue
        rel = path.relative_to(src).as_posix()
        if not any(rel == p.rstrip("/") or rel.startswith(p) for p in prefixes):
            continue
        # Core install without --full still needs empty GraphRAG/VectorRAG? Plan says Core without them.
        action = _copy_file(path, target / rel, force=args.force)
        if action == "wrote":
            wrote += 1
            print(f"  + {rel}")
        else:
            skipped += 1

    # Ensure executable bits
    for exe in (target / "bin" / "avcoord", target / "scripts" / "coord" / "avcoord.py", target / ".cursor" / "hooks" / "avcoord-lease-check.py", target / ".githooks" / "pre-commit"):
        if exe.exists():
            exe.chmod(exe.stat().st_mode | 0o111)

    # Symlink CLAUDE.md → AGENTS.md if missing
    claude = target / "CLAUDE.md"
    if not claude.exists() or args.force:
        try:
            if claude.exists() or claude.is_symlink():
                claude.unlink()
            claude.symlink_to("AGENTS.md")
            print("  + CLAUDE.md -> AGENTS.md")
            wrote += 1
        except OSError:
            if not claude.exists():
                claude.write_text("Follow AGENTS.md Boot exactly.\n", encoding="utf-8")
                wrote += 1

    print(json.dumps({"status": "ok", "target": str(target), "wrote": wrote, "skipped": skipped, "full": bool(args.full)}))
    print("Next: cd", target, "&& bin/avcoord doctor && bin/avcoord status")
    print("Optional: git config core.hooksPath .githooks")
    return 0


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
        "init": cmd_init,
        "rotate-events": cmd_rotate_events,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
