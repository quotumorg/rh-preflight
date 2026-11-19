"""Validate CA overrides once per startup configuration; keep TLS verification on."""
from copy import deepcopy
import os
from pathlib import Path
import ssl
import sys
import threading

_LOCK = threading.Lock()
_CONFIGS = {}


def configuration():
    values = tuple(os.environ.get(name) for name in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'))
    with _LOCK:
        if values in _CONFIGS:
            return _CONFIGS[values]
        failures, valid = [], []
        for name, path in zip(('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'), values):
            if not path:
                continue
            try:
                if not Path(path).is_file():
                    raise OSError()
                with open(path, 'rb') as bundle:
                    bundle.read(1)
                valid.append(path)
            except OSError:
                failures.append({'variable': name, 'path': path,
                                 'fix': 'make the bundle readable or unset the variable'})
        if failures:
            overrides = ', '.join(item['variable'] for item in failures)
            print(f'rh-preflight: warning: {overrides} missing or unreadable; '
                  'using the default TLS trust store; make the bundle readable or unset the variable',
                  file=sys.stderr)
        # A broken override must not cause an unverified HTTPS connection.
        if failures:
            try:
                import certifi
                context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                defaults = ssl.get_default_verify_paths()
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                cafile = defaults.openssl_cafile
                capath = defaults.openssl_capath
                context.load_verify_locations(
                    cafile=cafile if cafile and Path(cafile).is_file() else None,
                    capath=capath if capath and Path(capath).is_dir() else None)
        else:
            context = ssl.create_default_context(cafile=valid[0] if valid else None)
        result = (context, {'passed': not failures, 'failures': failures})
        _CONFIGS[values] = result
        return result


def check():
    return deepcopy(configuration()[1])
