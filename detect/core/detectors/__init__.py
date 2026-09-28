"""Pure detectors, one module per detector id (AD-5).

Pass 1 (``dq_*``) runs before ``dq_score``; pass 2 (``leak_*``, ``outlier_peer``) runs after it, on the dq-annotated
window. Each detector reads its tunables from ``tunables.detectors.<id>`` and checks them with
``check_tunables`` at cold start.
"""

from __future__ import annotations

from detect.core.detectors.dq_drift import DqDrift
from detect.core.detectors.dq_flatline import DqFlatline
from detect.core.detectors.dq_gap import DqGap
from detect.core.detectors.dq_stuck import DqStuck
from detect.core.detectors.leak_mass_balance import LeakMassBalance
from detect.core.detectors.leak_methane_rise import LeakMethaneRise
from detect.core.detectors.outlier_peer import OutlierPeer

PASS1 = (DqFlatline(), DqStuck(), DqDrift(), DqGap())
PASS2 = (LeakMassBalance(), LeakMethaneRise(), OutlierPeer())

__all__ = ["PASS1", "PASS2", "DqDrift", "DqFlatline", "DqGap", "DqStuck", "LeakMassBalance", "LeakMethaneRise", "OutlierPeer"]
