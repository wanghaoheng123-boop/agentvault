"""RFC T05 — path authorization.

Every wrong-ALLOW below was reproduced against the pre-P02 coordinator before these tests
were written; they are regression guards, not hypotheticals.
"""

from __future__ import annotations

import pytest

from conftest import claim, ns


# --------------------------------------------------------------------------- canon kernel

@pytest.mark.parametrize(
    "raw,expected,subtree",
    [
        ("scripts/evolution", "scripts/evolution", False),   # need not exist on disk
        ("scripts/coord/**", "scripts/coord", True),
        ("OpenViking/", "OpenViking", True),
        ("MemoryBank\\coord\\next_ids.json", "MemoryBank/coord/next_ids.json", False),
        (".//MemoryBank//CURRENT.md", "MemoryBank/CURRENT.md", False),
        ("MemoryBank/coord/../CURRENT.md", "MemoryBank/CURRENT.md", False),
    ],
)
def test_canon_normalizes(av, raw, expected, subtree):
    c = av.canon_resource(raw)
    assert c.display == expected
    assert c.explicit_subtree is subtree


@pytest.mark.parametrize(
    "raw,code",
    [
        ("../../etc/CURRENT.md", "ROOT_ESCAPE"),
        ("/etc/passwd", "OUTSIDE_ROOT"),
        ("scripts/*/x.py", "UNSUPPORTED_GLOB"),
        ("a/*", "UNSUPPORTED_GLOB"),
        ("f?o", "UNSUPPORTED_GLOB"),
        ("", "EMPTY"),
        ("   ", "EMPTY"),
    ],
)
def test_canon_fails_closed(av, raw, code):
    with pytest.raises(av.CanonError) as e:
        av.canon_resource(raw)
    assert e.value.code == code


def test_canon_accepts_absolute_inside_root(av):
    assert av.canon_resource(str(av.ROOT / "MemoryBank/CURRENT.md")).display == "MemoryBank/CURRENT.md"


def test_canon_survives_case_variant_root(av):
    """The measured exit-0 bypass: a case-variant root made the path 'not contested'."""
    spelled = str(av.ROOT).replace("ws", "ws")  # root itself
    weird = spelled[:-1] + spelled[-1].upper() if spelled[-1].isalpha() else spelled
    c = av.canon_or_none(weird + "/MemoryBank/CURRENT.md")
    assert c is None or c.display == "MemoryBank/CURRENT.md"


def test_canon_dereferences_symlink(av, tmp_path):
    link = av.ROOT / "CLAUDE.md"
    link.symlink_to("AGENTS.md")
    assert av.canon_resource("CLAUDE.md").display == "AGENTS.md"
    assert av.path_is_contested("CLAUDE.md") is True


# ------------------------------------------------------------------- directional coverage

@pytest.mark.parametrize(
    "lease,target,allowed",
    [
        # exact
        ("MemoryBank/CURRENT.md", "MemoryBank/CURRENT.md", True),
        # subtree
        ("scripts/coord/**", "scripts/coord/avcoord.py", True),
        ("scripts/coord", "scripts/coord/avcoord.py", True),
        # ANCESTOR — a lease on a descendant must never authorize its parent
        ("MemoryBank/CURRENT.md", "MemoryBank", False),
        ("scripts/coord/**", "scripts", False),
        ("docs/manuscript/foo", "docs/manuscript", False),
        # SIBLING PREFIX — no separator boundary in the old startswith arm
        ("docs/manuscript/foo", "docs/manuscript/foobar.md", False),
        ("MemoryBank/CURRENT.md", "MemoryBank/CURRENT.md.bak", False),
        ("docs/manuscript", "docs/manuscriptXXX", False),
        # unrelated
        ("reports/a", "reports/b", False),
        # alias spellings resolve to the same resource
        ("MemoryBank/CURRENT.md", ".//MemoryBank//CURRENT.md", True),
        ("MemoryBank/CURRENT.md", "memorybank/current.md", True),
        # escapes are never authorized
        ("MemoryBank/CURRENT.md", "../../etc/passwd", False),
    ],
)
def test_lease_authorizes_is_directional(av, lease, target, allowed):
    assert av.lease_authorizes(lease, target) is allowed


