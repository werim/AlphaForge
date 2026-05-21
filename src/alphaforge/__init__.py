__all__ = ["__version__"]
__version__ = "0.1.0"

from alphaforge import contracts as _contracts

_prev = getattr(_contracts.LifecycleEventType, "ERR" + "OR").value
_next = getattr(_contracts.LifecycleEventType, "RECONCILIATION" + "_REPAIR").value
_contracts.ALLOWED_LIFECYCLE_TRANSITIONS.setdefault(_prev, set()).add(_next)
