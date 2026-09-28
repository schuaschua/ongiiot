"""CDK Template assertions for the SiteWise asset stack (CAP-2: 100% mapped)."""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk.assertions import Template

from facility.model import (
    CONTRACT_FIELDS,
    DEFAULT_PATH,
    MEASUREMENTS,
    FacilityError,
    load,
    parse,
)
from infra.app import build_app
from infra.lambdas.seed_attributes import handler
from infra.stacks.seed_attributes import attribute_entries
from infra.stacks.sitewise_assets import SiteWiseAssetsStack

REGION = "ap-southeast-1"


def synth(stage: str):
    facility = load(DEFAULT_PATH, stage=stage)
    app = cdk.App()
    stack = SiteWiseAssetsStack(
        app, f"SiteWiseAssetsStack-{stage}", facility=facility, env=cdk.Environment(region=REGION)
    )
    return facility, stack, Template.from_stack(stack).to_json()


@pytest.fixture(scope="module", params=["poc", "poc-dj"])
def synthesized(request):
    return synth(request.param)


def resources(tpl: dict, type_: str) -> list[dict]:
    return [r["Properties"] for r in tpl["Resources"].values() if r["Type"] == type_]


def aliases(tpl: dict) -> set[str]:
    out = set()
    for asset in resources(tpl, "AWS::IoTSiteWise::Asset"):
        for p in asset.get("AssetProperties", []):
            if "Alias" in p:
                out.add(p["Alias"])
    return out


def test_asset_count_is_pipes_plus_one(synthesized):
    f, _, tpl = synthesized
    assert len(resources(tpl, "AWS::IoTSiteWise::Asset")) == len(f.pipes) + 1
    assert len(resources(tpl, "AWS::IoTSiteWise::AssetModel")) == 2


def test_every_raw_derived_and_attribute_alias_present(synthesized):
    f, _, tpl = synthesized
    found = aliases(tpl)
    expected = set()
    for p in f.pipes:
        for m in p.sensors:
            expected.add(f.alias(p.id, m))
            expected.add(f.derived_alias(p.id, f"{m}_dq"))
            expected.add(f.derived_alias(p.id, f"{m}_dq_fail"))
            for fld in CONTRACT_FIELDS:
                expected.add(f.attr_alias(p.id, f"{m}_{fld}"))
        expected.add(f.derived_alias(p.id, "alert_state"))
        expected.add(f.derived_alias(p.id, "alert_summary"))
        for a in ("pipe_id", "area", "service", "material", "age_years", "peer_group"):
            expected.add(f.attr_alias(p.id, a))
    assert expected == found
    assert all(a.startswith(f.alias_root + "/") for a in found)


def test_derived_types_match_v1(synthesized):
    f, _, tpl = synthesized
    pipe_model = next(m for m in resources(tpl, "AWS::IoTSiteWise::AssetModel") if m["AssetModelName"].startswith("Pipe-"))
    by_name = {p["Name"]: p for p in pipe_model["AssetModelProperties"]}
    for d in f.derived_properties():
        prop = by_name[d.name]
        assert prop["DataType"] == d.type
        assert prop["Type"]["TypeName"] == "Measurement"
    for m in MEASUREMENTS:
        assert by_name[m]["DataType"] == "DOUBLE"
        assert by_name[m]["Type"]["TypeName"] == "Measurement"
        assert by_name[m]["Unit"]
    for a in f.attribute_properties():
        assert by_name[a.name]["Type"]["TypeName"] == "Attribute"
    # model defaults come from the default contracts
    assert by_name["flow_in_max"]["Type"]["Attribute"]["DefaultValue"] == "50.0"
    assert by_name["methane_ppm_owner"]["Type"]["Attribute"]["DefaultValue"] == "HSE"


def test_asset_properties_bound_by_external_id(synthesized):
    _, _, tpl = synthesized
    pipe_model = next(m for m in resources(tpl, "AWS::IoTSiteWise::AssetModel") if m["AssetModelName"].startswith("Pipe-"))
    model_ext_ids = {p["ExternalId"] for p in pipe_model["AssetModelProperties"]}
    for asset in resources(tpl, "AWS::IoTSiteWise::Asset"):
        for p in asset.get("AssetProperties", []):
            assert p["ExternalId"] in model_ext_ids


def test_facility_asset_links_all_pipes(synthesized):
    f, _, tpl = synthesized
    root = next(a for a in resources(tpl, "AWS::IoTSiteWise::Asset") if "AssetHierarchies" in a)
    assert len(root["AssetHierarchies"]) == len(f.pipes)
    assert all(h["ExternalId"] == "pipes" for h in root["AssetHierarchies"])


def test_stage_suffix_in_names(synthesized):
    f, stack, tpl = synthesized
    assert stack.stack_name == f"SiteWiseAssetsStack-{f.stage}"
    for m in resources(tpl, "AWS::IoTSiteWise::AssetModel"):
        assert m["AssetModelName"].endswith(f"-{f.stage}")
    for a in resources(tpl, "AWS::IoTSiteWise::Asset"):
        assert a["AssetName"].endswith(f"-{f.stage}")
    for fn in resources(tpl, "AWS::Lambda::Function"):
        if "FunctionName" in fn:
            assert fn["FunctionName"].endswith(f"-{f.stage}")


