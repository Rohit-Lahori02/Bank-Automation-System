"""Regenerate /evidence from real runs against the running mock console.

    python scripts/make_evidence.py [--artifact artifacts/member.read_savings_balance.v4.json]

Copies the discovery run referenced by the artifact's provenance, then replays the artifact
through every scenario the result contract distinguishes (success, other member, business
outcomes, hard failures, recoverable conditions, human handoff) and writes one evidence
directory per scenario plus summary.json. Replays never call a model.

The "human operator" in the handoff scenarios is a second Playwright client attached to the
live browser over CDP - the same mechanism a person or a remote console uses; it is scripted
here so the evidence can be regenerated unattended.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from cua.artifact import Expectation, Step, TextVisible
from cua.artifact.schema import Capability
from cua.handoff import ConsoleServer, HandoffController, ReplayHandoff
from cua.policy import DEFAULT_SENSITIVE_FIELDS, Policy, PolicyEngine, Redactor
from cua.replay import ReplayConfig, ReplayEngine
from cua.surface import BrowserSurface, RoleNameStrategy, SessionControl, Target

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
KEEP_TRACES = {"discovery", "replay_success", "replay_handoff_resumed"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def chaos(base: str, **flags) -> None:
    import httpx

    httpx.post(f"{base}/__chaos/reset")
    if flags:
        httpx.post(f"{base}/__chaos", json=flags)


def secrets_from_env() -> dict[str, str]:
    return {"app.username": os.getenv("CUA_SECRET_APP_USERNAME") or os.getenv("MOCK_APP_USERNAME") or "teller01",
            "app.password": os.getenv("CUA_SECRET_APP_PASSWORD") or os.getenv("MOCK_APP_PASSWORD") or "Pa55word!"}


def risky_variant(cap: Capability) -> Capability:
    """The recorded capability plus one irreversible step, to exercise the handoff path."""
    risky = cap.model_copy(deep=True)
    risky.steps.append(Step(id="close_it", action="click", description="Open the close-account confirmation (irreversible path)",
                            target=Target(description='link "Close Account"', role="link", strategies=[
                                RoleNameStrategy(role="link", name="Close Account", frame='iframe[name="acctframe"]',
                                                 rationale="first Close Account link in the accounts grid")]),
                            expect=Expectation(description="confirmation page", detect=TextVisible(text="Confirm Close"))))
    risky.checkpoint.detect = TextVisible(text="Confirm Close")
    risky.checkpoint.description = 'goal state: close-account confirmation screen ("Confirm Close")'
    risky.version = cap.version
    risky.provenance.notes += "; evidence variant: appended irreversible step close_it to exercise handoff"
    return risky


def operator(controller: HandoffController, act, decision: str):
    """Scripted human: attach to the live browser over CDP, act, decide."""
    def run() -> None:
        while not controller.pending():
            time.sleep(0.05)
        request = controller.pending()[0]
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(request.session_url)
            page = browser.contexts[0].pages[0]
            act(page)
            time.sleep(1.0)
        controller.decide(request.id, decision, token=request.control_token)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", default="artifacts/member.read_savings_balance.v4.json")
    parser.add_argument("--base", default=f"http://{os.getenv('MOCK_APP_HOST', '127.0.0.1')}:{os.getenv('MOCK_APP_PORT', '8000')}")
    args = parser.parse_args()

    cap = Capability.model_validate_json((ROOT / args.artifact).read_text(encoding="utf-8"))
    policy = PolicyEngine(Policy.load(ROOT / "policy.yaml"))
    secrets = secrets_from_env()
    redactor = Redactor(list(secrets.values()), sensitive_field_patterns=policy.policy.sensitive_field_patterns or DEFAULT_SENSITIVE_FIELDS)

    for old in EVIDENCE.iterdir() if EVIDENCE.exists() else []:
        if old.name != "README.md":
            shutil.rmtree(old) if old.is_dir() else old.unlink()
    (EVIDENCE / "artifact").mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "artifact" / "member.read_savings_balance.json").write_text(
        cap.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    (EVIDENCE / "artifact" / "member.read_savings_balance.describe.txt").write_text(cap.describe(), encoding="utf-8")

    # ---- discovery run (copied from the run the artifact came from) ---------------------------
    run_dir = ROOT / "runs" / cap.provenance.discovery_run_id
    if not run_dir.exists():
        print(f"discovery run {run_dir} not found; run `cua discover` first", file=sys.stderr)
        return 1
    shutil.copytree(run_dir, EVIDENCE / "discovery")
    summary: dict[str, dict] = {"discovery": json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))}

    # ---- replay scenarios -----------------------------------------------------------------------
    scenarios = [
        ("replay_success", cap, {"member_id": "12345"}, {}, None),
        ("replay_other_member", cap, {"member_id": "10001"}, {}, None),
        ("replay_not_found", cap, {"member_id": "99999"}, {}, None),
        ("replay_permission_denied", cap, {"member_id": "40403"}, {}, None),
        ("replay_invalid_input", cap, {"member_id": "12ab"}, {}, None),
        ("replay_app_error", cap, {"member_id": "50500"}, {}, None),
        ("replay_maintenance_dialog", cap, {"member_id": "12345"}, {"maintenance_dialog": True}, None),
        ("replay_session_expired", cap, {"member_id": "12345"}, {}, "s04_click"),
    ]
    for name, capability, inputs, flags, expire_at in scenarios:
        chaos(args.base, **flags)
        import httpx

        def before(step_id: str, _base=args.base, _at=expire_at) -> None:
            if _at and step_id == _at:
                httpx.post(f"{_base}/__chaos", json={"expire_session": True})

        with BrowserSurface(headless=True, trace_dir=EVIDENCE / name) as surface:
            engine = ReplayEngine(surface=surface, policy=policy, redactor=redactor, evidence_root=EVIDENCE,
                                  config=ReplayConfig(before_step=before))
            result = engine.replay(capability, inputs, secrets, run_id=name)
        summary[name] = json.loads(result.to_json())
        print(f"{name:28s} {result.one_line()}")
    chaos(args.base)

    # ---- handoff scenarios (scripted operator over CDP) ---------------------------------------
    risky = risky_variant(cap)

    def clicks_close_account(page) -> None:
        page.frame_locator('iframe[name="acctframe"]').get_by_role("link", name="Close Account").first.click()
        page.wait_for_url("**/close")

    for name, act, decision in [("replay_handoff_resumed", clicks_close_account, "resumed"),
                                ("replay_handoff_aborted", lambda page: None, "aborted")]:
        control = SessionControl()
        with BrowserSurface(headless=True, trace_dir=EVIDENCE / name, control=control, cdp_port=free_port()) as surface:
            controller = HandoffController(surface=surface, control=control, redactor=redactor, timeout_s=120, poll_s=0.2)
            thread = operator(controller, act, decision)
            engine = ReplayEngine(surface=surface, policy=policy, redactor=redactor, evidence_root=EVIDENCE,
                                  escalation_handler=ReplayHandoff(controller))
            result = engine.replay(risky, {"member_id": "12345"}, secrets, run_id=name)
            thread.join(30)
        summary[name] = json.loads(result.to_json())
        print(f"{name:28s} {result.one_line()}")

    # ---- operator console screenshots -----------------------------------------------------------
    console_dir = EVIDENCE / "operator_console"
    console_dir.mkdir()
    control = SessionControl()
    with BrowserSurface(headless=True, control=control, cdp_port=free_port()) as surface:
        surface.navigate(f"{args.base}/login")
        controller = HandoffController(surface=surface, control=control, redactor=redactor, timeout_s=60, poll_s=0.2)
        server = ConsoleServer(controller, port=free_port()).start()
        shots: dict = {}

        def photographer() -> None:
            while not controller.pending():
                time.sleep(0.05)
            request = controller.pending()[0]
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1200, "height": 900})
                page.goto(f"{server.url}/")
                page.screenshot(path=console_dir / "index.png")
                page.goto(f"{server.url}/interventions/{request.id}")
                page.wait_for_timeout(300)
                page.screenshot(path=console_dir / "intervention.png", full_page=True)
                browser.close()
            controller.decide(request.id, "aborted", token=request.control_token)
            shots["done"] = True

        threading.Thread(target=photographer, daemon=True).start()
        engine = ReplayEngine(surface=surface, policy=policy, redactor=redactor, evidence_root=EVIDENCE / "operator_console",
                              escalation_handler=ReplayHandoff(controller))
        engine.replay(risky, {"member_id": "12345"}, secrets, run_id="console_demo")
        server.stop()
    shutil.rmtree(EVIDENCE / "operator_console" / "console_demo", ignore_errors=True)

    # ---- trim + summary ---------------------------------------------------------------------------
    for d in EVIDENCE.iterdir():
        if d.is_dir() and d.name not in KEEP_TRACES:
            (d / "trace.zip").unlink(missing_ok=True)
    (EVIDENCE / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    # paths: repo-relative, forward slashes (no machine-specific paths in committed evidence)
    import re

    absolute = [str(EVIDENCE), str(EVIDENCE).replace("\\", "\\\\"), str(EVIDENCE).replace("\\", "/")]
    token = re.compile(r"evidence(?:\\\\|\\|/)((?:[\w.-]+(?:\\\\|\\|/))*[\w.-]+)")
    for path in list(EVIDENCE.rglob("*.json")) + list(EVIDENCE.rglob("*.jsonl")):
        text = path.read_text(encoding="utf-8")
        for prefix in absolute:
            text = text.replace(prefix, "evidence")
        text = token.sub(lambda m: "evidence/" + re.sub(r"\\\\|\\|/", "/", m.group(1)), text)
        path.write_text(text, encoding="utf-8")
    lines = ["| scenario | status | detail |", "|---|---|---|"]
    for name, res in summary.items():
        if name == "discovery":
            lines.append(f"| discovery | {res['status']} | {res['steps_executed']} steps, {res['llm_calls']} model calls, "
                         f"{res['input_tokens'] + res['output_tokens']} tokens, {res['model']} |")
            continue
        detail = res.get("outputs") if res["status"] == "success" else (
            res.get("outcome_code") if res["status"] == "business_outcome" else
            (res["failure"]["code"] + " at " + str(res["failure"]["step_id"]) if res.get("failure") else
             ("escalated at " + str(res["escalation"]["step_id"]) if res.get("escalation") else "")))
        if res.get("interventions"):
            iv = res["interventions"][0]
            detail = f"{detail}; handoff {iv['kind']} -> {iv['decision']}, {iv.get('human_actions', 0)} human actions"
        lines.append(f"| {name} | {res['status']} | {detail} |")
    (EVIDENCE / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
