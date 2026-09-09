# Bank Automation System — Computer-Use Automation for Legacy Back-Office UIs

An LLM discovers how to complete a task on a legacy banking UI **once**, the run is
recorded as a typed, versioned **capability artifact**, and that artifact is then
**replayed deterministically** (no model in the loop) with typed inputs, typed outputs,
explicit error handling, safety guardrails, and a human-in-the-loop handoff path.

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is
> how an AI agent invokes it in production.

Design write-up: [`REPORT.md`](REPORT.md) (added in the final phase).
Evidence from real runs: [`evidence/`](evidence/).

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Repo scaffold, config, policy file | done |
| 1 | Mock legacy credit-union console (the automation target) with fault injection | done |
| 2 | Surface layer: Playwright driver, accessibility-style snapshot, locator strategies | done |
| 3 | Artifact schema, policy engine, redaction | done |
| 4 | LLM-driven discovery loop + recorder | done |
| 5 | Deterministic replay engine + CLI | done |
| 6 | Escalation and human handoff | planned |
| 7 | Evidence, REPORT.md, final run | planned |

## Setup

Requires Python 3.11+ (developed on 3.12) and a Chromium download for Playwright.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
pip install -e ".[dev]"
```

```bash
playwright install chromium
```

Copy `.env.example` to `.env` and fill in what you need. Nothing in phases 0–3 needs an
API key. Discovery (phase 4) needs either `NVIDIA_API_KEY` (free NIM tier, used for
development) or `ANTHROPIC_API_KEY` (used for the final evidence run). Replay never calls a
model.

## Run the mock target app

```bash
cua serve-app
```

Then open http://127.0.0.1:8000 and sign on with the demo operator credentials from
`.env.example` (`teller01` / `Pa55word!`). These are fake and exist only so the automation
layer has a login step to record and redact.

### What the mock app is

"Harbor Federal Credit Union — Member Servicing Console", a deliberately legacy,
server-rendered app: table layout, `<font>` tags, no test IDs, labels not associated with
inputs, and an `<iframe>` for the accounts panel. Flow:

1. Sign on
2. Member Inquiry → search by 5-digit member number
3. Member Profile (with accounts iframe showing balances)
4. Open Sub-Account → form → Review → **Confirm** (irreversible)

Seeded members (all synthetic): `10001`, `10002`, `12345`, `20077`, `31415`.

### Built-in runtime conditions

| Trigger | Condition | Where it shows |
|---|---|---|
| search `99999` | business outcome: not found | "No member found for number 99999." |
| search `40403` | permission denied | "Access Denied … SEC-0403" (HTTP 403) |
| open `/members/50500` | application error | "Application Error … CLK-0500" (HTTP 500) |
| search `12ab` | validation error | "VAL-1001" |
| empty nickname / deposit < $25 | validation error | "VAL-2002" / "VAL-2004" |

### Fault injection (chaos)

`POST /__chaos` with JSON or form fields. One-shot flags clear after firing unless
`sticky` is true. `POST /__chaos/reset` clears everything. `GET /__chaos` shows state.

| Flag | Effect |
|---|---|
| `slow_ms` | delay every page by N ms |
| `expire_session` | next authenticated request is bounced to `/login?reason=expired` |
| `maintenance_dialog` | next full page render shows a modal "System Maintenance Notice" with an OK button |
| `app_error` | next member profile load returns the CLK-0500 error page |
| `sticky` | keep one-shot flags armed |

Example:

```bash
curl -X POST http://127.0.0.1:8000/__chaos -H "Content-Type: application/json" -d "{\"expire_session\": true}"
```

## Tests

```bash
pytest
```

## Repository layout

```
mock_app/        FastAPI + Jinja mock legacy console, chaos controls, seed data
cua/
  llm/           provider-agnostic LLM client (OpenAI-compatible / Anthropic)
  surface/       Playwright driver, snapshot walker, locator strategies, session control
  artifact/      capability schema, recorder, store
  policy/        allowlist + risk engine, redaction
  agent/         discovery loop and prompts
  replay/        deterministic replay engine, conditions, result contract
  handoff/       escalation controller, operator console, human action capture
  evidence/      structured run logs and tracing
  cli.py         `cua` command-line entry point
