"""Story 3.4: Deep link v1 (AD-8), built only by ``detect/adapters/deeplink.build_link``."""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from detect.adapters.deeplink import build_link, epoch_ms
from detect.core.incident_rules import DetectorState, Incident

URL = "https://g-abc.grafana-workspace.ap-southeast-1.amazonaws.com"
FROM = datetime(2026, 9, 29, 1, 0, tzinfo=UTC)
TO = datetime(2026, 9, 29, 1, 20, 0, 500_000, tzinfo=UTC)
ALIASES = ("/lng1/train1/P-007/flow_in", "/lng1/train1/P-007/flow_out", "/lng1/train1/P-007/methane_ppm")


def incident(aliases=ALIASES, **kw) -> Incident:
    det = DetectorState(0.8, "Mass balance gap.", "1", FROM, TO, 0)
    base = dict(
        incident_id="01K6ABCDEFGHJKMNPQRSTVWXYZ",
        pipe_id="P-007",
        area="train1",
        status="OPEN",
        opened_at=FROM,
        last_seen=TO,
        evidence_from=FROM,
        evidence_to=TO,
        evidence_aliases=tuple(aliases),
        detectors=MappingProxyType({"leak_mass_balance": det}),
        confidence=0.8,
    )
    return Incident(**{**base, **kw})


def test_exact_link_with_three_aliases():
    link = build_link(incident(), URL, "pipe-detail-dj")
    f, t = int(FROM.timestamp() * 1000) - 300_000, int(TO.timestamp() * 1000) + 300_000
    assert link == (
        f"{URL}/d/pipe-detail-dj/pipe-detail?orgId=1&var-pipe_id=P-007&var-area=train1"
        "&var-evidence=%2Flng1%2Ftrain1%2FP-007%2Fflow_in"
        "&var-evidence=%2Flng1%2Ftrain1%2FP-007%2Fflow_out"
        "&var-evidence=%2Flng1%2Ftrain1%2FP-007%2Fmethane_ppm"
        f"&from={f}&to={t}"
    )
    assert link.count("&var-evidence=") == 3
    assert not any(c.isspace() for c in link) and "\n" not in link
    assert f == 1_790_643_600_000 - 300_000 and t == 1_790_644_800_500 + 300_000


def test_trailing_slash_and_poc_uid():
    assert build_link(incident(), URL + "/", "pipe-detail").startswith(f"{URL}/d/pipe-detail/pipe-detail?orgId=1&")


def test_special_characters_are_encoded():
    link = build_link(incident(aliases=("/root x/a&b=c/P-007/flow_in",)), URL, "pipe-detail")
    assert "&var-evidence=%2Froot%20x%2Fa%26b%3Dc%2FP-007%2Fflow_in&" in link


def test_bad_inputs_are_refused():
    with pytest.raises(ValueError):
        build_link(incident(), "", "pipe-detail")
    with pytest.raises(ValueError):
        build_link(incident(), URL, "")
    with pytest.raises(ValueError):
        epoch_ms(datetime(2026, 9, 29))
