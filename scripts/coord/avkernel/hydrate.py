#!/usr/bin/env python3
"""JIT working-set compiler — snapshot + δ + approved rules, token-bounded."""

from __future__ import annotations

import json
from typing import Any

from . import compact, commit_rebased, store


def _estimate_tokens(text: str) -> int:
    # Rough: ~4 chars/token for English/code mix.
    return max(1, len(text) // 4)


def _rule_oneliners(events: list[dict], task_id: str, tags: list[str], budget_left: int) -> list[str]:
    lines: list[str] = []
    task_l = (task_id or "").lower()
    tagset = {t.lower() for t in tags}
    scored: list[tuple[int, str]] = []
    accepted: dict[str, dict] = {}
    for event in events:
        if event["type"] != "fingerprint.promoted":
            continue
        body = event.get("body") or {}
        doc = body.get("rule_document")
        rid = body.get("fingerprint_id")
        if isinstance(rid, str) and isinstance(doc, dict):
            accepted[rid] = doc
    for rid, doc in sorted(accepted.items()):
        desc = (doc.get("rule") or {}).get("description") or doc.get("description") or ""
        pred = (doc.get("rule") or {}).get("predicate") or ""
        rtags = [str(t).lower() for t in (doc.get("tags") or [])]
        score = 0
        if task_l and task_l in rid.lower():
            score += 3
        if tagset & set(rtags):
            score += 2
        if task_l and task_l in desc.lower():
            score += 1
        line = f"- [{rid}] {desc or pred}"
        scored.append((score, line))
    scored.sort(key=lambda x: (-x[0], x[1]))
    for _, line in scored:
        if _estimate_tokens("\n".join(lines + [line])) > budget_left:
            break
        lines.append(line)
    return lines


def hydrate(
    av: Any,
    *,
    task_id: str = "",
    budget: int | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Compile a plain-English + JSON working set for the agent."""
    cfg = commit_rebased.read_config(av)
    budget = budget if budget is not None else int(cfg["hydrate_token_budget"])
    cw = compact._cw(av)
    events = commit_rebased.journal_events(cw)
    slots = commit_rebased.journal_slots(cw, events)
    snap_doc = compact.load_latest_snapshot_doc(av)
    # Full fold is canonical: delta-only task replacement loses fields and revisions.
    state = cw.fold(events)
    snapshots = [e for e in events if e["type"] == "snapshot.created"]
    snap_meta = {"epoch": snap_doc["epoch"]} if snap_doc else None
    delta = [e for e in events if e["seq"] > snap_doc["last_seq"]] if snap_doc else events
    head_seq, head_hash = (events[-1]["seq"], events[-1]["hash"]) if events else (0, cw.ZERO_HASH)

    # Task focus.
    tasks = state.get("tasks") or {}
    focus = None
    if task_id and task_id in tasks:
        focus = tasks[task_id]
    elif task_id:
        focus = {"task_id": task_id, "status": "unknown"}

    open_tasks = [
        {"task_id": t.get("task_id"), "status": t.get("status"), "revision": t.get("revision")}
        for t in tasks.values()
        if (t.get("status") or "").lower() not in ("done", "closed", "cancelled")
    ][:12]

    rules_budget = max(80, budget // 4)
    rules = _rule_oneliners(events, task_id, tags or [], rules_budget)

    digest_lines = [
        f"AK-SPWS hydrate | head_seq={head_seq} | snapshot_epoch="
        f"{snap_meta['epoch'] if snap_meta else 0} | delta_events={len(delta) if snap_doc else 'full'}",
        f"Focus: {json.dumps(focus, ensure_ascii=False) if focus else '(none)'}",
        f"Open tasks ({len(open_tasks)}):",
    ]
    for t in open_tasks:
        digest_lines.append(f"  - {t['task_id']}: {t.get('status')} rev={t.get('revision')}")
    if rules:
        digest_lines.append("Approved rules:")
        digest_lines.extend(f"  {r}" for r in rules)
    else:
        digest_lines.append("Approved rules: (none)")

    # Slot summary (versions only — keep small).
    slot_lines = [f"{s['id']}=v{s['version']}" for s in slots[:20]]
    if slot_lines:
        digest_lines.append("Slots: " + ", ".join(slot_lines))

    digest = "\n".join(digest_lines)
    # Trim to budget.
    while _estimate_tokens(digest) > budget and len(digest_lines) > 4:
        digest_lines.pop(-2)  # drop from middle/end of lists
        digest = "\n".join(digest_lines)

    return {
        "status": "ok",
        "digest": digest,
        "token_estimate": _estimate_tokens(digest),
        "budget": budget,
        "head_seq": head_seq,
        "head_hash": head_hash,
        "snapshot_epoch": int(snap_meta["epoch"]) if snap_meta else 0,
        "delta_count": len(delta) if snap_doc else None,
        "focus": focus,
        "open_tasks": open_tasks,
        "rules": rules,
        "slots": [{"id": s["id"], "version": s["version"]} for s in slots],
    }
