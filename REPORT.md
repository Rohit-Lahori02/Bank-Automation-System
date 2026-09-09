# Design write-up

## 1. Architecture

The system is one Python process with five layers and two seams. The **surface** layer
perceives and acts on a live UI. The **agent** layer runs an LLM through an observe → decide →
act loop over that surface, once, to discover a flow. The **artifact** layer turns the
successful run into a typed capability. The **replay** layer executes a capability with no model
in the loop. The **handoff** layer cedes the live session to a human and takes it back. A
**policy** engine sits in front of every action in both the agent and the replay, and a
redactor sits in front of every byte that reaches disk.

The target is a mock legacy credit-union console I built (FastAPI + Jinja): table-based layout,
no test IDs, labels not associated with inputs, an `<iframe>` for the accounts panel, and a
multi-step flow (sign on → member inquiry → profile → open sub-account → review → confirm). It
has a fault-injection endpoint so every runtime condition in the assignment can be produced on
demand: not found, permission denied, validation error, session expiry, slow load, maintenance
dialog, application error. This was the single highest-leverage decision: it costs nothing to
run, has no terms-of-service or PII risk, and makes every error path a deterministic test
instead of a hope.

Key decisions and trade-offs:

- **Text perception, not screenshots.** An injected walker produces a numbered list of visible
  elements with an inferred role, a human-visible name, a value and a bounding box, per frame.
  It is roughly an order of magnitude cheaper per step than an image, reads values exactly, and
  the *same structure* can be produced from a desktop accessibility API. Screenshots are taken
  only on failure and escalation, where they are evidence.
- **A JSON action protocol instead of provider-native tool calling.** The agent loop is
  identical on the free NVIDIA NIM model used for development and on Claude. The trade-off is
  a parse-and-retry step; it fired zero times in the recorded run.
- **The loop enforces, the model proposes.** Every proposed action passes the policy engine
  before it touches the surface; blocked, held, malformed and unknown-ref actions are returned to
  the model as step results. Only the two most recent screens stay verbatim in context, so cost is
  flat in run length (the two recorded runs: 9 and 15 steps, 24k and 42k tokens, on a free tier).
- **Two capabilities recorded, both replayed.** *Read savings balance* (search → detail → extract)
  and *open a sub-account* (form with a `select`, review, irreversible Confirm, extract the
  confirmation number). The second one is where the safety and handoff model shows up in a
  natural recording: the policy held the Confirm click during discovery, an operator approved it,
  and the recorded step carries `risk: risky`, so every replay escalates there by itself.
- **Single process, no queues.** Discovery is rare and human-paced; replay is a sub-minute
  sequential job. The abstractions (capability store, run evidence, handoff queue) are the units
  a service would later own; nothing in the code assumes they are in-process.

## 2. Artifact schema

A capability is a contract, not a transcript (`cua/artifact/schema.py`, strict Pydantic, every
cross-reference validated at load):

- **`inputs`** — typed (`string | integer | number | boolean | money`), with pattern, example and
  a `sensitive` flag; bound and coerced before the UI is touched, so a bad member number is
  rejected as `INVALID_INPUT` without a browser.
- **`outputs`** — typed, each bound to the `extract` step that produces it; parsed on return
  (`"$5,432.10"` → `Decimal("5432.10")`).
- **`secrets`** — names only (`app.password`). Steps reference `{{secrets.app.password}}`; the
  value is supplied at replay and the store refuses to persist any file containing a known secret.
- **`steps`** — ordered; each has an action, a `Target`, a template value, a risk class, and an
  optional `expect` postcondition (a detector with a timeout). A `Target` carries a *ranked chain
  of locator strategies, each with a rationale*: `role_name` (accessible role + name), `text`,
  `label_text` (our inference for unlabeled legacy inputs: adjacent table cell, nearest text
  left/above), `anchor_relative` (nearest element in a direction from an anchor text — also how
  a value next to its label, such as a confirmation number, is re-read without depending on the
  value itself), `table_cell` (row anchor + column header, resolved geometrically), `css`, `bbox`.
  Replay records which one resolved.
- **`conditions`** — the app's failure vocabulary, each classified as `business_outcome` (with a
  code), `recoverable` (with a handler: dismiss-click, bounded retry, re-login sub-flow,
  escalate) or `hard_failure` (with a code).
