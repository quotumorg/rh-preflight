"""Agent Wormhole metadata observations, never a contract assessment."""
import re
from .common import Keyless, Reading, Absent, field, plural
from ..extract import ADDRESS


class Client(Keyless):
    origin = 'https://dashboard.agentwormhole.com'
    label = 'Agent Wormhole'
    page = origin

    def target_page(self, target=None, **kwargs):
        return self.page + "/t/4663/" + target if isinstance(target, str) and ADDRESS.fullmatch(target) else self.page

    def fetch(self, target, deadline, has_code=None):
        if not ADDRESS.fullmatch(target):
            return Reading(self.label, 'unlisted', '')
        page = self.page + '/t/4663/' + target
        try:
            data = self.read('/api/v1/token/4663/' + target, deadline)
        except Absent as error:
            if error.data.get('observed') is not False:
                raise
            data = error.data
        if data['observed'] is False:
            return Reading(self.label, 'missing', f'{self.label} has not observed this token; that is not a verdict · {page}')
        if data['observed'] is not True or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', data['verdict']):
            raise ValueError()
        if type(data['mutations']) is not int or data['mutations'] < 0 or type(data['attestation_current']) is not bool:
            raise ValueError()
        return Reading(self.label, 'present',
            f"{self.label}: token metadata `{field(data['verdict'])}` (rules), {data['mutations']} {plural(data['mutations'], 'change')} since "
            f"{field(data['first_seen'])[:10]}, attestation {'current' if data['attestation_current'] else 'expired'} · {page}")
