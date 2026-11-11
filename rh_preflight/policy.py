"""Return the user's rules, without adding a judgement about a project."""

from dataclasses import dataclass
from datetime import datetime, timezone
from .sources.priors import LABEL as PAY_LABEL
from .sources.sherwood import LABEL as VAULT_LABEL


@dataclass(frozen=True)
class Decision:
    action: str
    rules: tuple[str, ...]


def evaluate(cards, settings, unattended=False, now=None):
    now = now or datetime.now(timezone.utc)
    rules = []
    for card in cards:
        official_stock = False
        if card.kind == "network":
            for reading in card.data.get("sources", []):
                if (reading.source == "Toll" and reading.state == "present"
                        and reading.stock_ticker and settings.sources.get(reading.source.lower(), "on") == "on"):
                    official_stock = True
                if reading.source == PAY_LABEL and card.data.get("is_payment", True) and settings.ask_when.get("payee_no_priors_record", True):
                    if reading.state == "missing":
                        rules.append("your rule: payee has no " + PAY_LABEL + " record")
                    elif reading.state in {"unreachable", "refused", "rate_limited", "payment_required"}:
                        rules.append("your rule: payee record could not be read")
                if reading.source == VAULT_LABEL and settings.ask_when.get("vault_strategy_inactive", False):
                    if reading.inactive:
                        rules.append("your rule: vault strategy inactive")
                    elif reading.state in {"unreachable", "refused", "rate_limited", "payment_required"}:
                        rules.append("your rule: vault strategy could not be read")
            card = card.data["base"]
        ask_no_record = settings.ask_no_record and (
            not official_stock or settings.ask_when.get("stock_no_hey_record", False))
        d = card.data
        if card.kind == "unreachable":
            rules.append("your rule: HEY record could not be read" if d.get("read_error")
                         else "your rule: ask when HEY could not be reached")
            continue
        if card.kind == "agent" and (d.get("payload") or {}).get("status") == "not_found":
            if ask_no_record:
                rules.append("your rule: no HEY record")
            continue
        if card.kind == "missing":
            if ask_no_record:
                rules.append("your rule: no HEY record")
            continue
        if card.kind == "identifier":
            # No HEY read was requested; only the applicable source rules apply.
            continue
        measured = d.get("activity_measured") is True
        if not measured and settings.ask_unmeasured:
            rules.append("your rule: activity not measured")
        if settings.ask_not_verified and d.get("verified_builder") is not True:
            rules.append("your rule: not a Verified Builder on HEY's record")
        ship = d.get("latest_meaningful_ship") or {}
        if settings.ask_quiet and measured:
            try:
                shipped = datetime.fromisoformat(ship.get("timestamp", "").replace("Z", "+00:00"))
                quiet = shipped.tzinfo is not None and (now - shipped).total_seconds() >= settings.quiet_days * 86400
            except (TypeError, ValueError):
                # The API measures only a 30-day count; it cannot establish a longer absence.
                count = d.get("meaningful_ships_30d")
                quiet = type(count) is int and count == 0 and settings.quiet_days <= 30
            if quiet:
                rules.append(f"your rule: no ship in {settings.quiet_days} days")
    rules = list(dict.fromkeys(rules))
    if settings.mode == "note":
        return Decision("note", tuple(rules))
    if settings.mode == "ask":
        rules.insert(0, "your rule: ask before every detected action")
    if unattended:
        if not settings.allow_unattended:
            rules.append("your setting: human approval required; unattended actions are blocked")
            return Decision("block", tuple(rules))
        return Decision("pass", tuple(rules + ["your setting: allow_unattended is enabled"]))
    return Decision("ask" if rules else "pass", tuple(rules))