def test_overlap_is_symmetric_but_authorization_is_not(av):
    parent, child = av.canon_resource("a/b"), av.canon_resource("a/b/c")
    assert av.resources_overlap(parent, child) is True
    assert av.resources_overlap(child, parent) is True
    assert av.lease_authorizes("a/b", "a/b/c") is True
    assert av.lease_authorizes("a/b/c", "a/b") is False


def test_boundary_safety(av):
    assert av.resources_overlap(av.canon_resource("foo"), av.canon_resource("foobar")) is False


# ------------------------------------------------------------------ contested detection

def test_contested_is_permissive_upward(av):
    """A write to MemoryBank/ still has to be flagged: it swallows CURRENT.md."""
    assert av.path_is_contested("MemoryBank/CURRENT.md") is True
    assert av.path_is_contested("MemoryBank") is True
    assert av.path_is_contested("OpenViking/L0_Core_Directives.md") is True


def test_contested_bare_name_uses_basename_not_endswith(av):
    assert av.path_is_contested("reports/notindex.json") is False
    assert av.path_is_contested("reports/index.json") is True


def test_contested_traversal_is_not_stripped_into_a_match(av):
    """'../../etc/CURRENT.md'.lstrip('./') used to become 'etc/CURRENT.md'."""
    assert av.path_is_contested("../../etc/CURRENT.md") is True  # uncanonicalizable -> contested


# ------------------------------------------------------------------- end-to-end check-lease

def test_check_lease_denies_sibling_and_ancestor(av, capsys):
    claim(av, "orchestrator", "docs/manuscript/foo", ttl="10m")
    def check(path):
        return av.cmd_check_lease(ns(agent="orchestrator", path=path, run=None))

    assert check("docs/manuscript/foo") == 0          # exact
    assert check("docs/manuscript/foo/x.md") == 0     # real subtree
    assert check("docs/manuscript/foobar.md") == 1    # sibling prefix
    assert check("docs/manuscript") == 1              # ancestor


def test_check_lease_fails_closed_on_bad_path(av):
    assert av.cmd_check_lease(ns(agent="orchestrator", path="../../etc/passwd", run=None)) == 1


def test_equivalent_aliases_cannot_take_separate_leases(av):
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m") == 0
    assert claim(av, "code_generator", ".//MemoryBank//CURRENT.md", ttl="10m") == 1


def test_claim_refuses_workspace_root(av):
    assert claim(av, "orchestrator", ".", ttl="10m") == 1


def test_claim_rejects_unsupported_glob(av):
    assert claim(av, "orchestrator", "scripts/*/x.py", ttl="10m") == 1


def test_lease_filenames_are_unchanged(av):
    """Canonicalization must not feed resource_hash, or live lease files are orphaned."""
    assert av.resource_hash("scripts/coord/**") == "9bf845a5a32659b8"
    assert av.resource_hash("MemoryBank/CURRENT.md") == "1357e0cc7fde517c"


# ------------------------------------------------- jurisdiction vs attack (hook regression)

def test_path_outside_workspace_is_allowed_not_denied(av):
    """Outside the root is not our jurisdiction. Denying it blocks every edit elsewhere
    on the machine via the Cursor hook, which forwards paths verbatim."""
    assert av.cmd_check_lease(ns(agent="orchestrator", path="/tmp/somewhere-else.md", run=None)) == 0


def test_traversal_escape_is_still_denied(av):
    assert av.cmd_check_lease(ns(agent="orchestrator", path="../../etc/passwd", run=None)) == 1


def test_malformed_and_globbed_paths_are_denied(av):
    for bad in ("scripts/*/x.py", "", "   "):
        assert av.cmd_check_lease(ns(agent="orchestrator", path=bad, run=None)) == 1
