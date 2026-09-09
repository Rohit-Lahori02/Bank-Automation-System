"""Command-line entry point for the computer-use automation system."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer(
    name="cua",
    help="Computer-use automation: discover, record, replay, and hand off.",
    no_args_is_help=True,
)

DEFAULT_ENTRY = f"http://{os.getenv('MOCK_APP_HOST', '127.0.0.1')}:{os.getenv('MOCK_APP_PORT', '8000')}/login"
CONSOLE_PORT = int(os.getenv("OPERATOR_CONSOLE_PORT", "8001"))
CDP_PORT = int(os.getenv("CUA_CDP_PORT", "9222"))


def _handoff_setup(mode: str, surface, control, redactor, timeout_s: float):
    """Wire a HandoffController (+ optional operator console) for a run. Returns (controller, console)."""
    from cua.handoff import ConsoleServer, HandoffController

    if mode == "none":
        return None, None
    console = None

    def announce(request) -> None:
        typer.echo("")
        typer.echo(f"*** INTERVENTION REQUIRED [{request.kind}] at {request.step_id}: {request.reason}")
        typer.echo("    The live browser window is now the operator's. Do the manual steps there, then decide:")
        if console:
            typer.echo(f"    operator console: {console.url}/interventions/{request.id}")
        typer.echo(f"    or from a shell:  cua resume \"{request.evidence_dir}\" --decision resumed|approved|aborted")
        if request.session_url:
            typer.echo(f"    remote operator client can attach via CDP: {request.session_url}")

    controller = HandoffController(surface=surface, control=control, redactor=redactor, timeout_s=timeout_s,
                                   on_escalate=announce)
    if mode == "console":
        console = ConsoleServer(controller, port=CONSOLE_PORT).start()
        typer.echo(f"operator console listening at {console.url}")
    return controller, console


def _parse_kv(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"expected name=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def load_secrets(names: list[str]) -> dict[str, str]:
    """Resolve secret references from the environment: app.password -> CUA_SECRET_APP_PASSWORD.

    For the bundled mock console, MOCK_APP_USERNAME / MOCK_APP_PASSWORD are accepted as
    fallbacks for app.username / app.password so the demo runs from .env.example alone.
    """
    fallbacks = {"app.username": ("MOCK_APP_USERNAME", "teller01"), "app.password": ("MOCK_APP_PASSWORD", "Pa55word!")}
    out: dict[str, str] = {}
    for name in names:
        env_key = "CUA_SECRET_" + name.upper().replace(".", "_")
        value = os.getenv(env_key)
        if not value and name in fallbacks:
            env_name, default = fallbacks[name]
            value = os.getenv(env_name) or default   # same demo defaults the mock console uses
        if not value:
            raise typer.BadParameter(f"secret '{name}' not found: set {env_key} in .env")
        out[name] = value
    return out


@app.command("serve-app")
def serve_app(
    host: str = typer.Option(None, help="Bind host (default: MOCK_APP_HOST or 127.0.0.1)"),
    port: int = typer.Option(None, help="Bind port (default: MOCK_APP_PORT or 8000)"),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (dev only)"),
    variant: str = typer.Option(None, help="Tenant variant of the product: harbor (default) or lakeshore"),
) -> None:
    """Run the mock legacy credit-union console (the automation target)."""
    from mock_app.__main__ import run

    run(host=host, port=port, reload=reload, variant=variant)


@app.command("discover")
def discover(
    goal: str = typer.Option(..., help="Natural-language goal for the agent"),
    inputs: list[str] = typer.Option([], "--input", "-i", help="Caller-supplied input, name=value (repeatable)"),
    entry_url: str = typer.Option(DEFAULT_ENTRY, help="Where the flow starts"),
    capability_id: str = typer.Option(..., help="Dotted id for the recorded capability, e.g. member.read_savings_balance"),
    name: str = typer.Option(None, help="Human name for the capability"),
    description: str = typer.Option(None, help="One-line description (defaults to the goal)"),
    app_id: str = typer.Option("corelink-member-servicing", "--app", help="Application profile id"),
    secret: list[str] = typer.Option(["app.username", "app.password"], help="Secret references the agent may use"),
    policy_path: Path = typer.Option(Path("policy.yaml"), "--policy", help="Safety policy file"),
    artifacts_dir: Path = typer.Option(Path("artifacts"), help="Where the capability JSON is written"),
    evidence_dir: Path = typer.Option(Path("runs"), help="Where run evidence (logs, screenshots, trace) goes"),
    headed: bool = typer.Option(None, "--headed/--headless", help="Show the browser (default: shown when handoff is on)"),
    max_steps: int = typer.Option(25, help="Step budget for the agent"),
    handoff: str = typer.Option("console", help="Human handoff: console (operator UI on :8001), file (cua resume), none"),
    handoff_timeout: float = typer.Option(600.0, help="Seconds to wait for an operator decision"),
    slow_mo: int = typer.Option(0, help="Milliseconds to pause between browser actions, to watch the run"),
    keep_open: bool = typer.Option(False, "--keep-open", help="Keep the browser open at the end until Enter is pressed"),
) -> None:
    """Run an LLM-driven discovery against the target and record a capability artifact."""
    from cua.agent.loop import DiscoveryAgent, DiscoveryConfig
    from cua.artifact.recorder import RecorderSpec, RecordingError, record_capability
    from cua.artifact.store import ArtifactStore
    from cua.evidence.logger import RunLogger
    from cua.handoff import DiscoveryHandoff
    from cua.llm.client import LLMError, client_from_env
    from cua.policy.engine import Policy, PolicyEngine
    from cua.policy.redaction import DEFAULT_SENSITIVE_FIELDS, Redactor
    from cua.surface.driver import BrowserSurface
    from cua.surface.session import SessionControl

    try:
        llm = client_from_env()
    except LLMError as exc:
        raise typer.BadParameter(str(exc))
    policy = PolicyEngine(Policy.load(policy_path))
    redactor = Redactor(sensitive_field_patterns=policy.policy.sensitive_field_patterns or DEFAULT_SENSITIVE_FIELDS)
    secrets = load_secrets(secret)
    for value in secrets.values():
        redactor.add_secret(value)
    input_values = _parse_kv(inputs)
    if headed is None:
        headed = handoff != "none"

    typer.echo(f"provider={llm.provider} model={llm.model}")
    typer.echo(f"goal: {goal}")
    run_id = DiscoveryAgent.new_run_id()
    control = SessionControl()
    surface = BrowserSurface(headless=not headed, trace_dir=evidence_dir / run_id, control=control,
                             cdp_port=CDP_PORT if handoff != "none" else None, slow_mo=slow_mo)
    surface.start()
    controller, console = _handoff_setup(handoff, surface, control, redactor, handoff_timeout)
    hooks = {}
    if controller:
        bridge = DiscoveryHandoff(controller, log_factory=lambda run: RunLogger(run.evidence_dir, redactor))
        hooks = {"stuck_handler": bridge.on_stuck, "risky_handler": bridge.on_risky}
    agent = DiscoveryAgent(llm=llm, surface=surface, policy=policy, redactor=redactor, evidence_root=evidence_dir,
                           config=DiscoveryConfig(max_steps=max_steps), **hooks)
    try:
        run = agent.run(goal, entry_url=entry_url, inputs=input_values, secrets=secrets, run_id=run_id)
        if keep_open and headed:
            typer.echo(f"status: {run.status}; browser left open; press Enter to close it")
            input()
    finally:
        surface.stop()
        if console:
            console.stop()

    typer.echo(f"status: {run.status} ({run.stop_reason})")
    typer.echo(f"steps: {len(run.actions)} total, {len(run.executed_actions)} executed; "
               f"model calls: {run.llm_calls}; tokens in/out: {run.input_tokens}/{run.output_tokens}")
    typer.echo(f"outputs: {run.outputs}")
    typer.echo(f"evidence: {run.evidence_dir}")
    if run.status != "success":
        raise typer.Exit(code=2)

    spec = RecorderSpec(capability_id=capability_id, name=name or capability_id, description=description or goal,
                        app=app_id)
    try:
        capability = record_capability(run, spec, policy=policy, redactor=redactor)
    except RecordingError as exc:
        typer.echo(f"recording failed: {exc}")
        raise typer.Exit(code=3)
    store = ArtifactStore(artifacts_dir)
    existing = [c for c in store.list() if c.id == capability.id]
    if existing:
        capability.version = max(c.version for c in existing) + 1
    path = store.save(capability, redactor=redactor)
    typer.echo(f"artifact: {path}")
    typer.echo(capability.describe())


@app.command("describe")
def describe(path: Path = typer.Argument(..., help="Capability JSON file")) -> None:
    """Print a reviewer-friendly description of a capability artifact."""
    from cua.artifact.schema import Capability

    typer.echo(Capability.model_validate_json(path.read_text(encoding="utf-8")).describe())


@app.command("replay")
def replay(
    artifact: Path = typer.Argument(..., help="Capability JSON file to replay"),
    inputs: list[str] = typer.Option([], "--input", "-i", help="Input value, name=value (repeatable)"),
    policy_path: Path = typer.Option(Path("policy.yaml"), "--policy", help="Safety policy file"),
    evidence_dir: Path = typer.Option(Path("runs"), help="Where run evidence goes"),
    headed: bool = typer.Option(None, "--headed/--headless", help="Show the browser (default: shown when handoff is on)"),
    json_out: bool = typer.Option(False, "--json", help="Print the full result JSON"),
    handoff: str = typer.Option("console", help="Human handoff: console (operator UI on :8001), file (cua resume), none"),
    handoff_timeout: float = typer.Option(600.0, help="Seconds to wait for an operator decision"),
    slow_mo: int = typer.Option(0, help="Milliseconds to pause between browser actions, to watch the replay"),
    keep_open: bool = typer.Option(False, "--keep-open", help="Keep the browser open at the end until Enter is pressed"),
    overlay: Path = typer.Option(None, help="Tenant overlay JSON to apply before replaying (see overlays/)"),
    entry_url_override: str = typer.Option(None, "--entry-url", help="Point the recording at another instance without an overlay"),
) -> None:
    """Replay a capability deterministically (no model) and report the structured result.

    Exit codes: 0 success, 10 business outcome, 20 failure, 30 escalated.
    """
    from cua.artifact.overlay import VariantOverlay, apply_overlay
    from cua.artifact.schema import Capability
    from cua.handoff import ReplayHandoff
    from cua.policy.engine import Policy, PolicyEngine
    from cua.policy.redaction import DEFAULT_SENSITIVE_FIELDS, Redactor
    from cua.replay.engine import ReplayEngine
    from cua.surface.driver import BrowserSurface
    from cua.surface.session import SessionControl

    capability = Capability.model_validate_json(artifact.read_text(encoding="utf-8"))
    if overlay is not None:
        capability = apply_overlay(capability, VariantOverlay.load(overlay))
    if entry_url_override:
        old = capability.target.entry_url
        capability.target.entry_url = entry_url_override
        for step in capability.steps:
            if step.url == old:
                step.url = entry_url_override
    policy = PolicyEngine(Policy.load(policy_path))
    redactor = Redactor(sensitive_field_patterns=policy.policy.sensitive_field_patterns or DEFAULT_SENSITIVE_FIELDS)
    secrets = load_secrets(capability.secrets)
    if headed is None:
        headed = handoff != "none"
    run_id = __import__("time").strftime("%Y%m%dT%H%M%S") + "-replay"
    typer.echo(f"replaying {capability.id} v{capability.version} (variant {capability.target.variant}) "
               f"with inputs {_parse_kv(inputs)}")
    control = SessionControl()
    surface = BrowserSurface(headless=not headed, trace_dir=evidence_dir / run_id, control=control,
                             cdp_port=CDP_PORT if handoff != "none" else None, slow_mo=slow_mo)
    surface.start()
    controller, console = _handoff_setup(handoff, surface, control, redactor, handoff_timeout)
    try:
        engine = ReplayEngine(surface=surface, policy=policy, redactor=redactor, evidence_root=evidence_dir,
                              escalation_handler=ReplayHandoff(controller) if controller else None)
        result = engine.replay(capability, _parse_kv(inputs), secrets, run_id=run_id)
        typer.echo(result.one_line())
        if keep_open and headed:
            typer.echo("browser left open; press Enter to close it")
            input()
    finally:
        surface.stop()
        if console:
            console.stop()
    typer.echo(f"steps: " + ", ".join(f"{r.step_id}:{r.status}" + (f"({r.strategy})" if r.strategy else "")
                                       for r in result.steps))
    for iv in result.interventions:
        typer.echo(f"intervention at {iv.get('step_id')}: {iv.get('kind')} -> {iv.get('decision')} "
                   f"({iv.get('human_actions', 0)} human actions captured)")
    if result.drift:
        typer.echo("drift: " + ", ".join(f"{d['step_id']} resolved on {d['strategy']}#{d['index']}" for d in result.drift)
                   + "  <- locators are falling back; consider an overlay or re-recording")
    typer.echo(f"evidence: {result.evidence_dir}")
    if json_out:
        typer.echo(result.to_json())
    raise typer.Exit(code=result.exit_code)


@app.command("resume")
def resume(
    run_dir: Path = typer.Argument(..., help="Evidence directory of the paused run (printed at escalation)"),
    decision: str = typer.Option("resumed", help="resumed | approved | aborted"),
) -> None:
    """Signal a paused run from a shell: the human has finished (or declined) the manual work."""
    import json

    from cua.handoff import INTERVENTION_FILE, RESUME_FILE

    if decision not in {"resumed", "approved", "aborted"}:
        raise typer.BadParameter("decision must be resumed, approved or aborted")
    token = None
    intervention = run_dir / INTERVENTION_FILE
    if intervention.exists():
        token = json.loads(intervention.read_text(encoding="utf-8")).get("control_token")
    (run_dir / RESUME_FILE).write_text(json.dumps({"decision": decision, "token": token}), encoding="utf-8")
    typer.echo(f"signalled {decision} for {run_dir}")


@app.command("chaos")
def chaos(
    url: str = typer.Option(DEFAULT_ENTRY.rsplit("/", 1)[0], help="Mock app base URL"),
    slow_ms: int = typer.Option(None, help="Delay every page by N ms"),
    expire_session: bool = typer.Option(None, "--expire-session/--no-expire-session", help="Bounce the next request to sign-on"),
    maintenance_dialog: bool = typer.Option(None, "--maintenance-dialog/--no-maintenance-dialog", help="Show the modal notice on the next page"),
    app_error: bool = typer.Option(None, "--app-error/--no-app-error", help="Fail the next member profile load"),
    sticky: bool = typer.Option(None, "--sticky/--no-sticky", help="Keep one-shot flags armed"),
    reset: bool = typer.Option(False, help="Clear all flags"),
) -> None:
    """Inject runtime faults into the mock console (for demos and error-path evidence)."""
    import httpx

    if reset:
        state = httpx.post(f"{url}/__chaos/reset").json()
    else:
        payload = {k: v for k, v in dict(slow_ms=slow_ms, expire_session=expire_session,
                                         maintenance_dialog=maintenance_dialog, app_error=app_error,
                                         sticky=sticky).items() if v is not None}
        state = httpx.post(f"{url}/__chaos", json=payload).json() if payload else httpx.get(f"{url}/__chaos").json()
    typer.echo(state)


if __name__ == "__main__":
    app()
