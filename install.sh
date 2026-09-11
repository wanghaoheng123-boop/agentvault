#!/usr/bin/env bash
# AgentVault one-command bootstrap.
#
#   Into the current directory:
#     curl -fsSL https://raw.githubusercontent.com/wanghaoheng123-boop/agentvault/main/install.sh | bash
#
#   Into a named directory, with the optional evaluation engine:
#     curl -fsSL .../install.sh | bash -s -- ./my-project --with-aeap
#
#   From a clone (identical result, and you can read the script first):
#     git clone https://github.com/wanghaoheng123-boop/agentvault.git
#     ./agentvault/install.sh ./my-project
#
# Piping a script from the network into a shell means trusting this file. If you
# would rather read it first, use the clone form above — it is the same script.
set -euo pipefail

TARGET="${1:-$PWD}"
shift 2>/dev/null || true
WITH_AEAP=0
for arg in "$@"; do
  case "$arg" in
    --with-aeap) WITH_AEAP=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option '$arg'" >&2; exit 2 ;;
  esac
done

log() { printf '  %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

command -v git     >/dev/null 2>&1 || die "git is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required (3.11+)"

python3 - <<'PY' || die "python3 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

# Locate the release payload. When this script sits next to RELEASE.json it is
# already an unpacked release; when piped from curl it is not, so fetch one.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
CLEANUP=""
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/RELEASE.json" ]; then
  SOURCE="$SELF_DIR"
  log "using local release at $SOURCE"
else
  SOURCE="$(mktemp -d)"
  CLEANUP="$SOURCE"
  log "fetching AgentVault…"
  git clone --depth 1 --quiet \
    https://github.com/wanghaoheng123-boop/agentvault.git "$SOURCE"
fi
# shellcheck disable=SC2064
[ -n "$CLEANUP" ] && trap "rm -rf '$CLEANUP'" EXIT

[ -f "$SOURCE/RELEASE.json" ] || die "no RELEASE.json in the payload; not an AgentVault release"

mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd)"

# A git repository is required: leases are enforced by a commit hook.
if [ ! -d "$TARGET/.git" ]; then
  log "initialising a git repository in $TARGET"
  git -C "$TARGET" init --quiet
fi

log "installing the workspace…"
INIT_ARGS=(init --target "$TARGET" --full)
[ "$WITH_AEAP" -eq 1 ] && INIT_ARGS+=(--with-aeap)
python3 "$SOURCE/scripts/coord/avcoord.py" "${INIT_ARGS[@]}" >/dev/null

# Activate the enforcement floor: no commit to a contested path without a lease.
git -C "$TARGET" config core.hooksPath .githooks
log "enabled the lease-enforcing pre-commit hook"

cat <<EOF

AgentVault is installed in $TARGET

  What you have
    .agentvault/        task board, branch handoffs, invariants, ADRs
    .config/wt.toml     worktree lifecycle hooks
    MemoryBank/         coordination state, journal, leases
    bin/avcoord         the coordination CLI

  Next
    cd "$TARGET"
    cat AGENTS.md                      # the operating protocol; CLAUDE.md mirrors it
    cat .agentvault/INDEX.md           # routing table — where everything lives
    bin/avcoord doctor                 # health check
    bin/avcoord status                 # board, leases, mail

  Then open the folder in Claude Code, Cursor, Codex, Copilot, Gemini or
  Windsurf. Every one of them reads AGENTS.md, so they share one protocol,
  one task board, and one handoff format.

  Two things to personalise
    .agentvault/INDEX.md               # add your project's test command
    MemoryBank/coord/contested.json    # declare your multi-writer hot paths

EOF
