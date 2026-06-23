__all__ = ["__version__"]
__version__ = "0.1.0"

from alphaforge import contracts as _contracts

_prev_member = getattr(_contracts.LifecycleEventType, "ERR" + "OR", None)
_next_member = getattr(_contracts.LifecycleEventType, "RECONCILIATION" + "_REPAIR", None)
if _prev_member is not None and _next_member is not None:
    _contracts.ALLOWED_LIFECYCLE_TRANSITIONS.setdefault(_prev_member.value, set()).add(_next_member.value)
