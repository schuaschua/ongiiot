"""CDK Template assertions for the SiteWise asset stack (CAP-2: 100% mapped)."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from facility.model import (
    CONTRACT_FIELDS,
    DEFAULT_PATH,
    MEASUREMENTS,
    FacilityError,
    load,
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


@pytest.fixture(scope="module", params=["poc-dj"])
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


def test_seed_custom_resource(synthesized):
    f, _, tpl = synthesized
    [seed] = resources(tpl, "Custom::SiteWiseAttributeSeed")
    entries = seed["Entries"]
    assert entries == attribute_entries(f)
    assert len(entries) == len(f.pipes) * len(f.attribute_properties())
    assert {"alias": f.attr_alias("P-007", "flow_in_max"), "type": "DOUBLE", "value": "80.0"} in entries
    assert len(seed["ModelHash"]) == 64


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


# -- app, IAM, asset identity, sensor subsets ------------------------------------------------------


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
