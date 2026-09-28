"""Pure detectors, one module per detector id (AD-5).

Pass 1 (``dq_*``) runs before ``dq_score``; each detector reads its tunables from
``tunables.detectors.<id>`` and checks them with ``check_tunables`` at cold start.
"""

from __future__ import annotations

from detect.core.detectors.dq_flatline import DqFlatline
from detect.core.detectors.dq_stuck import DqStuck

PASS1 = (DqFlatline(), DqStuck())

__all__ = ["PASS1", "DqFlatline", "DqStuck"]