- **`checkpoint`**, **`provenance`** (run id, provider, model, redaction, what the recorder had to
  drop), **`version`**, **`status`** (`draft | approved | deprecated`).

Why this shape: the reviewer question is "what does it need, what does it return, what can go
wrong, and how do we know it worked" — those are the top-level keys. The recorder derives it from
the run: literal input values become templates in values, URLs *and locator anchors*
(`{{inputs.member_id}}-S01`); the model's own `expect` hints become postconditions only when they
held on the live surface; dialog dismissals become conditions rather than steps; sign-on becomes
the re-login sub-flow; the model's checkpoint sentence is verified against the final screen and
reduced to a visible fragment or dropped (noted in provenance). Conditions belong to an **app
profile**, not the capability, which is what lets them be shared across every capability and
every tenant on that product.

## 3. Determinism & error handling

Replay (`cua/replay/engine.py`) per step: policy gate → scan the screen for declared conditions →
resolve the target through the strategy chain → act → poll the expectation while still
scanning for conditions → scan again. Then verify the checkpoint and return outputs. Nothing
guesses: every branch is either declared in the artifact or an explicit failure.

Determinism comes from three things. **Locators** resolve semantically first and structurally
last; the value-independent `table_cell` strategy is what makes an extracted balance
re-locatable for a different member. **Waiting** is postcondition-driven (a detector with a
timeout), never fixed sleeps. **Checkpoints** assert the state was reached; a click that "worked"
is never assumed.

The result contract has four terminal states and the exit code follows it:

| status | meaning | carries |
|---|---|---|
| `success` | checkpoint held | typed outputs, per-step strategy provenance |
| `business_outcome` | legitimate non-happy answer (`MEMBER_NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_INPUT`) | outcome code + message |
| `failed` | `APP_ERROR`, `AUTH_FAILED`, `TARGET_NOT_FOUND`, `EXPECTATION_TIMEOUT`, `POLICY_DENIED`, `CHECKPOINT_FAILED`, … | step id, expected vs observed, strategies tried, screenshot |
| `escalated` | a human must decide | step, reason, screenshot, screen listing |

Recoverable conditions never surface as results: a maintenance dialog is dismissed; an expired
session runs the recorded re-login and the step is re-executed (a weak expectation is never
trusted to have been reached by the recovery alone); a slow load gets a bounded retry, only for
safe steps. The condition being recovered is suppressed while its handler runs, so re-login
cannot recurse. The evidence folder contains one replay for every row of that table.

UI drift is secondary in this environment and is handled by the strategy chain: styling and DOM
restructuring fall through `role_name` → `label_text` → `anchor_relative` → `css` → `bbox`, and the
recorded strategy index in each replay is the drift signal (a capability that starts resolving
on `bbox` needs re-recording before it breaks).

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `cua/surface/base.py`: `snapshot()` returns the same
`Snapshot` structure (elements with role, name, value, bbox, frame) whatever produced it, and
`resolve(Target)` consumes the same strategy chain. The browser implementation is Playwright plus
the frame-aware walker; a legacy server-rendered app is *already* the tested case (framesets are
frames; the walker infers names from layout, not markup). A desktop app would implement the same
protocol over UI Automation / AT-SPI: roles and names map directly, `anchor_relative` and
`table_cell` are geometric, and `bbox` is universal. A pure screenshot surface would keep
`bbox` plus a grounding model for `role_name`. The artifact never mentions the DOM; nothing above
the seam changes.

**Multi-tenant reuse — built and demonstrated (stretch goal).** A capability's `target` names
the vendor `app` and a `variant`. One capability is recorded on the base variant; the app profile
(`cua/artifact/profiles.py`) holds the shared failure vocabulary and login sub-flow; a per-tenant
**overlay** (`cua/artifact/overlay.py`) is a small reviewed diff applied at load: entry URL,
relabelings applied to every locator, expectation and detector, per-step strategy overrides or
replacements, extra or removed conditions. The result is re-validated as a full capability. The
mock console has a second tenant (Lakeshore: relabeled lookup, new theme, a Branch selector that
shifts the layout). Evidence (`evidence/tenant_*`): the base recording, untouched, *degrades
gracefully* on it - it succeeds, but five of eight steps resolve on structural fallbacks and the
result's `drift` list names them; with a six-line overlay every step is back on its first-choice
semantic strategy and `drift` is empty. **Drift detection** is therefore built in: every replay
result carries the steps that resolved below strategy index 0. A tenant whose steps start
resolving on `css` or `bbox` still works today and is flagged for an overlay or re-recording
before it breaks. Not built: a fleet-level drift dashboard and `status` gating per variant; both
are additive over the per-replay provenance that already exists.

