"""Evidence-preserving, fail-closed AlphaForge Database Doctor."""
from .diagnostics import diagnose
def plan(*args,**kwargs):
    from .planner import plan as fn
    return fn(*args,**kwargs)
def repair(*args,**kwargs):
    from .repairs import repair as fn
    return fn(*args,**kwargs)
def certify(*args,**kwargs):
    from .verifier import certify as fn
    return fn(*args,**kwargs)
__all__=("diagnose","plan","repair","certify")
