"""Sherwood vault registry and bundle."""
from ..extract import ADDRESS
from .common import plural, Source, Reading, value, boolean, amount

LABEL = "Sherwood"


class Client(Source):
    origin = "https://app.sherwood.sh"
    label = LABEL
    page = origin
    probe_path = "/api/v1/vaults"

    def target_page(self, target=None, **kwargs):
        return self.page + "/api/v1/vault/" + target if isinstance(target, str) and ADDRESS.fullmatch(target) else self.page

    def fetch(self, target, deadline):
        rows = self.read(self.probe_path, deadline, ttl=60)
        row = next((r for r in rows if r.get("vault", "").lower() == target.lower()), None)
        if row is None:
            return Reading(LABEL, "unlisted", "")
        data = self.read("/api/v1/vault/" + target, deadline)
        return self.parse(row, data)

    def parse(self, row, data):
        info = data["info"]
        proposals = data.get("proposals")
        terminal = {"Settled", "Cancelled", "Executed", "Expired", "Vetoed", "Rejected"}
        count = sum(p.get("stateName") not in terminal for p in proposals) if isinstance(proposals, list) and all(p.get("stateName") for p in proposals) else None
        symbol = info.get("assetSymbol")
        total = amount(info.get("totalAssets"), info.get("assetDecimals"))
        agents = data.get("agents")
        return Reading(LABEL, "present",
            f"{LABEL}: vault {value(row.get('displayName'))}: strategy active: {boolean(row.get('strategyActive'))}, "
            f"open {plural(count, 'proposal')} {value(count)}, total assets {total} {value(symbol)}, "
            f"{value(len(agents) if isinstance(agents, list) else None)} {plural(len(agents) if isinstance(agents, list) else None, 'agent')} · {self.page}/api/v1/vault/{info.get('address')}",
            inactive=row.get("strategyActive") is False)
