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
    stops = []
    for card in cards:
        if card.kind == 'transaction':
            d = card.data
            for approval in d.get('approvals', []):
                if approval['unlimited'] and approval['kind'] == 'no code':
                    stops.append('your rule: approval for all to a spender with no code' if approval.get('all_items')
                                 else 'your rule: unlimited approval to a spender with no code')
                elif approval['unlimited'] and not approval.get('known', False):
                    rules.append('your rule: unlimited approval to an unknown contract or unread spender')
            if d.get('approval_eoa'):
                stops.append('your rule: unlimited approval to a spender with no code')
            if d.get('approval_all_eoa'):
                stops.append('your rule: approval for all to a spender with no code')
            if d.get('unbounded_approval'):
                rules.append('your rule: unlimited approval to an unknown contract or unread spender')
            if d.get('delegation'):
                rules.append('your rule: EIP-7702 delegation requires approval')
            if d.get('permit'):
                rules.append('your rule: Permit or Permit2 signature requires approval')
            if d.get('wrong_chain'):
                rules.append('your rule: signing domain chain differs from 4663')
            if d.get('hookr_refused') and settings.ask_hookr_refused:
                rules.append("your rule: Hookr pool rules refuse this wallet")
            if d.get('payout_other') and settings.ask_payout_other:
                rules.append('your rule: payout to another wallet')
            for fact, rule in [('reverted', 'simulation reverted'), ('expired', 'transaction deadline passed'), ('short_eth', 'sender short of ETH')]:
                if d.get(fact):
                    rules.append('your rule: ' + rule)
            if d.get('unchecked'):
                incomplete(rules, stops, settings)
            continue
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
                        incomplete(rules, stops, settings, "payee record could not be read")
                if reading.source == VAULT_LABEL and settings.ask_when.get("vault_strategy_inactive", False):
                    if reading.inactive:
                        rules.append("your rule: vault strategy inactive")
                    elif reading.state in {"unreachable", "refused", "rate_limited", "payment_required"}:
                        incomplete(rules, stops, settings, "vault strategy could not be read")
            card = card.data["base"]
        ask_no_record = settings.ask_no_record and (
            not official_stock or settings.ask_when.get("stock_no_hey_record", False))
        d = card.data
        if card.kind == "unreachable":
            incomplete(rules, stops, settings, "HEY record could not be read" if d.get("read_error") else "ask when HEY could not be reached")
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
    if stops:
        return Decision("block", tuple(dict.fromkeys(stops + rules)))
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


def incomplete(rules, stops, settings, detail='transaction could not be checked'):
    if settings.on_unchecked == 'stop':
        stops.append('your rule: ' + detail)
    elif settings.on_unchecked == 'ask':
        rules.append('your rule: ' + detail)
