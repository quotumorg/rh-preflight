"""Socket-free transaction cards; --write refreshes snapshots and README outputs."""
import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rh_preflight.extract import tx_intent
from rh_preflight.policy import evaluate
from rh_preflight.presentation import present
from rh_preflight.settings import Settings
from rh_preflight.transactions import local_card, project

ROOT = Path(__file__).resolve().parents[1]
TX = ROOT / 'tests/fixtures/toll-tx'
CODES = ROOT / 'tests/fixtures/toll-codes'


def render(request, response):
    intent = tx_intent(request)[0]
    card = project(local_card(intent), intent, response)
    return present(card, evaluate([card], Settings())).render() + '\n'


def outputs():
    for folder in sorted(TX.iterdir()):
        if folder.is_dir():
            yield folder / 'expected-card.txt', render(json.loads((folder / 'request.json').read_text()),
                                                     json.loads((folder / 'response.json').read_text()))
    request = {'to': '0x' + 'a' * 40, 'data': '0x12345678', 'chainId': 4663, 'from': '0x' + 'b' * 40}
    for case in json.loads((CODES / 'synthetic.json').read_text())['cases']:
        yield CODES / (case['code'] + '.txt'), render(request, case['response'])


def readme_outputs():
    from demo.r6_replay import cards
    from demo.gate_replay import replay_clock, replay_service, walkthrough
    priors, nvda = cards()
    gate = StringIO()
    with TemporaryDirectory() as directory, replay_clock() as clock, redirect_stdout(gate):
        walkthrough(replay_service(directory, clock))
    tx = {path.parent.name: output for path, output in outputs() if path.parent.parent == TX}
    # The slash command is input, not generated output.
    return iter([priors.render() + '\n', nvda.render() + '\n',
                 '/rh-preflight ' + nvda.target + '\n', gate.getvalue(),
                 tx['clean-swap'], tx['payout-other-wallet']])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    if args.write:
        for path, output in outputs():
            path.write_text(output)
        readme = ROOT / 'README.md'
        generated = readme_outputs()
        readme.write_text(re.sub(r'```text\n.*?```', lambda match: '```text\n' + next(generated) + '```',
                                 readme.read_text(), flags=re.S))
    else:
        for path, output in outputs():
            print(path.parent.name + '/' + path.name)
            print(output, end='')


if __name__ == '__main__':
    main()