tests/
evidence/        committed sample artifact, logs, screenshots, traces
policy.yaml      the safety policy the agent and replay engine enforce
```

## Discovery (phase 4)

Discovery runs an LLM in an observe → decide → act loop against the live mock console,
enforces the policy before every action, logs evidence, and records the successful run as a
capability artifact.

**Model configuration** lives in `.env`:

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `openai_compat` (NVIDIA NIM or any OpenAI-compatible endpoint) or `anthropic` |
| `LLM_MODEL` | model id, e.g. `meta/llama-3.3-70b-instruct` or `claude-opus-5` |
| `LLM_BASE_URL` | for `openai_compat`; defaults to NVIDIA NIM |
| `NVIDIA_API_KEY` / `ANTHROPIC_API_KEY` | credentials for the chosen provider |
| `LLM_EFFORT` | Anthropic only: `low` / `medium` / `high` (default `medium`) |

**Secrets** the agent may use are referenced by name (`app.username`, `app.password`) and
resolved from `CUA_SECRET_APP_USERNAME` / `CUA_SECRET_APP_PASSWORD`. For the bundled mock
console, `MOCK_APP_USERNAME` / `MOCK_APP_PASSWORD` are accepted as fallbacks. The model only
ever sees the placeholder `{{secrets.app.password}}`; the value is substituted at the surface
and scrubbed from all evidence.

With the mock app running in another terminal:

```bash
cua discover --goal "Look up member 12345 and read their current savings balance" --input member_id=12345 --capability-id member.read_savings_balance --name "Read member savings balance"
```

Outputs:

- `artifacts/member.read_savings_balance.v1.json` — the capability (see `cua describe <file>`)
- `runs/<run_id>/log.jsonl` — every decision, policy verdict, and action, redacted
- `runs/<run_id>/transcript.json` — what the model saw and replied at each step, redacted
- `runs/<run_id>/summary.json`, `final.png`, `trace.zip` — final state, screenshot, Playwright trace

The agent speaks a plain JSON action protocol (see `cua/agent/prompts.py`) rather than
provider-native tool calling, so the same loop runs on free models during development and on
Claude for the final evidence run.

## Replay (phase 5)

Replay runs a saved capability with **no model in the loop**. For each step it applies the
policy gate, scans the screen for declared conditions, resolves the target through its
strategy chain (recording which strategy hit), acts, and waits for the step's expectation
while still watching for conditions. It ends by verifying the checkpoint and returning the
declared outputs.

```bash
cua replay artifacts/member.read_savings_balance.v1.json --input member_id=12345
```

The result contract has four terminal states, and the exit code follows it:

| Status | Exit | Meaning | Carries |
|---|---|---|---|
| `success` | 0 | checkpoint held | typed `outputs` |
| `business_outcome` | 10 | the app gave a legitimate non-happy answer (`MEMBER_NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_INPUT`) | `outcome_code`, message |
| `failed` | 20 | something broke (`APP_ERROR`, `AUTH_FAILED`, `TARGET_NOT_FOUND`, `EXPECTATION_TIMEOUT`, `POLICY_DENIED`, `CHECKPOINT_FAILED`, ...) | step id, expected vs observed, screenshot, strategies tried |
| `escalated` | 30 | a human must decide (risky step, unrecoverable state) | step id, reason, screenshot, screen listing |

Recoverable conditions never surface as results: a maintenance dialog is dismissed, an
expired session triggers the recorded re-login sub-flow and the step is redone, a slow load
gets a bounded retry. Every replay writes `runs/<run_id>/log.jsonl`, `result.json`,
screenshots on outcome/failure/escalation, and a Playwright trace.

To exercise the error paths, inject faults into the mock console before replaying:

```bash
cua chaos --maintenance-dialog
```

```bash
cua chaos --expire-session
```

```bash
cua chaos --app-error
```

```bash
cua chaos --reset
```

## Demo path

```bash
cua discover --goal "Look up member 12345 and read their current savings balance" --input member_id=12345 --capability-id member.read_savings_balance
```

```bash
cua replay artifacts/member.read_savings_balance.v1.json --input member_id=12345
```

```bash
cua replay artifacts/member.read_savings_balance.v1.json --input member_id=99999
```
