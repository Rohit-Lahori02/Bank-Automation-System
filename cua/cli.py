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
) -> None:
    """Run the mock legacy credit-union console (the automation target)."""
    from mock_app.__main__ import run

    run(host=host, port=port, reload=reload)


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
    headed: bool = typer.Option(False, help="Show the browser"),
    max_steps: int = typer.Option(25, help="Step budget for the agent"),
) -> None:
    """Run an LLM-driven discovery against the target and record a capability artifact."""
    from cua.agent.loop import DiscoveryAgent, DiscoveryConfig
    from cua.artifact.recorder import RecorderSpec, RecordingError, record_capability
    from cua.artifact.store import ArtifactStore
    from cua.llm.client import LLMError, client_from_env
    from cua.policy.engine import Policy, PolicyEngine
    from cua.policy.redaction import DEFAULT_SENSITIVE_FIELDS, Redactor
    from cua.surface.driver import BrowserSurface

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

    typer.echo(f"provider={llm.provider} model={llm.model}")
    typer.echo(f"goal: {goal}")
    run_id = DiscoveryAgent.new_run_id()
    surface = BrowserSurface(headless=not headed, trace_dir=evidence_dir / run_id)
    surface.start()
    agent = DiscoveryAgent(llm=llm, surface=surface, policy=policy, redactor=redactor, evidence_root=evidence_dir,
                           config=DiscoveryConfig(max_steps=max_steps))
    try:
        run = agent.run(goal, entry_url=entry_url, inputs=input_values, secrets=secrets, run_id=run_id)
    finally:
        surface.stop()

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
    headed: bool = typer.Option(False, help="Show the browser"),
    json_out: bool = typer.Option(False, "--json", help="Print the full result JSON"),
) -> None:
    """Replay a capability deterministically (no model) and report the structured result.

    Exit codes: 0 success, 10 business outcome, 20 failure, 30 escalated.
    """
    from cua.artifact.schema import Capability
    from cua.policy.engine import Policy, PolicyEngine
    from cua.policy.redaction import DEFAULT_SENSITIVE_FIELDS, Redactor
    from cua.replay.engine import ReplayEngine
    from cua.surface.driver import BrowserSurface

    capability = Capability.model_validate_json(artifact.read_text(encoding="utf-8"))
    policy = PolicyEngine(Policy.load(policy_path))
    redactor = Redactor(sensitive_field_patterns=policy.policy.sensitive_field_patterns or DEFAULT_SENSITIVE_FIELDS)
    secrets = load_secrets(capability.secrets)
    run_id = __import__("time").strftime("%Y%m%dT%H%M%S") + "-replay"
    typer.echo(f"replaying {capability.id} v{capability.version} with inputs {_parse_kv(inputs)}")
    surface = BrowserSurface(headless=not headed, trace_dir=evidence_dir / run_id)
    surface.start()
    try:
        engine = ReplayEngine(surface=surface, policy=policy, redactor=redactor, evidence_root=evidence_dir)
        result = engine.replay(capability, _parse_kv(inputs), secrets, run_id=run_id)
    finally:
        surface.stop()
    typer.echo(result.one_line())
    typer.echo(f"steps: " + ", ".join(f"{r.step_id}:{r.status}" + (f"({r.strategy})" if r.strategy else "")
                                       for r in result.steps))
    typer.echo(f"evidence: {result.evidence_dir}")
    if json_out:
        typer.echo(result.to_json())
    raise typer.Exit(code=result.exit_code)


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
