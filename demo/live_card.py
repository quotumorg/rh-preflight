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
    parser.add_argument('--hookr-income', action='store_true', help='read bounded live hookr income evidence')
    parser.add_argument('--days', type=int, default=7)
    args = parser.parse_args()
    if not args.target and not args.project:
        parser.error("provide a target or --project")
    if args.hookr_income:
        from rh_preflight.income import address
        if not address(args.target) or not 1 <= args.days <= 30:
            parser.exit(2, 'hookr income requires a token address and days from 1 to 30\n')
    with TemporaryDirectory() as directory:
        card = Service(ledger=Ledger(directory)).card(args.project or args.target, project=args.project)
        if args.hookr_income:
            from rh_preflight.hookr_live import read_income
            evidence = read_income(args.target, days=args.days)
            card.data['hookr_income'] = evidence
            print(card.render())
            if card.income() is None:
                print('hookr live: partial · ' + '; '.join(evidence['live']['reasons']))
        else:
            print(card.render())


if __name__ == '__main__':
    main()
