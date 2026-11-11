"""CLI registration without network work at registration time."""

import json
import os
import re
import sys

ANSI = {"go": "32", "ask": "33", "stop": "31"}


def colored(text):
    # Only the outcome word, only on a terminal, and never under NO_COLOR.
    if "NO_COLOR" in os.environ or not sys.stdout.isatty():
        return text
    return re.sub(r"^((?:if Hermes swapped this now: )?)(go|ask|stop)(?= · )",
                  lambda m: f"{m[1]}\033[1;{ANSI[m[2]]}m{m[2]}\033[0m", text, count=1)


def setup(parser):
    sub = parser.add_subparsers(dest="rh_preflight_command", required=True)
    card = sub.add_parser("card", help="rh-preflight by quotum with HEY Research Lab; read builder records plus ten Robinhood Chain projects")
    card.add_argument("target", nargs="?")
    card.add_argument("--project", help="An exact HEY project slug")
    sub.add_parser("log", help="Show the latest checks")
    sub.add_parser("doctor", help="Check source reachability and local state")


def run(service, args):
    try:
        if args.rh_preflight_command == "card":
            project = getattr(args, "project", None)
            if not args.target and not project:
                raise ValueError("Provide a target or --project")
            print(colored(service.card(project or args.target, project=project).render()))
        elif args.rh_preflight_command == "log":
            print(json.dumps(service.ledger.rows(), indent=2))
        else:
            print(json.dumps(service.doctor(), indent=2))
        return 0
    except ValueError:
        if args.rh_preflight_command == "card":
            print("Usage: hermes rh-preflight card <address|GitHub repository URL|project> or card --project <slug>")
        else:
            print("rh-preflight could not complete this command")
        return 1
    except Exception:
        print("rh-preflight could not complete this command")
        return 1
