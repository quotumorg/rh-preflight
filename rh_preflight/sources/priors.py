"""Priors repayment facts only."""
from urllib.parse import urlencode
from .common import plural, Source, Reading, value, boolean
from ..card import link

LABEL = "Priors"


class Client(Source):
    origin = "https://priors.trade"
    label = LABEL
    page = origin
    probe_path = "/api/check?agent=1"

    def fetch(self, target, deadline, agent=False):
        return self.parse(self.read("/api/check?" + urlencode({"agent" if agent else "address": target}), deadline))

    def parse(self, data):
        agents = data["agents"]
        if not isinstance(agents, list):
            raise ValueError()
        if not agents:
            return Reading(LABEL, "missing", f"{LABEL} has no record for this address or agent · {self.page}")
        # Address matches can include several agents. State the number rather than choosing one silently.
        a = agents[0]
        r = a.get("record") or {}
        line = (f"{LABEL}: agent #{value(a.get('agentId'))} {value(a.get('name'))}: "
                f"{value(r.get('loansRepaid'))} {plural(r.get('loansRepaid'), 'loan')} repaid, {value(r.get('volumeRepaidUsdg'))} USDG repaid, "
                f"defaulted: {boolean(r.get('defaulted'))}, frozen: {boolean(r.get('frozen'))}")
        if len(agents) > 1:
            line += f"; first of {len(agents)} recorded agents"
        return Reading(LABEL, "present", line + " · " + link((a.get("links") or {}).get("page") or self.page),
                       agent_ids=tuple(str(a["agentId"]) for a in agents if str(a.get("agentId", "")).isdigit()))
