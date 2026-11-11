"""Sentinelhood ERC-20 identity and Pons launch history."""
from urllib.parse import urlencode
from ..extract import ADDRESS
from .common import plural, Keyless, Reading, field


class Client(Keyless):
    origin = 'https://sentinelhood.com'
    label = 'Sentinelhood'
    page = origin

    def target_page(self, target=None, **kwargs):
        return self.page + "/?ca=" + target if isinstance(target, str) and ADDRESS.fullmatch(target) else self.page

    def fetch(self, target, deadline):
        if not ADDRESS.fullmatch(str(target)):
            return Reading(self.label, 'unlisted', '')
        page = self.page + '/?ca=' + target
        data = self.read('/api/service?' + urlencode({'action': 'token', 'value': target}), deadline)
        if data['chainId'] != 4663:
            raise ValueError()
        if data['contract'] is False:
            return Reading(self.label, 'present', f'{self.label}: no contract at this address, block {field(data["block"])} · {page}')
        if data.get('erc20') is not True:
            return Reading(self.label, 'present', f'{self.label}: contract with no ERC-20 metadata, block {field(data["block"])} · {page}')
        pons = data.get('pons')
        if not pons:
            return Reading(self.label, 'present', f'{self.label}: ERC-20 {field(data["symbol"])}, block {field(data["block"])} · {page}')
        deployer = pons['deployer']
        if not ADDRESS.fullmatch(str(deployer)):
            raise ValueError()
        history = self.read('/api/service?' + urlencode({'action': 'deployer', 'value': target}), deadline)
        if history['chainId'] != 4663 or history['deployer'].lower() != deployer.lower():
            raise ValueError()
        count = history['launches']
        if type(count) is not int or count < 0:
            raise ValueError()
        short = deployer[:6] + '…' + deployer[-4:]
        return Reading(self.label, 'present', f'{self.label}: ERC-20 {field(data["symbol"])}, launched on pons by {short}, {count} {plural(count, "launch")} by that deployer, block {field(data["block"])} · {page}', deployer=deployer)
