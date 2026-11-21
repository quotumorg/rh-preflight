"""Bounded income pages. Rolling-day APIs cannot prove older daily coverage."""
from copy import deepcopy
from .income import MAX_INCOME_LINES, line_key, number, address
from .hookr_live import ReadStopped


def income_window(reader, token, days):
    raw = reader.mcp('hookr_income', {'token': token, 'days': days})
    reader.income_pages = {'pages': [], 'complete': False}
    if not isinstance(raw.get('window'), dict):
        raise ReadStopped('income window unavailable')
    rows = raw.get('lines')
    if not isinstance(rows, list):
        raise ReadStopped('income shape unavailable')
    capped = len(rows) == 200 or (number(raw.get('linesOmitted')) or 0) > 0
    reader.income_pages = {'recordCap': 200, 'localCap': MAX_INCOME_LINES,
                           'cappedRecord': capped, 'pages': [], 'complete': not capped}
    if not capped:
        return raw
    reader.note('income record capped at 200')
    result = deepcopy(raw)
    result['status'] = 'partial'
    window = raw.get('window', {})
    start, end = number(window.get('from')), number(window.get('through'))
    if start is None or end is None or not 0 <= end - start <= days * 86400:
        return result
    known = {line_key(r): r for r in rows if line_key(r)}
    try:
        tools = reader.mcp('tools/list').get('tools', [])
        schemas = [t.get('inputSchema', {}) for t in tools if isinstance(t, dict) and t.get('name') == 'hookr_income']
        schema = schemas[0] if len(schemas) == 1 else {}
        properties = schema.get('properties', {})
        anchored = 'from' in properties and 'through' in properties
        intervals = [(a, min(end, a + 86400)) for a in range(start, end, 86400)] if anchored else [(max(start, end - 86400), end)]
        complete = bool(anchored and intervals)
        for lo, hi in intervals:
            args = {'token': token, 'days': 1}
            if anchored:
                for name, value in (('from', lo), ('through', hi)):
                    args[name] = str(value) if properties[name].get('type') == 'string' else value
                if any(k not in args for k in schema.get('required', [])):
                    complete = False
                    break
            page = reader.mcp('hookr_income', args)
            w, batch = page.get('window', {}), page.get('lines')
            valid = (isinstance(w, dict) and isinstance(batch, list)
                     and address((page.get('subject') or {}).get('token')) == token
                     and page.get('subject', {}).get('kind') == 'token')
            exact = valid and number(w.get('from')) == lo and number(w.get('through')) == hi
            full = (exact and page.get('status') == 'ready' and page.get('linesOmitted') == 0
                    and page.get('notCounted') == [] and w.get('cut') is False and len(batch) < 200)
            complete &= full
            reader.income_pages['pages'].append({'from': lo, 'through': hi, 'complete': bool(full),
                                                 'lines': len(batch) if valid else None})
            if valid:
                for row in batch[:MAX_INCOME_LINES]:
                    key = line_key(row)
                    if (key and key[1] == token and start <= key[4] <= end
                            and key[3] <= number(window.get('throughBlock'))):
                        known[key] = row
                    else:
                        complete = False
            if len(known) > MAX_INCOME_LINES:
                reader.hit('income line', MAX_INCOME_LINES)
                complete = False
                break
        result['lines'] = sorted(known.values(), key=lambda r: (r['time'], r['txHash'], r['currency']), reverse=True)[:MAX_INCOME_LINES]
        result['linesOmitted'] = (0 if complete else
            max(0, len(rows) + (number(raw.get('linesOmitted')) or 0) - len(result['lines'])))
        if complete:
            result['status'] = 'ready'
        reader.income_pages['complete'] = bool(complete)
    except (ReadStopped, TypeError, ValueError, KeyError, RecursionError):
        pass
    if not reader.income_pages['complete']:
        reader.note('income daily coverage incomplete')
    return result
