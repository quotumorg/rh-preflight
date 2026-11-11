"""Our policy answer and per-project coverage, separate from source text."""

import re

PROJECTS = ('HEY', 'Priors', 'Sentinelhood', 'HoodLock', 'Trnch',
            'Agent Wormhole', 'Hookr', 'Toll', 'Borro', 'Sherwood', 'OffRouter')
SILENT = {'unreachable': 'unreachable', 'refused': 'refused',
          'rate_limited': 'rate limited', 'payment_required': 'payment required',
          'timeout': 'unreachable'}


def answer_line(decision, read_only=False):
    if decision.action == 'block':
        answer = 'stop · human approval required, unattended'
    else:
        label = {'pass': 'go', 'ask': 'ask', 'note': 'note'}[decision.action]
        detail = decision.rules[0] if decision.rules else 'no rule of yours fired'
        if len(decision.rules) > 1:
            detail += f' +{len(decision.rules) - 1} more'
        answer = f'{label} · {detail}'
    return ('if Hermes swapped this now: ' if read_only else '') + answer


def source_data(card):
    """Project each source once for coverage and the local Desktop ledger."""
    from .card import link, source_line

    data = card.data
    settings = data.get('source_settings', {})
    rows = {r.source: r for r in data.get('sources', [])}
    sources = []
    base = data['base']
    for name in PROJECTS:
        key = {'Agent Wormhole': 'wormhole', 'HEY': 'hey'}.get(name, name.lower())
        reading = rows.get(name)
        if name == 'HEY':
            made = data.get('hey_asked', base.kind != 'identifier')
            state = 'unreachable' if base.kind == 'unreachable' else 'answered'
            elapsed = data.get('hey_elapsed', 0.0)
        else:
            made = reading is not None and reading.asked
            state = reading.state if reading else 'not_applicable'
            elapsed = reading.elapsed if reading else 0.0
        reason = None
        if settings.get(key, 'on') == 'off' and name != 'HEY':
            bucket = 'off'
        elif made and state in SILENT:
            prefix = '' if name == 'HEY' else f'{SILENT[state]}; '
            reason = f'{prefix}{elapsed:.1f} s'
            bucket = 'silent'
        elif (not made or state == 'not_applicable' or
              state == 'unlisted' and not reading.line and
              name in {'Hookr', 'Toll', 'Borro', 'Sherwood'}):
            bucket = 'not_for_target'
        else:
            bucket = 'answered'
        url = None
        if bucket == 'answered':
            if name == 'HEY':
                if base.kind == 'agent':
                    project = (base.data.get('payload', {}).get('subject') or {}).get('project') or {}
                    url = link(project.get('url'))
                elif base.kind in {'token', 'contract', 'repo'}:
                    url = link(base.data.get('project_link') or base.data.get('hey_project_url'))
            elif reading:
                urls = re.findall(r'https://[^\s,<>]+', source_line(reading))
                url = link(urls[-1]) if urls else None
        sources.append({'name': 'HEY Research Lab' if name == 'HEY' else name,
                        'bucket': bucket, 'reason': reason,
                        'link': url if url and url.startswith('https://') else None})
    return sources


def coverage(card):
    sources = source_data(card)
    answered, other, silent, off = [], [], [], []
    for source in sources:
        name = 'HEY' if source['name'] == 'HEY Research Lab' else source['name']
        if source['bucket'] == 'answered':
            answered.append(name)
        elif source['bucket'] == 'not_for_target':
            other.append(name)
        elif source['bucket'] == 'silent':
            silent.append(f"{name} ({source['reason']})")
        else:
            off.append(name)
    line = f'{len(sources)} sources'
    if answered:
        line += f' · {len(answered)} answered'
    if other:
        line += f' · {len(other)} not for this target: ' + ', '.join(other)
    if silent:
        line += ' · silent: ' + ', '.join(silent)
    if off:
        line += ' · off: ' + ', '.join(off)
    return line


def present(card, decision, read_only=False, rule_targets=None):
    from .card import Card
    if card.kind != 'network':
        card = Card(card.target, 'network', {'base': card, 'sources': []})
    extra = {'rule_targets': rule_targets} if rule_targets is not None else {}
    return Card(card.target, 'network', {**card.data, **extra,
                'decision': decision, 'read_only': read_only})
