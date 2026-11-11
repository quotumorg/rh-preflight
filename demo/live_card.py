"""Opt-in public HTTP reads; never executes an action."""
from pathlib import Path
import argparse
import sys
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rh_preflight.hooks import Service
from rh_preflight.ledger import Ledger


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('target', nargs='?')
    parser.add_argument('--project')
    args = parser.parse_args()
    if not args.target and not args.project:
        parser.error("provide a target or --project")
    with TemporaryDirectory() as directory:
        print(Service(ledger=Ledger(directory)).card(args.project or args.target, project=args.project).render())


if __name__ == '__main__':
    main()
