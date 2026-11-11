"""The adapter to Hermes' current approval context. No host state is changed."""


def unattended(ctx=None):
    try:
        from tools.approval import is_approval_bypass_active
        if is_approval_bypass_active():
            return True
        if ctx is not None and ctx.current_cron_execution() is not None:
            return True
        # Hermes has no public combined presence query. These read-only predicates
        # are isolated here so the host adapter has one compatibility boundary.
        from tools.approval_context import (
            _is_cron_approval_context, _is_single_query_approval_context,
        )
        if _is_cron_approval_context() or _is_single_query_approval_context():
            return True
        from tools.approval import _presence
        _, cli, gateway, ask = _presence()
        return not (cli or gateway or ask)
    except Exception:
        # An unreadable runtime cannot establish that a person can answer.
        return True
