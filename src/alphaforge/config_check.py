"""Compatibility entry point for the canonical configuration audit."""
from alphaforge.config_audit import audit_config, main

__all__ = ["audit_config", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
