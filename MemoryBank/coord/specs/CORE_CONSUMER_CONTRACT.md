---
version: 1.0
type: software_distribution_contract
status: active
last_updated: 2026-09-09T07:58:00+08:00
---

# Public core / private consumer contract

AgentVault has three roles with one direction of authority:

1. The private development source contains generic core source plus private instance data.
2. The clean release builder selects only declared public source paths, applies declared
   transforms, and emits an immutable artifact with a deterministic payload digest.
3. A consumer installs a pinned artifact. Its operational state remains private and is never
   copied back into the core or release.

The generated portable tree is a release artifact, not a second editing location. Changes to
generic code start in the source tree and reach the portable tree only through the release
builder. A pure release check rejects drift between a saved manifest and the artifact.

## Profiles

- `core`: agent adapters, coordinator, contracts, seed MemoryBank, and core dependencies.
- `full`: core plus generic OpenViking, GraphRAG, and VectorRAG scaffolding.
- `aeap`: an explicit optional development extension. It ships uncalibrated with production
  admission disabled.

The artifact manifest binds every path, byte hash, and executable mode. The payload digest is
computed without wall-clock metadata. A release timestamp may be recorded separately and has
no effect on payload identity.

The 2.1.0 portable release declares state schema `agentvault-v1`. Install and upgrade receipts
must match both values exactly; missing or incompatible pins fail before target writes.

## Ownership and pin

Each consumer records software ownership in root `AGENTVAULT_INSTALL.json`. It contains the
release version, selected profiles, release-manifest and payload digests, state-schema version,
and the hash/mode of each managed software file. `AGENTVAULT_INSTALL.json` is a software pin,
not an operational-state store and therefore is not a second SSOT.

All `MemoryBank/`, `EpisodicTracker/`, `GraphRAG/`, `VectorRAG/`, project documents, research,
datasets, local policy, credentials, and explicitly declared overrides are user owned after
initial creation. They never appear in the managed-files map. The installer preserves them on
repeat install, upgrade, and rollback.

## Install, upgrade, and rollback

Installation verifies the artifact before the first target write. It rejects unsafe paths,
path aliases, unsupported modes, unexpected symlinks, runtime debris, missing core files, and
hash mismatches. A fresh install generates current timestamps and empty state rather than
copying the developer instance's state.

Upgrade is a compare-and-swap operation over managed files. It first produces a write-free
preview. A managed file is replaceable only when its current hash equals the prior receipt.
Local changes produce a conflict before any file is written. State-schema incompatibility also
stops before writes. Accepted upgrades retain the previous receipt and before-images in
`AGENTVAULT_INSTALL_HISTORY/`.

Before the first managed write, the installer durably publishes
`AGENTVAULT_INSTALL_PENDING.json` and immutable before/after blobs. A process crash leaves that
marker in place, and `doctor` fails closed until the operator runs one of:

```sh
bin/avcoord init --target . --recover resume --preview
bin/avcoord init --target . --recover resume
# or restore the complete before-image
bin/avcoord init --target . --recover rollback
```

Recovery verifies every current, before, and intended-after hash and can itself be resumed
after another interruption. It never treats the software marker as task or session state.

Rollback verifies the current managed hashes against the installed receipt, restores only the
recorded before-images, and leaves user-owned paths untouched. A conflicting local change stops
the rollback before any restoration. Every install, upgrade, and rollback is tested in a
disposable directory made from the exact unpacked release layout.

## Release trust boundary

The builder receives an explicit public allowlist and transform policy. It constructs a new
empty staging tree, materializes approved adapters as regular files, scans every shipped byte,
rejects unknown output files, and verifies the final manifest again after tests. Publication
credentials and remote publication stay outside ordinary private-workspace sessions. A distinct
security or peer reviewer acknowledges the final payload digest before publication.

This contract does not claim host-process isolation, multi-host concurrent writing, public
repository settings, or production AEAP qualification. Those capabilities require separate
evidence and cannot be inferred from a successful software build.
