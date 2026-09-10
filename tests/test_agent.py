"""Discovery loop + recorder tests with a scripted model against the real mock console."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

import pytest

from cua.agent import DiscoveryAgent, DiscoveryConfig
from cua.artifact import ActionKind, ArtifactStore, Capability, ConditionClass, ParamType
from cua.artifact.recorder import RecorderSpec, RecordingError, record_capability
from cua.llm.client import LLMResponse
from cua.policy import Policy, PolicyEngine, Redactor
from cua.surface import BrowserSurface
from tests.conftest import PASSWORD, USER

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.timeout(120)

Script = Callable[[str], dict] | dict


class ScriptedLLM:
    """Replays a script of decisions; each entry is a dict or a function of the last user message."""

    provider = "scripted"
    model = "scripted-v1"

    def __init__(self, script: list[Script | str]) -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 2048) -> LLMResponse:
        self.calls.append(messages)
        if not self.script:
            raise AssertionError("script exhausted")
        entry = self.script.pop(0)
        # like a real model, look back to the most recent screen (a retry prompt carries none)
        last_screen = next(m["content"] for m in reversed(messages)
                           if m["role"] == "user" and "CURRENT SCREEN" in m["content"])
        if callable(entry):
            entry = entry(last_screen)
        text = entry if isinstance(entry, str) else json.dumps(entry)
        return LLMResponse(text=text, model=self.model, provider=self.provider, input_tokens=100, output_tokens=20)


def ref_of(screen: str, role: str, name: str, *, nth: int = 0) -> str:
    matches = re.findall(rf'\[(e\d+)\] {role} "{re.escape(name)}"', screen)
    assert matches, f'no {role} "{name}" on screen:\n{screen}'
    return matches[nth]


def act(role: str, name: str, **fields) -> Callable[[str], dict]:
    def build(screen: str) -> dict:
        return {"reasoning": f"act on {name}", "ref": ref_of(screen, role, name), **fields}
    return build


HAPPY_PATH: list[Script] = [
    act("textbox", "User ID", action="type", value="{{secrets.app.username}}"),
    act("textbox", "Password", action="type", value="{{secrets.app.password}}"),
    act("button", "Sign On", action="click", expect="Main Menu"),
    act("link", "Member Inquiry", action="click", expect="Member Inquiry"),
    act("textbox", "Member Number", action="type", value="12345"),
    act("button", "Search", action="click", expect="Search Results"),
    act("link", "View", action="click", expect="Member Profile"),
    act("cell", "$5,432.10", action="extract", output="savings_balance"),
    act("cell", "Oyelaran, Marcus", action="extract", output="member_name"),
    {"reasoning": "goal complete", "action": "done", "checkpoint": "Member Profile",
     "outputs": {"savings_balance": "$5,432.10", "member_name": "Oyelaran, Marcus"}},
]


@pytest.fixture
def policy(mock_server) -> PolicyEngine:
    base = Policy.load(ROOT / "policy.yaml")
    return PolicyEngine(base.model_copy(update={"allowed_origins": [mock_server.base_url]}))


@pytest.fixture
def surface():
    with BrowserSurface(headless=True) as s:
        yield s


def make_agent(llm, surface, policy, tmp_path, **cfg) -> DiscoveryAgent:
    return DiscoveryAgent(llm=llm, surface=surface, policy=policy, redactor=Redactor([PASSWORD]),
                          evidence_root=tmp_path / "runs", config=DiscoveryConfig(**cfg))


def run_happy(surface, policy, tmp_path, mock_server, script=None):
    llm = ScriptedLLM(script or list(HAPPY_PATH))
    agent = make_agent(llm, surface, policy, tmp_path)
    run = agent.run("Look up member 12345 and read their current savings balance",
                    entry_url=f"{mock_server.base_url}/login", inputs={"member_id": "12345"},
                    secrets={"app.username": USER, "app.password": PASSWORD})
    return run, llm, agent


# ------------------------------------------------------------ the loop
def test_discovery_completes_goal_and_extracts_outputs(surface, policy, tmp_path, mock_server):
    run, llm, agent = run_happy(surface, policy, tmp_path, mock_server)
    assert run.status == "success", run.stop_reason
    assert run.outputs == {"savings_balance": "$5,432.10", "member_name": "Oyelaran, Marcus"}
    assert run.checkpoint_text == "Member Profile" and run.final_url.endswith("/members/12345")
    assert len(run.executed_actions) == 10 and run.llm_calls == 10
    # expectations the model stated were verified on the live surface
    held = {a.decision.expect: a.expect_held for a in run.actions if a.decision.expect}
    assert held == {"Main Menu": True, "Member Inquiry": True, "Search Results": True, "Member Profile": True}
    # secrets never reached the model or the evidence
    for messages in llm.calls:
        assert PASSWORD not in json.dumps(messages)
    evidence = (run.evidence_dir / "log.jsonl").read_text(encoding="utf-8")
    transcript = (run.evidence_dir / "transcript.json").read_text(encoding="utf-8")
    assert PASSWORD not in evidence and PASSWORD not in transcript
    assert "[REDACTED:PHONE]" in transcript      # profile phone number scrubbed from logged screens
    assert "[REDACTED:EMAIL]" in transcript
    assert run.run_id in evidence                # run ids are not mistaken for card numbers
    assert '"input_tokens": 100' in evidence     # usage counters are not mistaken for tokens
    assert (run.evidence_dir / "summary.json").exists() and (run.evidence_dir / "final.png").exists()
    summary = json.loads((run.evidence_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "success" and summary["steps_executed"] == 10


def test_context_window_keeps_only_recent_screens(surface, policy, tmp_path, mock_server):
    run, llm, agent = run_happy(surface, policy, tmp_path, mock_server)
    last_messages = llm.calls[-1]
    omitted = [m for m in last_messages if m["role"] == "user" and "screen omitted" in m["content"]]
    verbatim = [m for m in last_messages if m["role"] == "user" and "--- frame: main ---" in m["content"]]
    assert len(omitted) >= 5 and len(verbatim) == 3   # 2 history screens + the current one
    assert last_messages[0]["role"] == "user" and last_messages[1]["role"] == "assistant"
    roles = [m["role"] for m in last_messages]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))


def test_invalid_reply_gets_one_retry_then_recovers(surface, policy, tmp_path, mock_server):
    script = ["I think I should click something", '{"action": "click"}', *HAPPY_PATH]
    run, llm, agent = run_happy(surface, policy, tmp_path, mock_server, script)
    assert run.status == "success"
    assert run.llm_calls == 12
    feedback = llm.calls[2][-1]["content"]
    assert "not a valid action" in feedback


def test_unknown_ref_is_reported_not_executed(surface, policy, tmp_path, mock_server):
    script = [{"reasoning": "guess", "action": "click", "ref": "e999"}, *HAPPY_PATH]
    run, llm, agent = run_happy(surface, policy, tmp_path, mock_server, script)
    assert run.status == "success"
    first = run.actions[0]
    assert not first.executed and "not on the current screen" in first.result
    assert "not on the current screen" in llm.calls[1][-1]["content"]


def test_risky_action_is_held_and_reported_to_model(surface, policy, tmp_path, mock_server):
    script = [
        *HAPPY_PATH[:7],
        act("link", "Close Account", action="click"),
        {"reasoning": "cannot close; goal met anyway", "action": "done", "checkpoint": "Member Profile", "outputs": {}},
    ]
    run, llm, agent = run_happy(surface, policy, tmp_path, mock_server, script)
    assert run.status == "success"
    held = run.actions[7]
    assert not held.executed and held.verdict.needs_human and "requires human approval" in held.result
    events = [json.loads(l) for l in (run.evidence_dir / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["kind"] == "policy" and e["result"] == "held" for e in events)


def test_policy_blocks_navigation_outside_allowlist(surface, policy, tmp_path, mock_server):
    script = [
        {"reasoning": "go elsewhere", "action": "navigate", "url": "https://evil.example.com/"},
        {"reasoning": "go elsewhere", "action": "navigate", "url": f"{mock_server.base_url}/__chaos"},
        {"reasoning": "go elsewhere", "action": "navigate", "url": "https://evil.example.com/"},
    ]
    run, llm, agent = run_happy(surface, policy, tmp_path, mock_server, script)
    assert run.status == "policy_blocked"
    assert all(not a.executed and "blocked by policy" in a.result for a in run.actions)
    assert surface.url.endswith("/login")


def test_done_without_extract_is_rejected(surface, policy, tmp_path, mock_server):
    script = [
        *HAPPY_PATH[:7],
        {"reasoning": "I can see it", "action": "done", "checkpoint": "Member Profile",
         "outputs": {"savings_balance": "$5,432.10"}},
        act("cell", "$5,432.10", action="extract", output="savings_balance"),
        {"reasoning": "now extracted", "action": "done", "checkpoint": "Member Profile",
         "outputs": {"savings_balance": "$5,432.10"}},
    ]
    run, llm, agent = run_happy(surface, policy, tmp_path, mock_server, script)
    assert run.status == "success" and run.outputs == {"savings_balance": "$5,432.10"}
    assert "never extracted" in run.actions[7].result


def test_no_progress_stops_the_run(surface, policy, tmp_path, mock_server):
    script = [act("button", "Clear", action="click")] * 3
    llm = ScriptedLLM(script)
    agent = make_agent(llm, surface, policy, tmp_path, max_no_progress=3)
    run = agent.run("loop forever", entry_url=f"{mock_server.base_url}/login", inputs={}, secrets={})
    assert run.status == "no_progress" and len(run.actions) == 3
    assert "did not change" in run.actions[0].result


def test_stuck_ends_run_with_reason_and_screenshot(surface, policy, tmp_path, mock_server):
    llm = ScriptedLLM([{"reasoning": "no way", "action": "stuck", "reason": "no member search on this screen"}])
    agent = make_agent(llm, surface, policy, tmp_path)
    run = agent.run("impossible", entry_url=f"{mock_server.base_url}/login", inputs={}, secrets={})
    assert run.status == "stuck" and run.stop_reason == "no member search on this screen"
    assert (run.evidence_dir / "stuck.png").exists()


def test_max_steps_budget(surface, policy, tmp_path, mock_server):
    llm = ScriptedLLM(list(HAPPY_PATH))
    agent = make_agent(llm, surface, policy, tmp_path, max_steps=3)
    run = agent.run("x", entry_url=f"{mock_server.base_url}/login", inputs={"member_id": "12345"},
                    secrets={"app.username": USER, "app.password": PASSWORD})
    assert run.status == "max_steps" and len(run.actions) == 3


# ------------------------------------------------------------ the recorder
def test_recorder_produces_a_valid_parameterized_capability(surface, policy, tmp_path, mock_server):
    run, llm, agent = run_happy(surface, policy, tmp_path, mock_server)
    spec = RecorderSpec(capability_id="member.read_savings_balance", name="Read savings balance",
                        description="Look up a member and read the savings balance", app="corelink-member-servicing")
    cap = record_capability(run, spec, policy=policy, redactor=Redactor([PASSWORD]))

    assert cap.inputs["member_id"].example == "12345"
    by_id = {s.id: s for s in cap.steps}
    assert [s.action for s in cap.steps] == [ActionKind.NAVIGATE, ActionKind.TYPE, ActionKind.TYPE, ActionKind.CLICK,
                                             ActionKind.CLICK, ActionKind.TYPE, ActionKind.CLICK, ActionKind.CLICK,
                                             ActionKind.EXTRACT, ActionKind.EXTRACT]
    assert by_id["s00_navigate"].url.endswith("/login")               # the loop's own entry navigation is recorded
    assert by_id["s00_navigate"].expect.detect.text == "Operator Sign On"   # page title, not tenant branding
    assert by_id["s05_type"].value == "{{inputs.member_id}}"          # literal input parameterized
    assert by_id["s02_type"].value == "{{secrets.app.password}}"      # secret placeholder preserved
    assert by_id["s03_click"].expect.detect.text == "Main Menu"        # model's expect became a postcondition
    assert by_id["s08_extract"].output == "savings_balance"
    assert cap.outputs["savings_balance"].type is ParamType.MONEY
    assert cap.outputs["member_name"].type is ParamType.STRING
    assert cap.outputs["savings_balance"].from_step == "s08_extract"
    # value cell got a value-independent table strategy first, with the input parameterized in the row anchor
    assert by_id["s08_extract"].target.strategies[0].kind == "table_cell"
    assert by_id["s08_extract"].target.strategies[0].row_text == "{{inputs.member_id}}-S01"
    view = by_id["s07_click"].target
    assert any(getattr(s, "selector", "") == 'a[href="/members/{{inputs.member_id}}"]' for s in view.strategies)
    # sign-on became the re-login subflow of the app's session_expired condition
    sub = cap.conditions["session_expired"].handler
    assert [s.id for s in sub.steps] == ["s01_type", "s02_type", "s03_click"]
    assert cap.conditions["member_not_found"].classification is ConditionClass.BUSINESS_OUTCOME
    # checkpoint: final url pattern + the model's checkpoint text
    assert cap.checkpoint.detect.kind == "all_of"
    assert cap.checkpoint.detect.detectors[0].pattern == r"/members/[^/]+(?:[?#]|$)"
    assert cap.checkpoint.detect.detectors[1].text == "Member Profile"
    assert cap.provenance.provider == "scripted" and cap.provenance.discovery_steps == 10
    assert all(s.risk.value == "safe" for s in cap.steps)

    store = ArtifactStore(tmp_path / "artifacts")
    path = store.save(cap, redactor=Redactor([PASSWORD, USER]))
    text = path.read_text(encoding="utf-8")
    assert PASSWORD not in text and USER not in text
    assert Capability.model_validate_json(text) == cap


def test_recorder_turns_dialog_dismissal_into_condition(surface, policy, tmp_path, mock_server):
    mock_server.chaos.update(maintenance_dialog=True)
    script = [act("button", "OK", action="click"), *HAPPY_PATH]
    run, llm, agent = run_happy(surface, policy, tmp_path, mock_server, script)
    assert run.status == "success"
    spec = RecorderSpec(capability_id="member.read_savings_balance", name="x", description="x",
                        app="corelink-member-servicing")
    cap = record_capability(run, spec, policy=policy)
    assert len(cap.steps) == 10                         # entry navigate + 9 flow steps; the OK click is not one
    learned = cap.conditions["dialog_system_maintenance_notice"]
    assert learned.classification is ConditionClass.RECOVERABLE
    assert learned.detect.name_contains == "System Maintenance Notice"
    assert learned.handler.target.strategies[0].kind == "role_name"


def test_recorder_validates_checkpoint_and_infers_expectations(surface, policy, tmp_path, mock_server):
    """A model that writes a sentence as its checkpoint, and omits 'expect' on a same-URL form post."""
    script = [
        *HAPPY_PATH[:5],
        act("button", "Search", action="click"),                   # no expect; URL does not change
        HAPPY_PATH[6],
        act("cell", "$5,432.10", action="extract", output="savings_balance", expect="$5,432.10"),  # value as hint
        HAPPY_PATH[8],
        {"reasoning": "done", "action": "done",
         # a sentence, not screen text - and it names the member, which must NOT become the checkpoint
         "checkpoint": "Member 12345 · Oyelaran, Marcus profile shows savings balance $5,432.10",
         "outputs": {"savings_balance": "$5,432.10", "member_name": "Oyelaran, Marcus"}},
    ]
    run, llm, agent = run_happy(surface, policy, tmp_path, mock_server, script)
    cap = record_capability(run, RecorderSpec("member.read_savings_balance", "x", "x", "corelink-member-servicing"),
                            policy=policy)
    by_id = {s.id: s for s in cap.steps}
    assert by_id["s06_click"].expect.detect.text == "Search Results"     # inferred from text that appeared
    assert by_id["s08_extract"].expect is None                           # reads never carry an expectation
    detectors = cap.checkpoint.detect.detectors
    assert detectors[0].pattern == r"/members/[^/]+(?:[?#]|$)"
    assert detectors[1].text == "Member Profile"    # static label wins over the member's name (a value cell)
    assert "reduced to the visible fragment" in cap.provenance.notes


def test_recorder_refuses_unsuccessful_runs(surface, policy, tmp_path, mock_server):
    llm = ScriptedLLM([{"reasoning": "no", "action": "stuck", "reason": "nope"}])
    run = make_agent(llm, surface, policy, tmp_path).run("x", entry_url=f"{mock_server.base_url}/login",
                                                         inputs={}, secrets={})
    with pytest.raises(RecordingError, match="only successful runs"):
        record_capability(run, RecorderSpec("a.b", "a", "b", "corelink-member-servicing"), policy=policy)


def test_recorder_parameterizes_urls():
    from cua.artifact.recorder import _parameterize, _url_pattern
    inputs = {"member_id": "12345", "product": "S-VAC"}
    assert _parameterize("http://x/members/12345/subaccount", inputs) == "http://x/members/{{inputs.member_id}}/subaccount"
    assert _url_pattern("http://x/members/12345", inputs) == r"/members/[^/]+(?:[?#]|$)"
    assert re.search(_url_pattern("http://x/members/12345", inputs), "http://y/members/10001")
    assert not re.search(_url_pattern("http://x/members/12345", inputs), "http://y/members/10001/subaccount/new")
    # a confirmation page carries a query string; the pattern must still match it for other refs
    confirmed = _url_pattern("http://x/members/12345/subaccount/confirmed?ref=CNF-1", inputs)
    assert re.search(confirmed, "http://x/members/10001/subaccount/confirmed?ref=CNF-9")
