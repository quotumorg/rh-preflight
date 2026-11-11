"""Identify scheduled execution through Hermes' public plugin context."""


def unattended(ctx=None):
    """Only public cron identity establishes the plugin's cron-only policy.

    Missing or unreadable context leaves approval requests to Hermes' gate.
    Presence and approval bypass are owned by the host.
    """
    try:
        return ctx is not None and ctx.current_cron_execution() is not None
    except Exception:
        return False
