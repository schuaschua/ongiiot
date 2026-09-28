"""Sole owner of the sensor data-quality score (AD-13).

- ``score(findings_for_alias) -> (dq, failing)``: ``dq = 1 - max(confidence of dq_* findings)``,
  ``1.0`` if there are none (confidences clamped to [0, 1], non-finite counted as 1.0); ``failing`` is the sorted, de-duplicated ``dq_*`` ids joined by commas.
- The gap rule: an alias with no samples in the window scores ``0.0`` with ``dq_gap``. The ``dq_gap``
  detector is the source of that finding when it ran; ``gap_findings`` adds the rule's own finding
  only for an empty alias that has no ``dq_gap`` finding yet (e.g. the detector failed), so exactly
  one ``dq_gap`` finding names each empty alias.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from detect.core.models import FacilityWindow, Finding

DQ_PREFIX = "dq_"
GAP_ID = "dq_gap"
GAP_RULE_VERSION = "1"


def is_dq(finding: Finding) -> bool:
    return finding.detector_id.startswith(DQ_PREFIX)


def score(findings_for_alias: Iterable[Finding]) -> tuple[float, str]:
    dq_findings = [f for f in findings_for_alias if is_dq(f)]
    if not dq_findings:
        return 1.0, ""
    worst = max(_confidence(f.confidence) for f in dq_findings)
    return 1.0 - worst, ",".join(sorted({f.detector_id for f in dq_findings}))


def _confidence(c: float) -> float:
    """Clamp to [0, 1]; a non-finite confidence counts as 1.0 (fail safe: never a NaN score)."""
    if not isinstance(c, (int, float)) or not math.isfinite(c):
        return 1.0
    return min(max(float(c), 0.0), 1.0)


def gap_findings(fw: FacilityWindow) -> list[Finding]:
    """One ``dq_gap`` finding (confidence 1.0) per expected alias with no samples in the window."""
    w = fw.window
    out = []
    for pipe_id in sorted(fw.pipes):
        for alias in sorted(fw.pipes[pipe_id].aliases.values()):
            if not w.series.get(alias):
                out.append(
                    Finding(
                        detector_id=GAP_ID,
                        detector_version=GAP_RULE_VERSION,
                        pipe_id=pipe_id,
                        evidence_aliases=(alias,),
                        window_start=w.start,
                        window_end=w.end,
                        confidence=1.0,
                        explanation=f"No samples from {alias} in the window.",
                    )
                )
    return out


def check_finding(f: Finding, known_aliases: Iterable[str]) -> None:
    """Raise ``ValueError`` unless a ``dq_*`` finding names exactly one alias from ``known_aliases``."""
    if not is_dq(f):
        return
    if len(f.evidence_aliases) != 1:
        raise ValueError(f"{f.detector_id}: a dq_* finding must name exactly one alias, got {f.evidence_aliases}")
    if f.evidence_aliases[0] not in set(known_aliases):
        raise ValueError(f"{f.detector_id}: alias {f.evidence_aliases[0]} is not an expected alias of the window")


def with_gap_rule(fw: FacilityWindow, findings: Iterable[Finding]) -> list[Finding]:
    """``findings`` plus the gap rule's finding for each empty alias not already named by a ``dq_gap`` finding."""
    found = list(findings)
    covered = {a for f in found if f.detector_id == GAP_ID for a in f.evidence_aliases}
    return found + [g for g in gap_findings(fw) if g.evidence_aliases[0] not in covered]


def score_facility(fw: FacilityWindow, findings: Iterable[Finding]) -> dict[str, tuple[float, str]]:
    """``alias -> (dq, failing)`` for every expected alias, with the gap rule applied."""
    by_alias: dict[str, list[Finding]] = {a: [] for p in fw.pipes.values() for a in p.aliases.values()}
    for f in with_gap_rule(fw, findings):
        if not is_dq(f):
            continue
        check_finding(f, by_alias)
        by_alias[f.evidence_aliases[0]].append(f)
    return {a: score(fs) for a, fs in by_alias.items()}
