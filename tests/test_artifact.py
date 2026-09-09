"""Artifact schema, templating and store tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from cua.artifact import (
    ArtifactStore, Capability, CapabilityStatus, InputError, InputParam, OutputParam, ParamType, Step, TemplateError,
    TextVisible, is_template, parse_output, references_secret, render_template,
)
from cua.artifact.examples import read_savings_balance
from cua.policy import Redactor
from cua.surface.locators import RoleNameStrategy, Target


@pytest.fixture
def cap() -> Capability:
    return read_savings_balance()


# ------------------------------------------------------------------ schema
def test_reference_capability_validates_and_round_trips(cap):
    text = cap.model_dump_json(indent=2, exclude_none=True)
    restored = Capability.model_validate_json(text)
    assert restored == cap
    assert restored.filename == "member.read_savings_balance.v1.json"
    assert {s.id for s in restored.steps} >= {"open", "login_submit", "search", "read_balance"}
    assert restored.outputs["savings_balance"].from_step == "read_balance"


def test_describe_is_reviewer_readable(cap):
    text = cap.describe()
    assert "member_id: string (required) pattern=\\d{5}" in text
    assert "savings_balance: money from step read_balance" in text
    assert "secrets (supplied at replay, never stored): app.username, app.password" in text
    assert "member_not_found [business_outcome] -> MEMBER_NOT_FOUND" in text
    assert "session_expired [recoverable] -> handler:subflow" in text
    assert "Pa55word" not in text


def test_undeclared_input_reference_is_rejected(cap):
    data = cap.model_dump()
    data["steps"][5]["value"] = "{{inputs.account_id}}"
    with pytest.raises(ValidationError, match="undeclared input 'account_id'"):
        Capability.model_validate(data)


def test_undeclared_secret_reference_is_rejected(cap):
    data = cap.model_dump()
    data["secrets"] = ["app.username"]
    with pytest.raises(ValidationError, match="undeclared secret 'app.password'"):
        Capability.model_validate(data)


def test_duplicate_step_ids_are_rejected(cap):
    data = cap.model_dump()
    data["steps"][1]["id"] = data["steps"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate step ids"):
        Capability.model_validate(data)


def test_output_must_come_from_matching_extract_step(cap):
    data = cap.model_dump()
    data["outputs"]["savings_balance"]["from_step"] = "search"
    with pytest.raises(ValidationError, match="output 'savings_balance' must come from an extract step"):
        Capability.model_validate(data)


def test_unknown_condition_reference_is_rejected(cap):
    data = cap.model_dump()
    data["steps"][6]["on_conditions"] = ["ghost"]
    with pytest.raises(ValidationError, match="unknown condition 'ghost'"):
        Capability.model_validate(data)


def test_condition_shape_rules(cap):
    data = cap.model_dump()
    data["conditions"]["member_not_found"]["code"] = None
    with pytest.raises(ValidationError, match="needs a result code"):
        Capability.model_validate(data)
    data = cap.model_dump()
    data["conditions"]["maintenance_dialog"]["handler"] = None
    with pytest.raises(ValidationError, match="recoverable but has no handler"):
        Capability.model_validate(data)


def test_unknown_fields_are_rejected(cap):
    data = cap.model_dump()
    data["steps"][0]["selector"] = "#legacy"
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_step_action_requirements():
    tgt = Target(description="x", role="button", strategies=[RoleNameStrategy(role="button", name="X")])
    with pytest.raises(ValidationError, match="requires a target"):
        Step(id="a", action="click")
    with pytest.raises(ValidationError, match="requires url"):
        Step(id="b", action="navigate")
    with pytest.raises(ValidationError, match="requires value"):
        Step(id="c", action="type", target=tgt)
    with pytest.raises(ValidationError, match="requires output"):
        Step(id="d", action="extract", target=tgt)
    risky = Step(id="e", action="click", target=tgt, irreversible=True)
    assert risky.risk.value == "risky"


# ------------------------------------------------------------ inputs/outputs
def test_bind_inputs_validates_and_coerces(cap):
    assert cap.bind_inputs({"member_id": " 12345 "}) == {"member_id": "12345"}
    with pytest.raises(InputError, match="required"):
        cap.bind_inputs({})
    with pytest.raises(InputError, match="does not match pattern"):
        cap.bind_inputs({"member_id": "12ab"})
    with pytest.raises(InputError, match="not a declared input"):
        cap.bind_inputs({"member_id": "12345", "extra": "x"})


def test_input_param_types():
    assert InputParam(type=ParamType.INTEGER).coerce("n", "42") == 42
    assert InputParam(type=ParamType.MONEY).coerce("amt", "$1,250.5") == Decimal("1250.50")
    assert InputParam(type=ParamType.BOOLEAN).coerce("flag", "yes") is True
    assert InputParam(required=False).coerce("opt", None) is None
    assert InputParam(default="S-VAC").coerce("product", None) == "S-VAC"
    with pytest.raises(InputError, match="expected integer"):
        InputParam(type=ParamType.INTEGER).coerce("n", "four")


def test_parse_output_types():
    assert parse_output(OutputParam(type=ParamType.MONEY, from_step="s"), "$5,432.10") == Decimal("5432.10")
    assert parse_output(OutputParam(type=ParamType.INTEGER, from_step="s"), "Ref 12345") == 12345
    assert parse_output(OutputParam(type=ParamType.STRING, from_step="s"), "  Oyelaran, Marcus ") == "Oyelaran, Marcus"
    assert parse_output(OutputParam(type=ParamType.BOOLEAN, from_step="s"), "Open") is True


# --------------------------------------------------------------- templating
def test_render_template_substitutes_inputs_and_secrets():
    out = render_template("{{inputs.member_id}} / {{ secrets.app.password }}",
                          {"member_id": "12345"}, {"app.password": "Pa55word!"})
    assert out == "12345 / Pa55word!"
    assert render_template("plain", {}, {}) == "plain"
    assert render_template(None, {}, {}) is None
    assert is_template("{{inputs.x}}") and not is_template("x")
    assert references_secret("{{secrets.a}}") and not references_secret("{{inputs.a}}")
    with pytest.raises(TemplateError, match="secrets.app.password"):
        render_template("{{secrets.app.password}}", {}, {})


# -------------------------------------------------------------------- store
def test_store_saves_loads_lists_and_versions(cap, tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    path = store.save(cap)
    assert path.name == cap.filename and path.exists()
    with pytest.raises(FileExistsError):
        store.save(cap)
    v2 = cap.model_copy(update={"version": 2, "status": CapabilityStatus.APPROVED})
    store.save(v2)
    listing = store.list()
    assert [(c.id, c.version, c.status) for c in listing] == [
        ("member.read_savings_balance", 1, "draft"), ("member.read_savings_balance", 2, "approved"),
    ]
    assert store.load("member.read_savings_balance").version == 2
    assert store.load("member.read_savings_balance", version=1).version == 1
    assert store.load(path).version == 1
    with pytest.raises(FileNotFoundError):
        store.load("nope")


def test_store_refuses_to_persist_secret_values(cap, tmp_path):
    store = ArtifactStore(tmp_path)
    redactor = Redactor(["Pa55word!"])
    store.save(cap, redactor=redactor)  # template references only -> clean
    leaky = cap.model_copy(deep=True)
    leaky.steps[2].value = "Pa55word!"
    leaky.version = 2
    with pytest.raises(Exception, match="contains a secret value"):
        store.save(leaky, redactor=redactor)
    assert not (tmp_path / leaky.filename).exists()


def test_detectors_nest(cap):
    check = cap.checkpoint.detect
    assert check.kind == "all_of" and len(check.detectors) == 3
    assert isinstance(check.detectors[1], TextVisible)