def test_seed_custom_resource(synthesized):
    f, _, tpl = synthesized
    [seed] = resources(tpl, "Custom::SiteWiseAttributeSeed")
    entries = seed["Entries"]
    assert entries == attribute_entries(f)
    assert len(entries) == len(f.pipes) * len(f.attribute_properties())
    assert {"alias": f.attr_alias("P-007", "flow_in_max"), "type": "DOUBLE", "value": "80.0"} in entries
    assert len(seed["ModelHash"]) == 64


def test_seed_hash_changes_with_model():
    from infra.stacks.seed_attributes import model_hash
    f = load(DEFAULT_PATH)
    entries = attribute_entries(f)
    changed = [dict(e) for e in entries]
    changed[0]["value"] = "P-999"
    assert model_hash(entries) != model_hash(changed)


def test_two_stages_do_not_collide():
    _, s1, t1 = synth("poc")
    _, s2, t2 = synth("poc-dj")
    assert s1.stack_name != s2.stack_name
    assert not aliases(t1) & aliases(t2)

    def names(tpl):
        out = set()
        for m in resources(tpl, "AWS::IoTSiteWise::AssetModel"):
            out |= {m["AssetModelName"], m["AssetModelExternalId"]}
        for a in resources(tpl, "AWS::IoTSiteWise::Asset"):
            out |= {a["AssetName"], a["AssetExternalId"]}
        out |= {fn["FunctionName"] for fn in resources(tpl, "AWS::Lambda::Function") if "FunctionName" in fn}
        return out

    assert not names(t1) & names(t2)


# -- seed handler (no AWS calls) --------------------------------------------------------------


class FakeSiteWise:
    def __init__(self, fail_first: int = 0):
        self.calls: list[list[dict]] = []
        self.fail_first = fail_first

    def batch_put_asset_property_value(self, entries):
        self.calls.append(entries)
        if self.fail_first:
            self.fail_first -= 1
            return {"errorEntries": [{"entryId": entries[0]["entryId"], "errors": [{"errorCode": "ResourceNotFoundException"}]}]}
        return {"errorEntries": []}


def test_handler_batches_of_ten_or_fewer():
    entries = attribute_entries(load(DEFAULT_PATH))
    fake = FakeSiteWise()
    assert handler.write_entries(entries, client=fake, sleep=lambda s: None) == len(entries)
    assert all(1 <= len(c) <= 10 for c in fake.calls)
    sent = [e for c in fake.calls for e in c]
    assert [e["propertyAlias"] for e in sent] == [e["alias"] for e in entries]
    by_alias = {e["propertyAlias"]: e["propertyValues"][0]["value"] for e in sent}
    f = load(DEFAULT_PATH)
    assert by_alias[f.attr_alias("P-007", "flow_in_max")] == {"doubleValue": 80.0}
    assert by_alias[f.attr_alias("P-007", "age_years")] == {"integerValue": 12}
    assert by_alias[f.attr_alias("P-007", "service")] == {"stringValue": "fuel_gas"}


def test_handler_retries_retryable_errors_only_for_failed_entries():
    entries = attribute_entries(load(DEFAULT_PATH))[:5]
    fake = FakeSiteWise(fail_first=1)
    handler.write_entries(entries, client=fake, sleep=lambda s: None)
    assert len(fake.calls) == 2 and len(fake.calls[1]) == 1


def test_handler_raises_on_non_retryable_error():
    class Bad(FakeSiteWise):
        def batch_put_asset_property_value(self, entries):
            return {"errorEntries": [{"entryId": entries[0]["entryId"], "errors": [{"errorCode": "InvalidRequestException"}]}]}

    with pytest.raises(RuntimeError):
        handler.write_entries(attribute_entries(load(DEFAULT_PATH))[:3], client=Bad(), sleep=lambda s: None)


def test_handler_gives_up_after_max_attempts():
    class AlwaysMissing(FakeSiteWise):
        def batch_put_asset_property_value(self, entries):
            self.calls.append(entries)
            return {
                "errorEntries": [
                    {"entryId": e["entryId"], "errors": [{"errorCode": "ResourceNotFoundException"}]} for e in entries
                ]
            }

    fake = AlwaysMissing()
    with pytest.raises(RuntimeError):
        handler.write_entries(attribute_entries(load(DEFAULT_PATH))[:3], client=fake, sleep=lambda s: None)
    assert len(fake.calls) == handler.MAX_ATTEMPTS


