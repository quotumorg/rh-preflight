"""Small, factual views of the fields HEY publishes."""

from dataclasses import dataclass, field
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


WORDING = re.compile(r"\b(?:safe|unsafe|scam|risky|trusted|legit|rug|rugged|dump|dumped|blocked|honeypot|verdict|score)\b", re.I)


_TRNCH_WORDING = re.compile(
    r"Trnch: (?:passed the rug screen, live in the Heat|"
    r"no rug, no dump on record · first seen (?P<date>[^\r\n]{1,81})|"
    r"blocked · (?P<category>[^\r\n]{1,81})|rugged|dumped|not screened yet)"
    r" · https://trnch\.fun/token/0x[0-9a-f]{40}"
)


def _trnch_attribution(value):
    match = _TRNCH_WORDING.fullmatch(value)
    return bool(match and not any(WORDING.search(part) for part in match.groups() if part))


def source_line(reading):
    from .sources.trnch import TrnchReading
    # Preserve only Wormhole's established fixed absence sentence, with no
    # external prose able to share its exception.
    if (reading.source == 'Agent Wormhole' and reading.state == 'missing' and
            re.fullmatch(r'Agent Wormhole has not observed this token; that is not a verdict'
                         r' · https://dashboard\.agentwormhole\.com/t/4663/0x[0-9a-fA-F]{40}', reading.line)):
        return reading.line
    attributed = (isinstance(reading, TrnchReading) and reading.source == 'Trnch'
                  and _trnch_attribution(reading.line))
    return text(reading.line, _trnch=attributed) if WORDING.search(reading.line) else reading.line


def clean_url(value):
    try:
        p = urlsplit(value)
        query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                           if not k.lower().startswith("utm_") and k.lower() != "format"])
        return urlunsplit((p.scheme, p.netloc, p.path, query, p.fragment))
    except ValueError:
        return value


def text(value, fallback="not in HEY's answer", *, _trnch=False):
    if value is None or value == "":
        return fallback
    value = " ".join(re.sub(r"\s*[\u2013\u2014]\s*", ", ", str(value)).split())[:600]
    if WORDING.search(value) and not (_trnch and _trnch_attribution(value)):
        return "source text omitted by the wording rule"
    value = re.sub(r"https://[^\s,<>]+", lambda m: clean_url(m[0]), value)
    # Prevent terminal controls and multiline source text becoming instructions.
    return "".join(c for c in value if c.isprintable())


def link(value):
    if not isinstance(value, str):
        return "link not in HEY's answer"
    try:
        p = urlsplit(value)
        if p.scheme == "https" and p.hostname and not p.username and not p.password:
            return text(clean_url(value))
    except ValueError:
        pass
    return "link not in HEY's answer"


def stamp(value):
    return text(value, "date not in HEY's answer")


