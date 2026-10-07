# retryamp

Static retry-amplification and timeout-budget auditor for service call graphs.

Every team adds "just 3 retries" to its own client. Stack three of those layers and one failing database sees 27 requests for every user click, right when it is least able to cope. `retryamp` reads a small description of who calls whom, with what retry policy and timeouts, and tells you before an outage how bad the storm can get and which policies are wasting work.

No traffic, no tracing, no cluster access. It is a pure, offline, stdlib-only Python CLI that fits in CI next to your config.

## What's novel

Linters for retries usually look at one client at a time. `retryamp` reasons about the whole graph at once.

- **Worst-case amplification per service.** For each entry point it computes how many requests one incoming request can become at every downstream service when every try fails (sum over all paths of the product of attempts times fanout). Diamonds and fan-out are handled correctly.
- **Worst-case load against capacity.** Give entry points an `rps` and services a `capacity_rps`, and it tells you which ones a full retry storm would push past capacity.
- **Layered retries.** Flags any path that retries at two or more layers, the classic anti-pattern from the Google SRE book.
- **Timeout budget math.** Computes each edge's worst-case wall time (tries times per-try timeout plus capped exponential backoff) and reports how many of the configured tries can actually finish inside the caller's own deadline.
- **Abandoned work.** Flags per-try timeouts shorter than the callee's own deadline, where the callee keeps working on tries the caller already gave up on.
- **Unsafe policies.** Retrying non-idempotent calls, zero backoff, missing jitter, retries with no per-try timeout, and call cycles.

## Install

```bash
pip install git+https://github.com/rashedhasan090/retryamp
# or, from a clone
pip install .
```

Requires Python 3.11+ (uses the stdlib `tomllib`). No dependencies.

## Run

```bash
retryamp examples/checkout.toml            # human-readable report
retryamp graph.json --format json          # machine-readable
retryamp graph.toml --fail-on warn         # stricter CI gate
retryamp graph.toml --max-amp 5            # override the amplification limit
python -m retryamp graph.toml              # without installing the script
```

Exit codes are `0` for clean (or below `--fail-on`), `1` when a finding at or above `--fail-on` exists (default `error`), and `2` for a bad spec.

## Spec format

TOML or JSON with the same shape. `attempts` counts the first try, so `attempts = 3` means one try plus two retries.

```toml
[defaults]
max_amplification = 10

[services.edge]
entry = true          # optional; if no service is marked, nodes with no callers are entries
deadline_ms = 3000    # this service's own request deadline
rps = 500             # incoming load at an entry point

[services.ledger-db]
capacity_rps = 6000

[[calls]]
from = "edge"
to = "checkout"
attempts = 3
per_try_timeout_ms = 1000
backoff_ms = 100          # first backoff
backoff_multiplier = 2.0  # exponential growth (default 2)
max_backoff_ms = 1000     # optional cap
jitter = true
idempotent = true         # default true
fanout = 1                # calls per incoming request (batch splits, scatter-gather)
```

A calls-only spec is fine too; services are then declared implicitly.

## Demo

`examples/checkout.toml` is a checkout flow where each team picked its own retries.

```text
$ retryamp examples/checkout.toml
entry edge: worst-case requests per 1 incoming request
  ledger-db                x36  <-- over limit
  payments                 x9
  inventory                x6
  checkout                 x3
  edge                     x1
worst-case load (all entries, every try failing):
  ledger-db                18000 rps
  payments                 4500 rps
  inventory                3000 rps
  checkout                 1500 rps
  edge                     500 rps

[ERROR] RA001 ledger-db: worst-case amplification x36 from edge exceeds limit x10
[ERROR] RA005 checkout->payments: retries a non-idempotent call (3 tries); duplicates side effects
[ERROR] RA009 ledger-db: worst-case load 18000 rps exceeds capacity 6000 rps
[ERROR] RA009 payments: worst-case load 4500 rps exceeds capacity 4000 rps
[WARN ] RA002 edge -> checkout -> inventory: retries at 2 layers on one path multiply to x6; retry at one layer only
[WARN ] RA002 edge -> checkout -> payments -> ledger-db: retries at 3 layers on one path multiply to x36; retry at one layer only
[WARN ] RA003 checkout->payments: retry budget 4500ms exceeds caller deadline 2500ms; only 1 of 3 tries can finish
[WARN ] RA003 edge->checkout: retry budget 3300ms exceeds caller deadline 3000ms; only 2 of 3 tries can finish
[WARN ] RA004 checkout->inventory: per-try timeout 400ms < callee deadline 800ms; callee keeps working on abandoned tries while retries pile on
[WARN ] RA004 checkout->payments: per-try timeout 1500ms < callee deadline 2000ms; callee keeps working on abandoned tries while retries pile on
[WARN ] RA004 edge->checkout: per-try timeout 1000ms < callee deadline 2500ms; callee keeps working on abandoned tries while retries pile on
[WARN ] RA004 payments->ledger-db: per-try timeout 300ms < callee deadline 500ms; callee keeps working on abandoned tries while retries pile on
[WARN ] RA006 checkout->payments: retries with zero backoff; failed tries hammer the callee immediately
[INFO ] RA006 checkout->inventory: retry backoff has no jitter; clients will retry in lockstep

summary: 4 error, 9 warn, 1 info
```

`examples/fixed.toml` keeps retries at a single layer with backoff, jitter, and timeouts that fit inside each caller's deadline, and comes back clean.

## Checks

| Code | Severity | Meaning |
|------|----------|---------|
| RA001 | error | Worst-case amplification at a service exceeds the limit |
| RA002 | warn | Retries at two or more layers on one path |
| RA003 | warn | An edge's retry budget exceeds the caller's own deadline |
| RA004 | warn/info | Per-try timeout shorter than the callee's deadline (warn when retrying) |
| RA005 | error | Retrying a non-idempotent call |
| RA006 | warn/info | Zero backoff (warn) or no jitter (info) |
| RA007 | error | Call cycle, so amplification is unbounded |
| RA008 | error | Call references an undeclared service |
| RA009 | error | Worst-case load exceeds a service's capacity |
| RA010 | warn | Retries without a per-try timeout |

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

MIT
