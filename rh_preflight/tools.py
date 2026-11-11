"""Read-only model tool."""

import json
import re


SCHEMA = {
    "name": "builder_card",
    "description": "Read a public network card for a chain 4663 address, project name or slug, or exact registered GitHub repository. Returns source facts and gaps.",
    "parameters": {"type": "object", "properties": {
        "address": {"type": "string", "description": "A 40-hex address on chain 4663"},
        "project": {"type": "string", "description": "A project name, $TICKER or exact slug"},
        "repo_url": {"type": "string", "description": "A GitHub repository root URL"},
    }, "anyOf": [{"required": ["address"]}, {"required": ["repo_url"]}, {"required": ["project"]}], "additionalProperties": False},
}


def handler(service, args, **kwargs):
    try:
        if not any(args.get(k) for k in ("address", "repo_url", "project")) or args.get("address") and args.get("repo_url"):
            raise ValueError("Provide an address, repository URL or project")
        target = args.get("address") or args.get("repo_url") or args["project"]
        project = args.get("project")
        # Display names and tickers use q; a plain slug uses project.
        if project:
            project = project if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", project) else None
        return json.dumps(service.card(target, project=project).public())
    except (TypeError, ValueError, KeyError, AttributeError):
        return json.dumps({"error": "Provide an address, project name or slug, or GitHub repository root URL"})
    except Exception:
        return json.dumps({"error": "rh-preflight could not complete this read"})
