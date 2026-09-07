"""AST firewall — static validation of a candidate expression.

Ordering is the whole point: this runs to completion BEFORE any feature handle, label
column or data path is supplied to the candidate. A candidate that fails here never
reaches execution, so it never sees data at all.

What this is NOT: an OS security boundary. AST filtering constrains a *language*; it does
not contain a hostile process. That is why `sandbox.v1.yaml` records
`is_security_boundary: false` and admission stays disabled.

Accepted language: a small typed expression grammar over declared features and the
operators in `operators.REGISTRY`. Anything else is rejected by default — the allowlist
is closed, so an operation nobody thought about is denied rather than permitted.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import keyword
import math
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import operators

POLICY_PATH = Path(__file__).resolve().parents[1] / "policies" / "firewall.v1.yaml"
# A policy may tighten these parser safety ceilings, never enlarge them.
_PARSER_CAPS = {"max_source_bytes": 2000, "max_nodes": 400, "max_depth": 20,
                "max_tokens": 128, "max_numeric_literal_chars": 32}
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_RESERVED = {"features", "compute", "eval", "exec", "getattr", "setattr", "open",
             "globals", "locals", "compile", "fwd_excess", "returns", "target", "label"}
_BINOP = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div"}


@dataclass
class FirewallResult:
    ok: bool
    candidate_id: str
    expression_sha256: str
    operator_registry_sha256: str
    policy_sha256: str
    violations: list[str] = field(default_factory=list)
    used_features: list[str] = field(default_factory=list)
    used_operators: list[str] = field(default_factory=list)
    node_count: int = 0
    depth: int = 0

    def to_dict(self) -> dict:
        return {
            "schema_version": "1.0", "status": "PASS" if self.ok else "FAIL",
            "candidate_id": self.candidate_id,
            "expression_sha256": self.expression_sha256,
            "operator_registry_sha256": self.operator_registry_sha256,
            "policy_sha256": self.policy_sha256, "violations": self.violations,
            "used_features": sorted(set(self.used_features)),
            "used_operators": sorted(set(self.used_operators)),
            "node_count": self.node_count, "depth": self.depth,
        }


def load_policy(path: Path | None = None) -> tuple[dict, str]:
    raw = (path or POLICY_PATH).read_bytes()
    return yaml.safe_load(raw), hashlib.sha256(raw).hexdigest()


def _preflight(expression: str, pre: dict) -> str | None:
    """Linear, bounded token scan BEFORE Python's parser sees the expression.

    Structural tokens conservatively bound depth: even a flat binary/unary chain may
    produce a deep AST. Counting only parentheses misses that case. Each operator or
    opening delimiter consumes one level of the total depth budget. This deliberately
    rejects some broad trees that would fit the later AST budget, in exchange for a
    simple bound on every parser input (Python AST docs warn about C-stack exhaustion).
    """
    count = structure = nesting = 0
    ignored = {tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER, tokenize.ENCODING}
    try:
        for token in tokenize.generate_tokens(io.StringIO(expression).readline):
            if token.type in ignored:
                continue
            count += 1
            if count > pre["max_tokens"] or 4 * count + 1 > pre["max_nodes"]:
                return "pre-parse token/node budget exceeds max_nodes or max_tokens"
            if token.type == tokenize.NUMBER and len(token.string) > pre["max_numeric_literal_chars"]:
                return "numeric literal exceeds pre-parse size budget"
            if token.type == tokenize.OP:
                if token.string in "([{":
                    nesting += 1
                elif token.string in ")]}":
                    nesting -= 1
                if token.string not in (",", ")", "]", "}"):
                    structure += 1
                if structure + 2 > pre["max_depth"] or nesting > pre["max_depth"]:
                    return f"pre-parse structural depth exceeds max_depth={pre['max_depth']}"
            if token.type in {tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}:
                return "disallowed syntax: comments and indentation are not factor expressions"
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        return f"syntax error: {exc}"
    return None


def _tree_metrics(tree: ast.AST, pre: dict) -> tuple[int, int, str | None]:
    stack, count, depth = [(tree, 0)], 0, 0
    while stack:
        node, level = stack.pop()
        count += 1
        depth = max(depth, level)
        if count > pre["max_nodes"] or depth > pre["max_depth"]:
            return count, depth, "AST node count/depth exceeds configured budget"
        stack.extend((child, level + 1) for child in ast.iter_child_nodes(node))
    return count, depth, None


def _literal_window(node: ast.AST) -> int | None:
    if type(node) is ast.Constant and type(node.value) is int:
        return node.value
    if (type(node) is ast.UnaryOp and type(node.op) is ast.USub
            and type(node.operand) is ast.Constant and type(node.operand.value) is int):
        return -node.operand.value
    return None


def check(expression: str, declared_features: list[str], *, candidate_id: str = "",
          policy_path: Path | None = None) -> FirewallResult:
    """Validate without receiving data handles; all walks and parser inputs are bounded."""
    policy, policy_sha = load_policy(policy_path)
    res = FirewallResult(False, candidate_id, "", operators.registry_sha256(), policy_sha)
    v = res.violations
    pre = {}
    for name, ceiling in _PARSER_CAPS.items():
        limit = policy.get("pre_parse", {}).get(name, ceiling)
        if type(limit) is not int or not 1 <= limit <= ceiling:
            v.append(f"invalid parser policy {name}: must be in 1..{ceiling}")
        pre[name] = limit
    if v:
        return res
    if not isinstance(expression, str):
        v.append("expression must be a string")
        return res
    # Check characters before UTF-8 encoding/hashing to avoid an unbounded allocation.
    if len(expression) > pre["max_source_bytes"]:
        v.append(f"source exceeds max_source_bytes={pre['max_source_bytes']}")
        return res
    try:
        raw = expression.encode("utf-8")
    except UnicodeEncodeError:
        v.append("expression must contain valid UTF-8")
        return res
    res.expression_sha256 = hashlib.sha256(raw).hexdigest()
    if len(raw) > pre["max_source_bytes"]:
        v.append(f"source exceeds max_source_bytes={pre['max_source_bytes']}")
        return res
    if not expression.strip():
        v.append("empty expression")
        return res
    if not expression.isascii() or "\x00" in expression:
        v.append("disallowed syntax: expressions must be ASCII and contain no NUL")
        return res
    allowed_ops = set(policy.get("allowed_operators", {}))
    if not allowed_ops <= set(operators.REGISTRY):
        v.append("policy names an operator outside the trusted typed registry")
        return res
    for name in allowed_ops:
        declaration = policy["allowed_operators"][name]
        if not isinstance(declaration, dict) or declaration.get("arity") != operators.ARITY[name]:
            v.append(f"{name}: policy arity disagrees with typed registry")
    if not isinstance(declared_features, list) or len(declared_features) > 64:
        v.append("declared_features must be a bounded list of identifiers")
        return res
    declared = set()
    for name in declared_features:
        if (not isinstance(name, str) or not _IDENTIFIER.fullmatch(name)
                or keyword.iskeyword(name) or "__" in name
                or name in _RESERVED or name in operators.REGISTRY):
            v.append(f"invalid or reserved feature identifier {name!r}")
        elif name in declared:
            v.append(f"duplicate declared feature {name!r}")
        else:
            declared.add(name)
    if v:
        return res
    error = _preflight(expression, pre)
    if error:
        v.append(error)
        return res
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        v.append(f"syntax error or parser resource rejection: {type(exc).__name__}: {exc}")
        return res
    res.node_count, res.depth, error = _tree_metrics(tree, pre)
    if error:
        v.append(error)
        return res

    # Iterative postorder type inference. No recursive NodeVisitor or _depth function.
    types: dict[int, str] = {}
    pending = [(tree.body, False)]
    temporal = policy["temporal_rules"]
    while pending:
        node, visited = pending.pop()
        if visited:
            args = ([node.operand] if type(node) is ast.UnaryOp else
                    [node.left, node.right] if type(node) is ast.BinOp else node.args)
            name = ("neg" if type(node) is ast.UnaryOp else
                    _BINOP[type(node.op)] if type(node) is ast.BinOp else node.func.id)
            arg_types = [types.get(id(arg), "invalid") for arg in args]
            widx = operators.WINDOW_ARG.get(name)
            if widx is not None and len(args) > widx:
                window = _literal_window(args[widx])
                if window is None:
                    v.append(f"{name}: window must be a constant int literal, not a computed value")
                else:
                    arg_types[widx] = "window"
                    minimum = operators.MIN_WINDOW[name]
                    maximum = min(int(temporal["window_max"]), operators.MAX_WINDOW)
                    if window < 0:
                        v.append(f"{name}: negative window {window} reads the future")
                    elif window < minimum:
                        v.append(f"{name}: window {window} below window_min={minimum}")
                    if window > maximum:
                        v.append(f"{name}: window {window} above window_max={maximum}")
            try:
                types[id(node)] = operators.result_type(name, arg_types)
            except operators.OperatorError as exc:
                v.append(str(exc))
                types[id(node)] = "invalid"
            continue
        if type(node) is ast.Name:
            if node.id in declared:
                res.used_features.append(node.id)
                types[id(node)] = "series"
            elif node.id in allowed_ops:
                v.append(f"operator '{node.id}' used as a bare name")
            else:
                v.append(f"undeclared feature or unknown name '{node.id}'")
        elif type(node) is ast.Constant:
            if type(node.value) not in (int, float):
                v.append(f"only numeric constants are allowed, got {type(node.value).__name__}")
            elif not math.isfinite(node.value) or abs(node.value) > operators.MAX_SCALAR:
                v.append("numeric constant exceeds finite scalar bound")
            else:
                types[id(node)] = "scalar"
        elif type(node) in (ast.UnaryOp, ast.BinOp, ast.Call):
            if type(node) is ast.Call:
                if type(node.func) is not ast.Name:
                    v.append("only direct calls to allowlisted operators are permitted")
                    continue
                name, args = node.func.id, node.args
                if node.keywords:
                    v.append(f"{name}: keyword arguments are not permitted")
            elif type(node) is ast.BinOp:
                name, args = _BINOP.get(type(node.op)), [node.left, node.right]
            else:
                name, args = ("neg" if type(node.op) is ast.USub else None), [node.operand]
            if name not in allowed_ops:
                v.append(f"operator '{name}' not in policy allowlist")
                continue
            if len(args) != operators.ARITY[name]:
                v.append(f"{name}: expected {operators.ARITY[name]} args, got {len(args)}")
                continue
            res.used_operators.append(name)
            pending.append((node, True))
            pending.extend((arg, False) for arg in reversed(args))
        else:
            v.append(f"disallowed syntax: {type(node).__name__}")
    if not res.used_features:
        v.append("expression references no declared feature")
    if types.get(id(tree.body)) != "series":
        v.append("factor expression must return a Series")
    res.ok = not v
    return res


def lower_expression(expression: str, declared_features: list[str], *,
                     policy_path: Path | None = None) -> str:
    """Validate then lower arithmetic to the checked operator implementation.

    Executors must run this expression, because Python's raw infix division bypasses
    div's missing/zero-denominator rules. The lowered form is generated, never authored.
    """
    result = check(expression, declared_features, policy_path=policy_path)
    if not result.ok:
        raise ValueError("firewall rejected expression: " + "; ".join(result.violations))
    root = ast.parse(expression, mode="eval").body
    rendered: dict[int, str] = {}
    stack = [(root, False)]
    while stack:
        node, done = stack.pop()
        if type(node) is ast.Name:
            rendered[id(node)] = node.id
        elif type(node) is ast.Constant:
            rendered[id(node)] = repr(node.value)
        else:
            name = ("neg" if type(node) is ast.UnaryOp else
                    _BINOP[type(node.op)] if type(node) is ast.BinOp else node.func.id)
            args = ([node.operand] if type(node) is ast.UnaryOp else
                    [node.left, node.right] if type(node) is ast.BinOp else node.args)
            if done:
                rendered[id(node)] = f"{name}({', '.join(rendered[id(a)] for a in args)})"
            else:
                stack.append((node, True))
                stack.extend((arg, False) for arg in reversed(args))
    return rendered[id(root)]


def emit_code(expression: str, declared_features: list[str]) -> str:
    """White-box projection with exactly the same checked arithmetic as the executor."""
    lowered = lower_expression(expression, declared_features)
    return (
        "# GENERATED — do not edit. Emitted from the frozen candidate expression.\n"
        f"from aeap.engine.operators import {', '.join(sorted(operators.REGISTRY))}\n\n\n"
        "def compute(features):\n"
        f"    \"\"\"Declared features: {', '.join(sorted(declared_features))}\"\"\"\n"
        + "".join(f"    {name} = features[{name!r}]\n" for name in sorted(declared_features))
        + f"    return {lowered}\n"
    )


def receipt_sha256(result: FirewallResult) -> str:
    return hashlib.sha256(
        json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