@dataclass(frozen=True)
class Card:
    target: str
    kind: str
    data: dict = field(default_factory=dict)

    @property
    def evidence_ids(self):
        if self.kind == "network":
            return self.data["base"].evidence_ids
        ids = []
        for name in ("latest_meaningful_ship", "latest_change"):
            item = self.data.get(name) or {}
            if item.get("evidence_id"):
                ids.append(text(item["evidence_id"]))
        ids.extend(text(v) for v in self.data.get("evidence", []) if isinstance(v, str))
        return list(dict.fromkeys(ids))

    def render(self):
        d = self.data
        if self.kind == "transaction":
            from .presentation import answer_line
            heading = [answer_line(d['decision'])] if 'decision' in d else []
            rules = list(d['decision'].rules) if 'decision' in d else []
            heading.extend(rule for rule in rules if rule not in heading[0])
            return "\n".join(heading + d['lines'])
        if self.kind == "network":
            from .presentation import answer_line, coverage
            from .policy import evaluate
            from .settings import Settings
            decision = d.get('decision') or evaluate([self], Settings())
            heading = [answer_line(decision, d.get('read_only', True)), coverage(self)]
            if 'rule_targets' in d:
                heading.extend(f"rule target: {text(item['target'])} · {item['rule']}"
                               for item in d['rule_targets'])
                heading.extend(rule for rule in decision.rules if rule not in heading[0] and
                               (rule.startswith('your setting:') or
                                rule == 'your rule: ask before every detected action'))
            elif len(decision.rules) > 1 or (decision.rules and (decision.action == "block" or decision.rules[0] not in heading[0])):
                heading.extend(decision.rules)
            base = d["base"].render().splitlines()
            citation = d.get("citation")
            if citation and base and base[-1] == citation:
                base.pop()
            return "\n".join(heading + base + [source_line(r) for r in d.get("sources", []) if r.line] + ([citation] if citation else []))
        if self.kind == "agent":
            return render_agent(self.target, d)
        if self.kind == "identifier":
            return f"{text(self.target)}\nHEY not queried for agent or pool ids"
        if self.kind == "unreachable":
            return (f"{self.target}\n{text(d.get('read_error'), 'HEY could not be reached')}\n"
                    "HEY record not read for this check\n"
                    "Freshness (as_of): HEY record not read for this check")
        if self.kind == "missing":
            repository = self.target.startswith("https://")
            noun = "project record for this repository" if repository else "record of this address"
            address_type = "" if repository else "; address type not established"
            answer = (text(d["read_error"]) + "\n") if d.get("read_error") else ""
            return (f"{self.target}\nHEY has no {noun}\n" + answer +
                    f"Builder activity: not in HEY's answer{address_type}\n"
                    "Freshness (as_of): not in HEY's answer")
        project = text(d.get("hey_project_name"), "project name not in HEY's answer")
        header = f"{project} · {self.target} · {link(d.get('project_link') or d.get('hey_project_url'))}"
        if d.get("repository_match"):
            header += "\nHEY registers this exact repository: " + link(d["repository_match"])
        if d.get("activity_applies_to_token") is False:
            header = f"Project record only, activity does not apply to this token: {header}"
        if self.kind == "contract":
            creation = d.get("creation") or {}
            source = d.get("verifiedSource") or {}
            verified = "HEY does not know" if source.get("state") == "UNKNOWN" else "not in HEY's answer"
            if source.get("state") == "MEASURED" and type(source.get("verified")) is bool:
                verified = "yes" if source["verified"] else "no"
            return "\n".join([
                header,
                f"Contract record (HEY) · {self.target}",
                "Verified Builder: not in HEY's answer",
                f"Creation: {stamp(creation.get('at'))} · evidence {text(creation.get('evidenceId'))}",
                f"Source published: {verified} (HEY) · checked {stamp(source.get('checkedAt'))}",
                "Ships in 30 days: not measured here; builder activity not in HEY's answer",
                f"Freshness: {text(d.get('freshness'))}",
            ])
        verified = d.get("verified_builder")
        vb = "yes (HEY)" if verified is True else "no (HEY)" if verified is False else "not in HEY's answer"
        ship = d.get("latest_meaningful_ship") or {}
        if ship:
            latest = (f"Latest meaningful ship: {text(ship.get('title'))} · {stamp(ship.get('timestamp'))}"
                      f" · {link(ship.get('url'))} · Evidence: {link(ship.get('evidence_link'))}")
        else:
            latest = "Latest meaningful ship: not in HEY's answer"
        from .sources.common import plural
        count = d.get("meaningful_ships_30d")
        ships_label = plural(count, "Ship")
        count = str(count) if type(count) is int and count >= 0 else "not measured; count not in HEY's answer"
        change = d.get("latest_change") or {}
        if change:
            changed = (f"Latest change: {text(change.get('summary'))} · {stamp(change.get('occurred_at'))}"
                       f" · observed {stamp(change.get('detected_at'))}"
                       f" · evidence {text(change.get('evidence_id'))} · {link(change.get('url'))}")
        else:
            changed = "Latest change: not in HEY's answer"
        if d.get("hey_status") == "UNKNOWN":
            unknown = "HEY does not know current building activity; activity not measured"
        elif d.get("activity_measured") is not True:
            unknown = "Current building activity: not in HEY's answer; activity not measured"
        elif d.get("last_commit") is None:
            unknown = "Individual commit identifiers: not in HEY's answer"
        else:
            unknown = "Next ship date: not in HEY's answer"
        return "\n".join([
            header,
            f"Status: {text(d.get('hey_status_label') or d.get('hey_status'))} · Verified Builder: {vb}",
            latest,
            f"{ships_label} in 30 days: {count}",
            changed,
            f"Freshness (as_of): {stamp(d.get('as_of'))}",
            unknown,
        ])

    @property
    def digest(self):
        return hashlib.sha256(self.render().encode()).hexdigest()

    def public(self):
        rendered = self.render()
        extra = {}
        if self.kind == 'transaction':
            extra = {'answer': rendered.splitlines()[0], 'rules': list(self.data['decision'].rules), 'sources': []}
        if self.kind == 'network':
            from .presentation import source_data
            from .policy import evaluate
            from .settings import Settings
            decision = self.data.get('decision') or evaluate([self], Settings())
            extra = {'answer': rendered.splitlines()[0], 'coverage': rendered.splitlines()[1],
                     'rules': list(decision.rules), 'sources': source_data(self)}
            if 'rule_targets' in self.data:
                extra['rule_targets'] = self.data['rule_targets']
        return {**extra, "target": self.target, "kind": self.kind, "card": rendered,
                "digest": self.digest, "evidence_ids": self.evidence_ids}


