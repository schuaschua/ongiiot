"""Sole owner of the sensor data-quality score (AD-13).

- ``score(findings_for_alias) -> (dq, failing)``: ``dq = 1 - max(confidence of dq_* findings)``,
  ``1.0`` if there are none; ``failing`` is the sorted, de-duplicated ``dq_*`` ids joined by commas.
- The gap rule: an alias with no samples in the window scores ``0.0`` with ``dq_gap``
  (``gap_findings``), whether or not a ``dq_gap`` detector also ran.
"""

from __future__ import annotations

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
    worst = max(min(max(f.confidence, 0.0), 1.0) for f in dq_findings)
    return 1.0 - worst, ",".join(sorted({f.detector_id for f in dq_findings}))


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


def score_facility(fw: FacilityWindow, findings: Iterable[Finding]) -> dict[str, tuple[float, str]]:
    """``alias -> (dq, failing)`` for every expected alias, with the gap rule applied."""
    by_alias: dict[str, list[Finding]] = {a: [] for p in fw.pipes.values() for a in p.aliases.values()}
    for f in [*findings, *gap_findings(fw)]:
        if not is_dq(f):
            continue
        if len(f.evidence_aliases) != 1:
            raise ValueError(f"{f.detector_id}: a dq_* finding must name exactly one alias, got {f.evidence_aliases}")
        (alias,) = f.evidence_aliases
        if alias not in by_alias:
            raise ValueError(f"{f.detector_id}: alias {alias} is not an expected alias of the window")
        by_alias[alias].append(f)
    return {a: score(fs) for a, fs in by_alias.items()}
