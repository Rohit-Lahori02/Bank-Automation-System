# Evidence

Everything here comes from real runs against the mock console, regenerated with
`python scripts/make_evidence.py` while `cua serve-app` is running. `summary.md` is the
one-screen index; `summary.json` has the full structured results.

## The discovery run (`discovery/`)

A genuine LLM-driven run: NVIDIA NIM, `nvidia/nemotron-3-super-120b-a12b`, goal *"Look up
member 12345 and read their current savings balance"*.

| file | what it is |
|---|---|
| `log.jsonl` | every decision (with the model's reasoning), policy verdict and action, redacted |
| `transcript.json` | what the model saw (the screen listing) and replied at every step, redacted |
| `summary.json` | status, outputs, token usage, per-step results |
| `final.png` | the screen when the model called `done` |
| `trace.zip` | Playwright trace of the whole run (`playwright show-trace`) |

## The artifact (`artifact/`)

`member.read_savings_balance.json` is the capability the recorder produced from that run;
`member.read_savings_balance.describe.txt` is the reviewer view (`cua describe`). Note the
parameterised row anchor `{{inputs.member_id}}-S01` in the balance step, the inferred
expectation on the Search step, and the provenance note about the model's checkpoint text.

## Replays (`replay_*/`)

Each directory has `log.jsonl`, `result.json`, a screenshot at the terminal state and, for
`replay_success` and `replay_handoff_resumed`, the Playwright trace. All replays used the
artifact above with no model involved.

| directory | what it shows |
|---|---|
| `replay_success` | the recorded member: `success`, balance 5432.10, strategy provenance per step |
| `replay_other_member` | a member the model never saw: `success`, balance 1240.50 — the capability generalises |
| `replay_not_found` | member 99999: `business_outcome MEMBER_NOT_FOUND` at the search step, not a failure |
| `replay_permission_denied` | member 40403: `business_outcome PERMISSION_DENIED` |
| `replay_invalid_input` | member "12ab": the recorded artifact declares no input pattern (the recorder does not infer one), so the value reaches the app and its validation error is reported as `business_outcome INVALID_INPUT`. The hand-authored reference capability declares `\d{5}` and rejects it as `failed INVALID_INPUT` before the browser is touched (see `tests/test_replay.py`) |
| `replay_app_error` | member 50500: `failed APP_ERROR` at the profile step with expected vs observed and screenshot |
| `replay_maintenance_dialog` | a modal notice injected mid-run: dismissed by the recorded handler, run succeeds |
| `replay_session_expired` | session expiry injected before the inquiry step: re-login sub-flow, step redone, run succeeds |
| `replay_handoff_resumed` | an irreversible step escalates; the operator performs it on the live session; `intervention.json` records the captured human actions; replay verifies and completes |
| `replay_handoff_aborted` | the operator declines: `escalated` with the full context |

The operator in the handoff runs was a second Playwright client attached to the live browser
over CDP — the same mechanism a person or a remote console uses — scripted so the evidence can
be regenerated unattended. `operator_console/` shows the console a person would use.

## Cross-tenant reuse (`tenant_*`)

The mock console has a second tenant variant, **Lakeshore Community Credit Union**: the same
CoreLink product on version 4.3 with the member lookup relabeled ("Member Lookup", "Member
No.", "Find", "Open", "Current Balance"), a green theme, and a Branch selector inserted above
the member number field so structural fallbacks shift too.

| directory | what it shows |
|---|---|
| `tenant_lakeshore_without_overlay` | the base recording pointed at the second tenant, untouched: it **degrades gracefully** - `success`, but `drift` lists 5 of 8 steps that resolved on structural fallbacks (`css#2`) because "Member Inquiry", "Member Number", "Search" and "View" no longer exist under those names |
| `tenant_lakeshore_with_overlay` | the same recording with `artifact/member.read_savings_balance.lakeshore.overlay.json` applied (six relabelings and an entry URL, no re-recording): `success`, every step on its first-choice strategy, `drift: []` |

That pair is the multi-tenant claim in one comparison: a recording with deep locator chains
survives a relabeled tenant on fallbacks, the `drift` list says exactly which steps are living
on borrowed time, and a few-line overlay puts them back on semantic locators. (A shallow,
hand-authored chain with only semantic strategies fails outright at the first relabeled control -
`tests/test_tenant.py` covers that case too.) The overlay is reviewed like any other artifact;
the recorded flow, the app's condition vocabulary and the input/output contract are shared
unchanged.

## Second capability: open a sub-account (`subaccount_*`)

A second genuine discovery run for the goal *"Open a new 'Savings - Holiday Club' sub-account
for member 12345 with the nickname 'Holiday Fund' and an initial deposit of 125.50, confirm it,
and read back the confirmation number"* — the multi-field form with a confirmation step from
the brief. Four typed inputs (member, product, nickname, deposit), a `select` step, and an
**irreversible Confirm**. During discovery the policy held the Confirm click and routed it to
an operator, who approved it with `cua resume --decision approved` (see
`subaccount_discovery/intervention.json` and the `handoff_*` events in its log). The recorder
therefore marked that step `risky`, so every replay escalates there on its own — no appended
step.

| directory | what it shows |
|---|---|
| `subaccount_discovery` | the run: 15 steps, the held Confirm, the approval, the extracted confirmation number |
| `artifact/member.open_subaccount.json` | the capability; note `{{inputs.product}}` on the select step and `[RISKY]` on Confirm |
| `subaccount_replay_success` | escalates at Confirm, operator approves, `success` with a fresh confirmation number |
| `subaccount_replay_other_member` | same for member 10001 |
| `subaccount_replay_validation_error` | deposit 10 (below the $25 minimum): `business_outcome VALIDATION_ERROR` at Continue, Confirm never reached |
| `subaccount_replay_escalation_aborted` | operator declines at Confirm: `escalated`, nothing was opened |