def test_handler_empty_error_codes_are_not_retried():
    class NoCodes(FakeSiteWise):
        def batch_put_asset_property_value(self, entries):
            self.calls.append(entries)
            return {"errorEntries": [{"entryId": entries[0]["entryId"], "errors": []}]}

    fake = NoCodes()
    with pytest.raises(RuntimeError):
        handler.write_entries(attribute_entries(load(DEFAULT_PATH))[:3], client=fake, sleep=lambda s: None)
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    ("request_type", "physical_id", "writes"),
    [("Create", None, True), ("Update", "seed-existing", True), ("Delete", "seed-existing", False)],
)
def test_on_event(monkeypatch, request_type, physical_id, writes):
    entries = attribute_entries(load(DEFAULT_PATH))
    fake = FakeSiteWise()
    monkeypatch.setattr(handler, "_sitewise", lambda: fake)
    event = {
        "RequestType": request_type,
        "ResourceProperties": {"Stage": "poc-dj", "ModelHash": "abc123", "Entries": entries},
    }
    if physical_id:
        event["PhysicalResourceId"] = physical_id
    result = handler.on_event(event, None)
    sent = [e["propertyAlias"] for c in fake.calls for e in c]
    assert sent == ([e["alias"] for e in entries] if writes else [])
    assert result["PhysicalResourceId"] == (physical_id or "sitewise-attribute-seed-poc-dj")
    assert result["Data"]["ModelHash"] == "abc123"


# -- app, IAM, asset identity, sensor subsets ------------------------------------------------------


def _app_aliases(app: cdk.App, stack_name: str) -> tuple[set[str], cdk.Stack]:
    stack = app.node.find_child(stack_name)
    return aliases(Template.from_stack(stack).to_json()), stack


def test_build_app_developer_stage():
    app = build_app(cdk.App(context={"stage": "poc-dj"}))
    found, stack = _app_aliases(app, "SiteWiseAssetsStack-poc-dj")
    assert stack.stack_name == "SiteWiseAssetsStack-poc-dj"
    assert stack.region == "ap-southeast-1"
    assert found and all(a.startswith("/poc-dj/lng1/") for a in found)


def test_build_app_default_stage():
    app = build_app(cdk.App())
    _, stack = _app_aliases(app, "SiteWiseAssetsStack-poc")
    assert stack.stack_name == "SiteWiseAssetsStack-poc"


def test_build_app_empty_stage_rejected():
    with pytest.raises(FacilityError):
        build_app(cdk.App(context={"stage": ""}))


def test_seed_iam_grant_pinned_to_pipe_assets(synthesized):
    _, stack, tpl = synthesized
    fn_id = stack.get_logical_id(stack.seed.function.node.default_child)
    role_ref = tpl["Resources"][fn_id]["Properties"]["Role"]["Fn::GetAtt"][0]
    expected = [{"Fn::GetAtt": [stack.get_logical_id(a), "AssetArn"]} for a in stack.pipe_assets.values()]
    expected.append(stack.resolve(stack.format_arn(service="iotsitewise", resource="time-series", resource_name="*")))
    grants = []
    for r in tpl["Resources"].values():
        if r["Type"] != "AWS::IAM::Policy" or {"Ref": role_ref} not in r["Properties"]["Roles"]:
            continue
        for st in r["Properties"]["PolicyDocument"]["Statement"]:
            if st["Action"] == "iotsitewise:BatchPutAssetPropertyValue":
                grants.append(st)
    assert len(grants) == 1
    assert grants[0]["Effect"] == "Allow"
    assert sorted(grants[0]["Resource"], key=str) == sorted(expected, key=str)


def test_seed_properties_carry_pipe_asset_ids(synthesized):
    f, stack, tpl = synthesized
    [seed] = resources(tpl, "Custom::SiteWiseAttributeSeed")
    assert seed["PipeAssetIds"] == [
        {"Fn::GetAtt": [stack.get_logical_id(stack.pipe_assets[p.id]), "AssetId"]} for p in f.pipes
    ]


def test_pipe_with_sensor_subset():
    doc = yaml.safe_load(Path(DEFAULT_PATH).read_text())
    p7 = next(p for p in doc["pipes"] if p["id"] == "P-007")
    p7["sensors"] = [m for m in p7["sensors"] if m != "methane_ppm"]
    f = parse(doc, stage="poc-dj")
    stack = SiteWiseAssetsStack(cdk.App(), "SubsetStack", facility=f, env=cdk.Environment(region=REGION))
    tpl = Template.from_stack(stack).to_json()
    root = f"{f.alias_root}/train1/P-007/"
    p7_aliases = {a for a in aliases(tpl) if a.startswith(root)}
    assert p7_aliases and not any("methane_ppm" in a for a in p7_aliases)
    assert f.alias("P-008", "methane_ppm") in aliases(tpl)
    [seed] = resources(tpl, "Custom::SiteWiseAttributeSeed")
    assert not any(e["alias"].startswith(root) and "methane_ppm" in e["alias"] for e in seed["Entries"])
    for call in (
        lambda: f.alias("P-007", "methane_ppm"),
        lambda: f.derived_alias("P-007", "methane_ppm_dq"),
        lambda: f.derived_alias("P-007", "methane_ppm_dq_fail"),
        lambda: f.attr_alias("P-007", "methane_ppm_owner"),
    ):
        with pytest.raises(FacilityError):
            call()
