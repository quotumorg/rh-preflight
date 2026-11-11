"""Toll's free coin check on a token that is not a Robinhood Stock Token: fixed Figures lines only, never a verdict."""
import time

from rh_preflight.sources.toll import Client as Toll

COIN = '0xff9e86e6b21fd688027981bee4b64c8b6b804ed3'

# A real check_coin answer (Toll MCP, 2026-11-11), trimmed to the parts the adapter may and may not read.
WHOLE = '''"$TOLL" ("Toll"), a Robinhood Chain token
Token: 0xff9e86e6b21fd688027981bee4b64c8b6b804ed3
Launched on Pons, 37 days ago. Paired with ETH. On its curve, 20% to graduation.
Maker: 0x5388872a6bbb5fd94533a4dab2a7a1cab02fa5bc

Verdict: CAREFUL (0 against, 5 careful, 5 clear).
Findings: 0 against, 5 careful, 5 clear
- careful: first launch from this dev
- clear: 3.37% of supply burned

Figures:
- Maker's launches: 1 launch
- Buyers on the curve: at least 303
- Flagged wallets: 8, bought 5.9%
- Maker holds now: 1.5% of supply left after burns
- Burned: 3.4% of supply
- Pools: 2

Full report: https://t0ll.xyz/check/0xff9e86e6b21fd688027981bee4b64c8b6b804ed3?utm_source=mcp'''

READING = '''Verdict: INCOMPLETE. Toll is still reading this coin from Robinhood Chain, so this is not a clear. The whole answer in about 13 s.
Call check_coin again in about 13 seconds for the whole answer.
Code: still_reading
Full report: https://t0ll.xyz/check/0xff9e86e6b21fd688027981bee4b64c8b6b804ed3?utm_source=mcp'''


def rpc(text):
    return {'jsonrpc': '2.0', 'id': 1, 'result': {'content': [{'type': 'text', 'text': text}]}}


def client(answer):
    toll, calls = Toll(), []

    def read(path, deadline, payload=None):
        calls.append((path, payload))
        if path.startswith('/api/markets/stock/'):
            return 404, {'error': 'not a Robinhood Stock Token'}
        assert path == '/mcp'
        return 200, rpc(answer)
    toll._read = read
    return toll, calls


def test_non_stock_token_reads_coin_figures():
    toll, calls = client(WHOLE)
    reading = toll.lookup(COIN, time.monotonic() + 5)
    assert reading.state == 'present'
    assert [p for p, _ in calls] == ['/api/markets/stock/' + COIN, '/mcp']
    assert calls[1][1]['params'] == {'name': 'check_coin', 'arguments': {'address': COIN}}
    line = reading.line
    assert line.startswith('Toll: ')
    assert 'launched on Pons 37 days ago' in line
    assert 'maker has 1 launch' in line
    assert "8 wallets on Toll's flag list bought 5.9%" in line
    assert 'maker holds 1.5%' in line
    assert line.endswith('https://t0ll.xyz/check/' + COIN)


def test_coin_line_never_carries_a_verdict_or_finding_prose():
    toll, _ = client(WHOLE)
    line = toll.lookup(COIN, time.monotonic() + 5).line.lower()
    for word in ('careful', 'clear', 'against', 'verdict', 'findings', 'first launch from this dev', 'utm_source'):
        assert word not in line


def test_still_reading_is_unlisted_not_a_fact():
    toll, _ = client(READING)
    reading = toll.lookup(COIN, time.monotonic() + 5)
    assert reading.state == 'unlisted' and reading.line == ''


def test_ticker_that_is_not_a_stock_token_stays_unlisted_without_a_coin_read():
    toll, calls = client(WHOLE)
    reading = toll.lookup('NOTASTOCK', time.monotonic() + 5)
    assert reading.state == 'unlisted'
    assert [p for p, _ in calls] == ['/api/markets/stock/NOTASTOCK']


def test_coin_answer_for_another_token_is_not_used():
    other = WHOLE.replace('Token: ' + COIN, 'Token: 0x' + '1' * 40)
    toll, _ = client(other)
    assert toll.lookup(COIN, time.monotonic() + 5).state == 'unlisted'


def failing(error):
    toll, calls = Toll(), []

    def read(path, deadline, payload=None):
        calls.append(path)
        if path.startswith('/api/markets/stock/'):
            return 404, {}
        raise error
    toll._read = read
    return toll, calls


def test_coin_timeout_429_or_5xx_stays_silent_not_unreachable():
    # quotum review 1: an unanswered coin read is silent, like a 404 or still reading
    from rh_preflight.hey import RateLimited, TemporaryUnavailable, Unavailable
    for error in (Unavailable(), TemporaryUnavailable(), RateLimited(60)):
        toll, _ = failing(error)
        reading = toll.lookup(COIN, time.monotonic() + 5)
        assert reading.state == 'unlisted' and reading.line == '', type(error).__name__


def test_quota_used_up_answer_is_silent():
    used = ("new coins for today are used (10 free). coins people already checked stay free. "
            "more: a Toll key from a wallet holding $TOLL (t0ll.xyz/toll#keys, sent as Authorization: Bearer <key>), "
            "or pay per check with x402 at t0ll.xyz/api/x402.")
    toll, _ = client(used)
    reading = toll.lookup(COIN, time.monotonic() + 5)
    assert reading.state == 'unlisted' and reading.line == ''


def test_whole_coin_answer_is_cached_per_address():
    # quotum review 2: repeats inside the process do not spend the free daily quota
    toll, calls = client(WHOLE)
    first = toll.lookup(COIN, time.monotonic() + 5)
    second = toll.lookup(COIN.upper().replace('0X', '0x'), time.monotonic() + 5)
    assert first.line == second.line and first.state == 'present'
    assert [p for p, _ in calls].count('/mcp') == 1


def test_still_reading_is_not_cached():
    toll, calls = client(READING)
    toll.lookup(COIN, time.monotonic() + 5)
    toll.lookup(COIN, time.monotonic() + 5)
    assert [p for p, _ in calls].count('/mcp') == 2
