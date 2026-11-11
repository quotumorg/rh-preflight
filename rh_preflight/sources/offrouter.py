"""OffRouter's own manifest, only after exact HEY project resolution."""
from .common import Keyless, Reading, field, plural
from ..extract import ADDRESS


class Client(Keyless):
    origin = 'https://docs.offrouter.org'
    label = 'OffRouter'
    page = origin + '/.well-known/x402'
    probe_path = '/.well-known/x402'

    def fetch(self, target, deadline):
        manifest = self.read(self.probe_path, deadline)
        models = self.read('/models', deadline)['data']
        if not isinstance(models, list) or not isinstance(manifest['endpoints'], list):
            raise ValueError()
        payees = list(dict.fromkeys(a['payTo'] for e in manifest['endpoints'] for a in e.get('accepts', [])
                                   if a.get('network') == 'eip155:4663' and ADDRESS.fullmatch(a.get('payTo', ''))))
        payment = 'pays to ' + ', '.join(payees) + ' on eip155:4663 on Robinhood Chain' if payees else 'no payTo for eip155:4663 in manifest'
        return Reading(self.label, 'present', f"{self.label}: x402 mode {field(manifest['mode'])}, {payment}, {len(models)} {plural(len(models), 'model')} listed · {self.page}")
