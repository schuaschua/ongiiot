"""Deep link v1: the only builder of an incident's one-click evidence link (AD-8).

    {GRAFANA_URL}/d/{DASHBOARD_UID}/pipe-detail?orgId=1&var-pipe_id={pipe_id}&var-area={area}
    &var-evidence={urlencode(alias_1)}&var-evidence={urlencode(alias_2)}...
    &from={epoch_ms(evidence_from) - 300000}&to={epoch_ms(evidence_to) + 300000}

Emitted on one line, no whitespace. ``var-evidence`` is repeated per contributing alias (the incident's sorted
``evidence_aliases``), each URL-encoded (``/`` included). ``tests/test_deeplink_contract.py`` pins the variable
names and the dashboard uid to ``dashboards/pipe-detail.json``.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from detect.core.incident_rules import Incident

MARGIN_MS = 300_000  # 5 minutes either side of the evidence window
DASHBOARD_SLUG = "pipe-detail"


def epoch_ms(t: datetime) -> int:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError(f"datetime must be tz-aware, got {t!r}")
    return int(t.timestamp() * 1000)


def build_link(incident: Incident, grafana_url: str, dashboard_uid: str) -> str:
    """Deep link v1 for ``incident`` on the stage's pipe-detail dashboard."""
    if not grafana_url or not dashboard_uid:
        raise ValueError("grafana_url and dashboard_uid are required")
    parts = [
        f"{grafana_url.rstrip('/')}/d/{dashboard_uid}/{DASHBOARD_SLUG}?orgId=1",
        f"&var-pipe_id={quote(incident.pipe_id, safe='')}",
        f"&var-area={quote(incident.area, safe='')}",
        *(f"&var-evidence={quote(a, safe='')}" for a in incident.evidence_aliases),
        f"&from={epoch_ms(incident.evidence_from) - MARGIN_MS}",
        f"&to={epoch_ms(incident.evidence_to) + MARGIN_MS}",
    ]
    link = "".join(parts)
    if any(c.isspace() for c in link):
        raise ValueError(f"deep link contains whitespace: {link!r}")
    return link
