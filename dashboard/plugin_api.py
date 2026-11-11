"""Read-only routes, using the router already supplied by the Hermes host."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi import APIRouter


# The dashboard host loads this file independently of the agent package name.
spec = spec_from_file_location("rh-preflight_ledger_view", Path(__file__).resolve().parents[1] / "rh_preflight" / "ledger.py")
module = module_from_spec(spec)
spec.loader.exec_module(module)
router = APIRouter()


@router.get("/checks")
def checks():
    ledger = module.Ledger()
    return {"today": ledger.today(), "checks": ledger.rows()}
