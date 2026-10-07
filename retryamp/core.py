"""Core analysis for retryamp.

The model is a directed call graph. Each edge carries the caller's retry
policy for that dependency. From it we derive, without running anything:

* worst-case load multiplier on every service (sum over paths of the product
  of attempts along the path), i.e. how many requests a single entry request
  can turn into when everything downstream is failing;
* worst-case wall time an edge can consume (tries * per-try timeout + backoff);
* a set of findings about policies that are known to cause retry storms or
  wasted work.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

try:  # Python 3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

SEVERITIES = ("info", "warn", "error")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}


class SpecError(ValueError):
    pass


@dataclass
class Service:
    name: str
    entry: bool = False
    deadline_ms: Optional[float] = None
    rps: Optional[float] = None
    capacity_rps: Optional[float] = None


@dataclass
class Call:
    src: str
    dst: str
    attempts: int = 1
    per_try_timeout_ms: Optional[float] = None
    backoff_ms: float = 0.0
    backoff_multiplier: float = 2.0
    max_backoff_ms: Optional[float] = None
    jitter: bool = False
    idempotent: bool = True
    fanout: int = 1  # calls per incoming request (e.g. batch splits)

    def backoff_total_ms(self) -> float:
        total, delay = 0.0, float(self.backoff_ms)
        for _ in range(max(self.attempts - 1, 0)):
            d = delay if self.max_backoff_ms is None else min(delay, self.max_backoff_ms)
            total += d
            delay *= self.backoff_multiplier
        return total

    def worst_case_ms(self) -> Optional[float]:
        if self.per_try_timeout_ms is None:
            return None
        return self.attempts * self.per_try_timeout_ms + self.backoff_total_ms()


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    where: str
    path: List[str] = field(default_factory=list)


@dataclass
class Spec:
    services: Dict[str, Service]
    calls: List[Call]
    max_amplification: float = 10.0


def _load_raw(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as fh:
        data = fh.read()
    if ext == ".json":
        return json.loads(data.decode("utf-8"))
    if ext == ".toml":
        if tomllib is None:  # pragma: no cover
            raise SpecError("TOML needs Python 3.11+; use JSON instead")
        return tomllib.loads(data.decode("utf-8"))
    raise SpecError(f"unsupported spec extension {ext!r} (use .toml or .json)")


def parse_spec(raw: dict) -> Spec:
    if not isinstance(raw, dict):
        raise SpecError("spec must be a table/object")
    defaults = raw.get("defaults", {}) or {}
    services: Dict[str, Service] = {}
    for name, body in (raw.get("services") or {}).items():
        body = body or {}
        services[name] = Service(
            name=name,
            entry=bool(body.get("entry", False)),
            deadline_ms=body.get("deadline_ms"),
            rps=body.get("rps"),
            capacity_rps=body.get("capacity_rps"),
        )
    calls: List[Call] = []
    for i, c in enumerate(raw.get("calls") or []):
        try:
            src, dst = c["from"], c["to"]
        except (KeyError, TypeError):
            raise SpecError(f"calls[{i}] needs 'from' and 'to'")
        attempts = int(c.get("attempts", 1))
        if attempts < 1:
            raise SpecError(f"calls[{i}] attempts must be >= 1 (it counts the first try)")
        calls.append(Call(
            src=src, dst=dst, attempts=attempts,
            per_try_timeout_ms=c.get("per_try_timeout_ms"),
            backoff_ms=float(c.get("backoff_ms", 0)),
            backoff_multiplier=float(c.get("backoff_multiplier", 2.0)),
            max_backoff_ms=c.get("max_backoff_ms"),
            jitter=bool(c.get("jitter", False)),
            idempotent=bool(c.get("idempotent", True)),
            fanout=int(c.get("fanout", 1)),
        ))
    return Spec(services=services, calls=calls,
                max_amplification=float(defaults.get("max_amplification", 10)))


def load_spec(path: str) -> Spec:
    return parse_spec(_load_raw(path))


def _out_edges(spec: Spec) -> Dict[str, List[Call]]:
    out: Dict[str, List[Call]] = {n: [] for n in spec.services}
    for c in spec.calls:
        out.setdefault(c.src, []).append(c)
        out.setdefault(c.dst, [])
    return out


def find_cycle(spec: Spec) -> Optional[List[str]]:
    out = _out_edges(spec)
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in out}
    stack: List[str] = []

    def dfs(n: str) -> Optional[List[str]]:
        color[n] = GREY
        stack.append(n)
        for c in out[n]:
            if color[c.dst] == GREY:
                return stack[stack.index(c.dst):] + [c.dst]
            if color[c.dst] == WHITE:
                r = dfs(c.dst)
                if r:
                    return r
        stack.pop()
        color[n] = BLACK
        return None

    for n in sorted(out):
        if color[n] == WHITE:
            r = dfs(n)
            if r:
                return r
    return None


def topo_order(spec: Spec) -> List[str]:
    out = _out_edges(spec)
    indeg = {n: 0 for n in out}
    for c in spec.calls:
        indeg[c.dst] += 1
    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: List[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for c in out[n]:
            indeg[c.dst] -= 1
            if indeg[c.dst] == 0:
                ready.append(c.dst)
                ready.sort()
    return order


def entries(spec: Spec) -> List[str]:
    marked = [s.name for s in spec.services.values() if s.entry]
    if marked:
        return sorted(marked)
    has_in = {c.dst for c in spec.calls}
    return sorted(n for n in spec.services if n not in has_in)


def amplification(spec: Spec, entry: str) -> Dict[str, float]:
    """Worst-case requests reaching each service per one request at `entry`.

    Assumes every try fails downstream (the storm case), so each try of an
    edge re-drives the whole subtree below it.
    """
    out = _out_edges(spec)
    mult = {n: 0.0 for n in out}
    mult[entry] = 1.0
    for n in topo_order(spec):
        if mult[n] == 0:
            continue
        for c in out[n]:
            mult[c.dst] += mult[n] * c.attempts * c.fanout
    return {n: m for n, m in mult.items() if m > 0}


def worst_paths(spec: Spec, entry: str, limit: int = 2000) -> List[Tuple[List[str], List[Call]]]:
    out = _out_edges(spec)
    paths: List[Tuple[List[str], List[Call]]] = []

    def walk(n: str, nodes: List[str], edges: List[Call]) -> None:
        if len(paths) >= limit:
            return
        if not out[n]:
            paths.append((nodes, edges))
            return
        for c in out[n]:
            walk(c.dst, nodes + [c.dst], edges + [c])

    walk(entry, [entry], [])
    return paths


def analyze(spec: Spec) -> dict:
    findings: List[Finding] = []
    known = set(spec.services)
    strict_names = bool(known)  # a calls-only spec declares services implicitly
    for c in spec.calls:
        for n in (c.src, c.dst):
            if n not in known:
                if strict_names:
                    findings.append(Finding("RA008", "error", f"call references undeclared service {n!r}", f"{c.src}->{c.dst}"))
                spec.services[n] = Service(name=n)
                known.add(n)

    cyc = find_cycle(spec)
    if cyc:
        findings.append(Finding("RA007", "error",
                                "call cycle: amplification is unbounded under failure",
                                " -> ".join(cyc), cyc))
        return _report(spec, findings, {}, {})

    # per-edge checks
    for c in spec.calls:
        where = f"{c.src}->{c.dst}"
        caller = spec.services[c.src]
        callee = spec.services[c.dst]
        if c.attempts > 1 and not c.idempotent:
            findings.append(Finding("RA005", "error",
                                    f"retries a non-idempotent call ({c.attempts} tries); duplicates side effects",
                                    where))
        if c.attempts > 1 and c.backoff_ms <= 0:
            findings.append(Finding("RA006", "warn",
                                    "retries with zero backoff; failed tries hammer the callee immediately",
                                    where))
        elif c.attempts > 1 and not c.jitter:
            findings.append(Finding("RA006", "info",
                                    "retry backoff has no jitter; clients will retry in lockstep",
                                    where))
        wc = c.worst_case_ms()
        if wc is not None and caller.deadline_ms is not None and wc > caller.deadline_ms:
            fit = _tries_that_fit(c, caller.deadline_ms)
            findings.append(Finding("RA003", "warn",
                                    f"retry budget {wc:.0f}ms exceeds caller deadline {caller.deadline_ms:.0f}ms; "
                                    f"only {fit} of {c.attempts} tries can finish",
                                    where))
        if c.per_try_timeout_ms is not None and callee.deadline_ms is not None \
                and c.per_try_timeout_ms < callee.deadline_ms:
            sev = "warn" if c.attempts > 1 else "info"
            findings.append(Finding("RA004", sev,
                                    f"per-try timeout {c.per_try_timeout_ms:.0f}ms < callee deadline {callee.deadline_ms:.0f}ms; "
                                    "callee keeps working on abandoned tries" + (" while retries pile on" if c.attempts > 1 else ""),
                                    where))
        if c.per_try_timeout_ms is None and c.attempts > 1:
            findings.append(Finding("RA010", "warn",
                                    "retries without a per-try timeout; one hung try can eat the whole budget",
                                    where))

    amps: Dict[str, Dict[str, float]] = {}
    load: Dict[str, float] = {}
    for e in entries(spec):
        amps[e] = amplification(spec, e)
        rps = spec.services[e].rps
        for n, m in amps[e].items():
            if m > spec.max_amplification:
                findings.append(Finding("RA001", "error",
                                        f"worst-case amplification x{m:g} from {e} exceeds limit x{spec.max_amplification:g}",
                                        n))
            if rps is not None:
                load[n] = load.get(n, 0.0) + rps * m
        seen_layered = set()
        for nodes, edges in worst_paths(spec, e):
            retrying = [i for i, c in enumerate(edges) if c.attempts > 1]
            if len(retrying) >= 2:
                key = tuple(nodes[: retrying[-1] + 2])
                if key in seen_layered:
                    continue
                seen_layered.add(key)
                prod = 1
                for i in retrying:
                    prod *= edges[i].attempts
                findings.append(Finding("RA002", "warn",
                                        f"retries at {len(retrying)} layers on one path multiply to x{prod}; retry at one layer only",
                                        " -> ".join(key), list(key)))

    for n, rps in load.items():
        cap = spec.services[n].capacity_rps
        if cap is not None and rps > cap:
            findings.append(Finding("RA009", "error",
                                    f"worst-case load {rps:g} rps exceeds capacity {cap:g} rps", n))
    return _report(spec, findings, amps, load)


def _tries_that_fit(c: Call, deadline: float) -> int:
    t, delay, n = 0.0, float(c.backoff_ms), 0
    for i in range(c.attempts):
        if i > 0:
            d = delay if c.max_backoff_ms is None else min(delay, c.max_backoff_ms)
            t += d
            delay *= c.backoff_multiplier
        t += c.per_try_timeout_ms or 0
        if t <= deadline:
            n += 1
        else:
            break
    return n


def _report(spec: Spec, findings: List[Finding], amps, load) -> dict:
    findings.sort(key=lambda f: (-SEV_RANK[f.severity], f.code, f.where))
    edges = []
    for c in spec.calls:
        edges.append({"from": c.src, "to": c.dst, "attempts": c.attempts,
                      "worst_case_ms": c.worst_case_ms()})
    return {
        "max_amplification": spec.max_amplification,
        "amplification": amps,
        "worst_case_rps": load,
        "edges": edges,
        "findings": [asdict(f) for f in findings],
    }


def worst_severity(report: dict) -> Optional[str]:
    sevs = [f["severity"] for f in report["findings"]]
    if not sevs:
        return None
    return max(sevs, key=lambda s: SEV_RANK[s])
