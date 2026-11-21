"""Exact PoolManager Donate amounts, separate from approximate payer relations."""
from copy import deepcopy

from .hookr_live import POOL_MANAGER, SWAP, hash32, integer, log_index, words, word_address
from .payer_attribution import address

DONATE = '0x29ef05caaff9404b7cb6d1c0e9bbae9eaa7ab2541feba1a9c4248594c08156cb'
RECAPTURE = 'pool-donate-recapture-exact'


def actions(page):
    return [*page['trades'], *page.get('recaptures', [])]


def decode_donate(row, pool, currencies):
    topics, data = row.get('topics', []), words(row.get('data'))
    tx, block, index = (hash32(row.get('transactionHash')), integer(row.get('blockNumber')),
                        log_index(row.get('logIndex')))
    if (address(row.get('address')) != POOL_MANAGER or row.get('removed') is True
            or len(topics) != 3 or topics[0] != DONATE or topics[1] != pool
            or not hash32(topics[2]) or not word_address(topics[2][2:])
            or len(data) != 2 or not tx or block is None or index is None):
        return None
    return [{'poolId': pool, 'txHash': tx, 'blockNumber': str(block), 'logIndex': index,
             'time': integer(row.get('timeStamp')), 'currency': currencies[i],
             'amount': str(int(w, 16)), 'donor': word_address(topics[2][2:]),
             'sender': None, 'currencies': list(currencies), 'basis': RECAPTURE, 'rawLog': deepcopy(row)}
            for i, w in enumerate(data) if int(w, 16)]


def recapture_scan(reader, page):
    from .hookr_collect import log_scan
    pool = page['poolId']
    scan = log_scan(reader, DONATE, page['fromBlock'], page['throughBlock'],
                    topic1=pool, topic0_1_opr='and')
    page['recaptures'], page['donateComplete'] = [], scan['complete']
    by_tx = {}
    for swap in page['trades']:
        by_tx.setdefault(swap['txHash'], []).append(swap)
    for log in scan['logs']:
        rows = decode_donate(log, pool, page.get('currencies', []))
        if rows is None:
            page['donateComplete'] = False
            reader.note('donate event unavailable')
            continue
        for row in rows:
            swaps = [s for s in by_tx.get(row['txHash'], []) if s['blockNumber'] == row['blockNumber']]
            row['samePoolSwap'] = bool(swaps)
            row['swapEvents'] = [{k: s.get(k) for k in (
                'txHash', 'poolId', 'blockNumber', 'logIndex', 'time', 'eventSender', 'rawLog')} for s in swaps]
            if not swaps:
                row['reason'] = 'donate without same-pool swap'
                reader.note(row['reason'])
            page['recaptures'].append(row)


def exact_parts(candidates, currency, evidence):
    """One part per Donate currency leg. No fee-weight scaling or rounding."""
    parts = []
    for row in candidates:
        if row['currency'] != currency:
            continue
        payer = row.get('sender')
        reason = row.get('reason')
        if not reason and not payer:
            reason = row.get('traceSkipReason', 'recapture payer unresolved')
        part = {'basis': RECAPTURE, 'amount': row['amount'], 'complete': True,
                'approximate': False, 'sender': payer, 'senderResolved': bool(payer),
                'scanComplete': row.get('scanComplete') is True,
                'reconciliationOk': False, 'donate': deepcopy(row),
                'swaps': deepcopy(row.get('swapEvents', [])),
                'senderEvidence': deepcopy(evidence.get(payer, {'sender': payer})),
                'transaction': deepcopy(row.get('transaction'))}
        if reason:
            part['reason'] = reason
        parts.append(part)
    return parts