def token_card(target, payload):
    # Deliberate projection: market readings and the upstream disclaimer are omitted.
    keep = ("contract_address", "hey_project_name", "project_link", "hey_project_url", "hey_status_label", "hey_status",
            "verified_builder", "latest_meaningful_ship", "meaningful_ships_30d", "latest_change",
            "latest_change_state", "as_of", "activity_measured", "activity_applies_to_token", "repo_url",
            "last_commit")
    return Card(target, "token", {k: payload.get(k) for k in keep})


def contract_card(target, payload):
    project = payload.get("associatedProject") or {}
    return Card(target, "contract", {
        "hey_project_name": project.get("name"), "project_link": project.get("url"),
        "creation": payload.get("creation"), "verifiedSource": payload.get("verifiedSource"),
        "freshness": "; ".join(f"{k}: {stamp(v)}" for k, v in (payload.get("freshness") or {}).items()) or None,
        "evidence": payload.get("evidence") or [],
    })


def plain(value):
    return value.get("text") if isinstance(value, dict) else value


def agent_card(target, payload):
    data = payload.get("data") or {}
    inputs = {x.get("name"): x.get("value") for x in data.get("inputs", [])}
    state = data.get("builderState") or inputs
    claims = {c.get("id"): c for c in payload.get("claims", [])}
    badge = next((c.get("value") for k, c in claims.items()
                  if "verified_builder" in k or "research_level" in k), None)
    change = data.get("latestMeaningfulChange") or {}
    # Only building events can become a latest ship.
    ship = change if change.get("countsAsBuilding") is True else {}
    evidence = ship.get("evidence") or data.get("supportingEvidence") or []
    timestamp = state.get("lastMeaningfulShipAt")
    vb = badge if type(badge) is bool else (badge == "VERIFIED_BUILDER" if badge else None)
    return Card(target, "agent", {
        "payload": payload, "activity_measured": state.get("activityMeasured"),
        "verified_builder": vb, "meaningful_ships_30d": state.get("meaningfulEvents30d"),
        "latest_meaningful_ship": {"title": plain(ship.get("summary")), "timestamp": timestamp,
            "evidence_link": (evidence[0].get("receiptUrl") if evidence else None)},
        "evidence": [e["id"] for e in evidence if e.get("id")],
    })


def render_agent(target, d):
    payload = d["payload"]
    project = (payload.get("subject") or {}).get("project") or {}
    if payload.get('status') == 'not_found' and not project and re.fullmatch(r'0x[0-9a-fA-F]{40}', target):
        timing = ' · asOf: ' + stamp(payload['asOf']) if payload.get('asOf') else ''
        return ("HEY Research Lab: no published project for this contract "
                "(HEY's index, not a statement about the token)" + timing + " · https://heyresearch.xyz")
    ship = d["latest_meaningful_ship"]
    vb = d.get("verified_builder")
    title = text(ship.get('title'), "title not in HEY's answer")
    lines = [f"HEY Research Lab: {text(plain(project.get('name')), 'project unresolved')} · {text(target)} · {link(project.get('url'))}",
             text(plain(payload.get("answer"))),
             "Verified Builder: " + ("yes (HEY)" if vb is True else "no (HEY)" if vb is False else "not in HEY's answer"),
             f"Latest meaningful ship: {title} · {stamp(ship.get('timestamp'))} · {link(ship.get('evidence_link'))}"]
    if d.get("resolution_note"):
        lines.append(text(d["resolution_note"]))
    unknowns = payload.get("unknowns") or []
    if unknowns:
        item = unknowns[0]
        marked_unknown = any(item.get(key) == "UNKNOWN" for key in ("status", "state", "tag"))
        label = "HEY does not know: " if marked_unknown else "HEY reports: "
        lines.append(label + text(plain(item.get("statement"))))
    fresh = payload.get("freshness") or []
    fresh_values = []
    for item in fresh:
        parts = [text(item[k]) for k in ("freshnessStatus", "dataAsOf") if item.get(k)]
        if parts:
            fresh_values.append(" at ".join(parts))
    timing = (["Freshness: " + "; ".join(fresh_values)] if fresh_values else [])
    if payload.get("asOf"):
        timing.append("asOf: " + stamp(payload["asOf"]))
    if timing:
        lines.append(" · ".join(timing))
    citation = plain(payload.get("citation"))
    if citation:
        lines.append(text(citation))
    return "\n".join(lines)
