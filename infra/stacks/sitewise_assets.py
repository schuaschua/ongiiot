"""SiteWise asset stack, generated from the facility model (AD-2, AD-13, AD-14, CAP-2).

- ``Pipe-<stage>`` asset model: 6 raw DOUBLE measurements with units, the Derived
  properties v1 as measurements (so ``BatchPutAssetPropertyValue`` can write them),
  and the AD-14 attributes with model defaults taken from ``contracts``.
- ``Facility-<stage>`` asset model with a hierarchy to ``Pipe``.
- One asset per pipe with aliases on every raw, derived and attribute property,
  bound through model property external IDs; one Facility root asset linking them.
- A custom resource writes each pipe's attribute values by alias (seed_attributes).
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_iotsitewise as sitewise
from constructs import Construct

from facility.model import MEASUREMENTS, UNITS, Facility
from infra.stacks.seed_attributes import SeedAttributes

PIPES_HIERARCHY = "pipes"


def raw_ext_id(measurement: str) -> str:
    return f"raw.{measurement}"


def derived_ext_id(name: str) -> str:
    return f"derived.{name}"


def attr_ext_id(name: str) -> str:
    return f"attr.{name}"


def _default_value(value: Any) -> str:
    return value if isinstance(value, str) else repr(float(value))


class SiteWiseAssetsStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, facility: Facility, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.facility = facility
        stage = facility.stage

        self.pipe_model = sitewise.CfnAssetModel(
            self,
            "PipeModel",
            asset_model_name=f"Pipe-{stage}",
            asset_model_external_id=f"pipe-model.{stage}",
            asset_model_description=f"LNG pipe with raw sensors, derived data quality and alert state ({stage})",
            asset_model_properties=self._pipe_model_properties(),
        )

        self.facility_model = sitewise.CfnAssetModel(
            self,
            "FacilityModel",
            asset_model_name=f"Facility-{stage}",
            asset_model_external_id=f"facility-model.{stage}",
            asset_model_description=f"Facility root ({stage})",
            asset_model_properties=[
                sitewise.CfnAssetModel.AssetModelPropertyProperty(
                    name="facility_id",
                    external_id=attr_ext_id("facility_id"),
                    data_type="STRING",
                    type=sitewise.CfnAssetModel.PropertyTypeProperty(
                        type_name="Attribute",
                        attribute=sitewise.CfnAssetModel.AttributeProperty(default_value=facility.id),
                    ),
                ),
                sitewise.CfnAssetModel.AssetModelPropertyProperty(
                    name="facility_name",
                    external_id=attr_ext_id("facility_name"),
                    data_type="STRING",
                    type=sitewise.CfnAssetModel.PropertyTypeProperty(
                        type_name="Attribute",
                        attribute=sitewise.CfnAssetModel.AttributeProperty(default_value=facility.name),
                    ),
                ),
            ],
            asset_model_hierarchies=[
                sitewise.CfnAssetModel.AssetModelHierarchyProperty(
                    name="Pipes",
                    external_id=PIPES_HIERARCHY,
                    child_asset_model_id=self.pipe_model.attr_asset_model_id,
                )
            ],
        )

        self.pipe_assets: dict[str, sitewise.CfnAsset] = {}
        for pipe in facility.pipes:
            asset = sitewise.CfnAsset(
                self,
                f"Pipe{pipe.id.replace('-', '')}",
                asset_name=f"{pipe.id}-{stage}",
                asset_external_id=f"{pipe.id}.{stage}",
                asset_description=f"{pipe.service} {pipe.material}, {pipe.area}",
                asset_model_id=self.pipe_model.attr_asset_model_id,
                asset_properties=self._pipe_asset_properties(pipe.id),
            )
            self.pipe_assets[pipe.id] = asset

        self.facility_asset = sitewise.CfnAsset(
            self,
            "FacilityAsset",
            asset_name=f"{facility.id}-{stage}",
            asset_external_id=f"{facility.id}.{stage}",
            asset_description=facility.name,
            asset_model_id=self.facility_model.attr_asset_model_id,
            asset_hierarchies=[
                sitewise.CfnAsset.AssetHierarchyProperty(external_id=PIPES_HIERARCHY, child_asset_id=a.attr_asset_id)
                for a in self.pipe_assets.values()
            ],
        )

        self.seed = SeedAttributes(self, "SeedAttributes", facility=facility, pipe_assets=self.pipe_assets)

        cdk.CfnOutput(self, "AliasRoot", value=facility.alias_root)
        cdk.CfnOutput(self, "PipeAssetModelId", value=self.pipe_model.attr_asset_model_id)
        cdk.CfnOutput(self, "FacilityAssetId", value=self.facility_asset.attr_asset_id)

    # -- model ------------------------------------------------------------------------

    def _pipe_model_properties(self) -> list[sitewise.CfnAssetModel.AssetModelPropertyProperty]:
        P = sitewise.CfnAssetModel.AssetModelPropertyProperty
        T = sitewise.CfnAssetModel.PropertyTypeProperty
        props: list[sitewise.CfnAssetModel.AssetModelPropertyProperty] = []

        for m in MEASUREMENTS:
            props.append(
                P(name=m, external_id=raw_ext_id(m), data_type="DOUBLE", unit=UNITS[m], type=T(type_name="Measurement"))
            )

        for d in self.facility.derived_properties():
            props.append(
                P(name=d.name, external_id=derived_ext_id(d.name), data_type=d.type, type=T(type_name="Measurement"))
            )

        defaults = self._contract_defaults()
        for a in self.facility.attribute_properties():
            default = defaults.get(a.name)
            attribute = sitewise.CfnAssetModel.AttributeProperty(
                default_value=_default_value(default) if default is not None else None
            )
            props.append(
                P(
                    name=a.name,
                    external_id=attr_ext_id(a.name),
                    data_type=a.type,
                    type=T(type_name="Attribute", attribute=attribute),
                )
            )
        return props

    def _contract_defaults(self) -> dict[str, Any]:
        """Model-level attribute defaults from the authored default ``contracts``.

        Per-pipe values, overrides included, are written by the seed custom resource.
        """
        out: dict[str, Any] = {}
        for m, c in self.facility.contract_defaults.items():
            for field, value in c.items():
                if value is not None:
                    out[f"{m}_{field}"] = value
        return out

    # -- assets -----------------------------------------------------------------------

    def _pipe_asset_properties(self, pipe_id: str) -> list[sitewise.CfnAsset.AssetPropertyProperty]:
        f = self.facility
        pipe = f.pipe(pipe_id)
        AP = sitewise.CfnAsset.AssetPropertyProperty
        props = [AP(external_id=raw_ext_id(m), alias=f.alias(pipe_id, m)) for m in pipe.sensors]
        props += [
            AP(external_id=derived_ext_id(d.name), alias=f.derived_alias(pipe_id, d.name))
            for d in f.derived_properties()
            if d.measurement is None or d.measurement in pipe.sensors
        ]
        attrs = f.attribute_values(pipe_id)
        props += [
            AP(external_id=attr_ext_id(a.name), alias=f.attr_alias(pipe_id, a.name))
            for a in f.attribute_properties()
            if a.name in attrs
        ]
        return props