## 5. Escalation & handoff

**Detecting "stuck"** has three sources: a step whose target matches the risky policy
(`Confirm`, `Close Account`, …) or is marked irreversible; a state failure the artifact has no
answer for (`TARGET_NOT_FOUND`, `EXPECTATION_TIMEOUT`, `CHECKPOINT_FAILED`, recovery loops); and
the discovery model calling `stuck`. Each raises an intervention request with the goal or
capability, step id, reason, URL, screenshot and screen listing. The risky path is the same in
discovery and replay: in the sub-account recording the model's Confirm click was held, approved
by the operator, executed, and recorded as a risky step, which is why replays of that capability
pause at Confirm without anyone having to annotate the artifact.

**Control transfer** (`cua/handoff/controller.py`) is a token held by exactly one party:
`automation → paused → human → automation`. Every automated action asserts that automation holds
the token, so nothing acts behind the operator. The human operates the **same live browser** —
it runs headed with a Chrome DevTools endpoint, so a person uses the window on the machine and a
remote operator client can attach to the identical session (the tests do exactly that with a
second Playwright client). While a human holds control, an in-page listener captures clicks,
inputs (password values masked before leaving the page) and submits into the run's evidence
next to the automation's own steps.

**Handing back** is a decision carrying the token: *resumed* (the human did the work — replay
verifies the step's expected state on screen before continuing; a safe step is re-run if it
does not hold, a risky one fails as `HANDOFF_STATE_MISMATCH`), *approved* (automation performs
the held step), *aborted*/timeout (the run ends `escalated`). One shortcut, because operators
expect it: when a human action has been captured and the held step's own postcondition then
holds on screen, the handoff resolves itself as *resumed* — the screen is the proof, and it is
the same check replay would make anyway. Anything less waits for an explicit decision. In
discovery the model is told what the human did and continues from a fresh screen.

**The operator surface** is a minimal in-process web console (list, claim, context, live captured
actions, three decisions) plus a shell command. What I mocked and why: there is no co-browsing
stream, queueing, operator identity or SLA. The seam to add them is the intervention request
(already serialised) plus the CDP endpoint (screencast for a remote view, input injection for a
remote hand); the control model and the evidence trail would not change.

## 6. Safety

`policy.yaml` is an allowlist: origins, path patterns (deny wins), action types. Link clicks are
checked against where the link leads. Risk is classified per action from the target's name
(`confirm|submit|transfer|approve|close account|delete`) or an `irreversible` flag, and the mode is
configurable: `block`, `escalate` (default — reuses the handoff path, so the safety demo and the
handoff demo are the same run), or `flag`. The discovery agent is told the same rule in its
prompt, but the enforcement is in code: a held action is returned to the model as a result, and
repeated attempts end the run.

Data handling: credentials are never shown to the model (it types placeholders); every log,
transcript, artifact and intervention file passes the redactor (known secret values longest-first,
SSN, Luhn-valid card numbers, API keys and bearer tokens, e-mail, phone; sensitive keys masked);
the artifact store refuses to write a file containing a secret value; browser storage state is
never saved. Limits: the redactor is pattern-based, so novel identifier formats need adding per
app; the model still sees business identifiers on screen (member numbers, names), which is
inherent to the task; the allowlist is per deployment, not per capability.

## 7. Cuts

Cut deliberately: a fleet-level drift dashboard (the per-replay drift signal exists); a desktop
surface (seam only); the co-browsing operator console (minimal console + CDP seam); the
agent-facing capability catalogue endpoint; a CI workflow for the 126 tests; input `pattern`s in
recorded artifacts are not inferred (the hand-authored reference shows the intent); the discovery
runs were made on a free NVIDIA NIM model rather than Claude — the adapter exists and the run is
one config change.

Stretch goal taken: canonicalization / cross-tenant reuse (Section 4). Next, in order: a
multi-run stability score feeding `status: approved` and gating unattended replay; a
Claude-recorded run for comparison of expectation quality; `cua serve` exposing approved
capabilities as callable tools with typed arguments.
