"""Fixture-derived HookrRouter exact-input decoding and stdlib Keccak-256."""
import re
from decimal import Decimal, localcontext

ROUTER = '0xc2bc9a3fe6c4ce8c2f7eb4655b0869b01d8c93c9'
ZERO = '0x' + '0' * 40
_MASK = (1 << 64) - 1
_RC = (0x1, 0x8082, 0x800000000000808a, 0x8000000080008000,
       0x808b, 0x80000001, 0x8000000080008081, 0x8000000000008009,
       0x8a, 0x88, 0x80008009, 0x8000000a, 0x8000808b,
       0x800000000000008b, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x800a,
       0x800000008000000a, 0x8000000080008081, 0x8000000000008080,
       0x80000001, 0x8000000080008008)
_ROT = ((0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61),
        (28, 55, 25, 21, 56), (27, 20, 39, 8, 14))


def keccak256(raw):
    # Ethereum uses Keccak padding (0x01), not hashlib.sha3_256 (0x06).
    raw = bytearray(raw)
    raw.append(1)
    raw.extend(bytes((-len(raw)) % 136))
    raw[-1] |= 0x80
    state = [0] * 25
    def rotate(v, n):
        return ((v << n) | (v >> ((64 - n) % 64))) & _MASK
    for start in range(0, len(raw), 136):
        for i in range(17):
            state[i] ^= int.from_bytes(raw[start + i * 8:start + (i + 1) * 8], 'little')
        for rc in _RC:
            c = [state[x] ^ state[x+5] ^ state[x+10] ^ state[x+15] ^ state[x+20] for x in range(5)]
            d = [c[(x-1) % 5] ^ rotate(c[(x+1) % 5], 1) for x in range(5)]
            b = [0] * 25
            for x in range(5):
                for y in range(5):
                    b[y + 5*((2*x + 3*y) % 5)] = rotate(state[x+5*y] ^ d[x], _ROT[x][y])
            for x in range(5):
                for y in range(5):
                    state[x+5*y] = b[x+5*y] ^ ((~b[(x+1) % 5+5*y]) & b[(x+2) % 5+5*y])
            state[0] ^= rc
    return b''.join(v.to_bytes(8, 'little') for v in state)[:32].hex()


def units(raw, decimals):
    with localcontext() as ctx:
        ctx.prec = max(90, len(str(abs(raw))) + decimals + 1)
        return format(Decimal(raw).scaleb(-decimals), 'f')


def decode(intent):
    tx = intent.get('payload') or {}
    data = tx.get('data', '')
    if (str(tx.get('to', '')).lower() != ROUTER or not intent.get('sender')
            or not isinstance(data, str) or not re.fullmatch(r'0x19b36adf[0-9a-fA-F]{896,}', data)):
        return None
    words = [data[i:i+64].lower() for i in range(10, len(data), 64)]
    if any(len(w) != 64 for w in words):
        return None
    numbers = [int(w, 16) for w in words]
    # PoolKey(currency0,currency1,uint24 fee,int24 tickSpacing,hooks),
    # zeroForOne, int256 amountSpecified, amountBound, priceLimit, recipient,
    # deadline, flag, bytes offset. These positions agree in both captures.
    tick = numbers[3] - (1 << 256) if numbers[3] >> 255 else numbers[3]
    if (any(numbers[i] >= 1 << 160 for i in (0, 1, 4, 9))
            or numbers[0] >= numbers[1] or numbers[2] >= 1 << 24
            or not -(1 << 23) <= tick < 1 << 23 or numbers[5] not in (0, 1)
            or numbers[12] != 416 or numbers[13] * 2 > len(data) - 10 - 14*64):
        return None
    signed = numbers[6] - (1 << 256) if numbers[6] >> 255 else numbers[6]
    currencies = ['0x' + words[i][-40:] for i in (0, 1)]
    sold, bought = currencies if numbers[5] else currencies[::-1]
    return {'poolId': '0x' + keccak256(bytes.fromhex(''.join(words[:5]))),
            'sold': sold, 'bought': bought, 'raw': -signed if signed < 0 else None,
            'hook': '0x' + words[4][-40:], 'wallet': intent['sender']}
