"""Hermes directory-plugin entry point."""

if __package__:
    from .rh_preflight.plugin import register
else:
    from rh_preflight.plugin import register

__all__ = ["register"]
