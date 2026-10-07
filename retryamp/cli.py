from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import SEV_RANK, SpecError, analyze, load_spec, worst_severity


def render_text(report: dict) -> str:
    lines = []
    for entry, amp in sorted(report["amplification"].items()):
        lines.append(f"entry {entry}: worst-case requests per 1 incoming request")
        for n, m in sorted(amp.items(), key=lambda kv: (-kv[1], kv[0])):
            flag = "  <-- over limit" if m > report["max_amplification"] else ""
            lines.append(f"  {n:<24} x{m:g}{flag}")
    if report["worst_case_rps"]:
        lines.append("worst-case load (all entries, every try failing):")
        for n, r in sorted(report["worst_case_rps"].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {n:<24} {r:g} rps")
    lines.append("")
    if not report["findings"]:
        lines.append("no findings")
    for f in report["findings"]:
        lines.append(f"[{f['severity'].upper():5}] {f['code']} {f['where']}: {f['message']}")
    counts = {}
    for f in report["findings"]:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    lines.append("")
    lines.append("summary: " + ", ".join(f"{counts.get(s, 0)} {s}" for s in ("error", "warn", "info")))
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="retryamp",
                                description="Static retry-amplification and timeout-budget auditor for service call graphs.")
    p.add_argument("spec", help="call graph spec (.toml or .json)")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--max-amp", type=float, help="override max allowed amplification")
    p.add_argument("--fail-on", choices=("info", "warn", "error", "never"), default="error",
                   help="exit 1 when a finding at or above this severity exists (default: error)")
    p.add_argument("--version", action="version", version=f"retryamp {__version__}")
    a = p.parse_args(argv)
    try:
        spec = load_spec(a.spec)
    except (OSError, SpecError, ValueError) as e:
        print(f"retryamp: {e}", file=sys.stderr)
        return 2
    if a.max_amp is not None:
        spec.max_amplification = a.max_amp
    report = analyze(spec)
    print(json.dumps(report, indent=2) if a.format == "json" else render_text(report))
    worst = worst_severity(report)
    if a.fail_on != "never" and worst is not None and SEV_RANK[worst] >= SEV_RANK[a.fail_on]:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
