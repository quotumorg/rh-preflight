"""In-session command."""


def card_command(service, raw_args, **kwargs):
    try:
        return service.card(raw_args.strip()).render()
    except (ValueError, AttributeError):
        return "Usage: /rh-preflight <address|GitHub repository URL|project>"
    except Exception:
        return "rh-preflight could not complete this read"
