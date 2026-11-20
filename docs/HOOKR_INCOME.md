# Offline Hookr income-source check

This check adds payer facts to a token card. It classifies each supplied inflow
allocation as `external payer`, `creator or deployer wallet`, `self-funded loop`,
or `unknown`. Creator/deployer funding and self-funding carry machine severity
`WARN` and a lowercase `warn:` card prefix. Other results are `INFO`, never PASS.
The existing user policy and its ask/go/stop decision remain independent.

The implementation is offline. It does not call Hookr, discover streams, fetch
transactions or verify a historical payer path against a node. The caller must
provide reconciled evidence. An API total or a transaction sender is insufficient.

## Calling it

With Python 3.10 or newer, from the repository root:

```sh
python3 -m rh_preflight.income --token 0x1111111111111111111111111111111111111111 < tests/fixtures/hookr-income/synthetic.json
```

Add `--json` for the machine result. No result emits zero bytes on stdout. Invalid
JSON exits 2 with a fixed error, without echoing the input. Input is capped at
2 million characters. No file path, URL, key or credential is accepted by this CLI.

The normal card factory accepts explicit evidence separately from HEY's payload:

```python
from rh_preflight.card import token_card

card = token_card(token_address, hey_payload, hookr_income=offline_evidence)
print(card.render())
facts = card.public().get('hookr_income')
```

Other card constructors may use `data['hookr_income']`. Network wrapping preserves
the result and prints it once. A payload field from HEY is not automatically
trusted as attribution. No new live read is scheduled by this patch.

## Evidence contract

`schema` must be `rh-preflight/hookr-income/v1`. The fixture is a complete example.
Only `income` uses the documented Hookr response shape. `identity`, `attributions`
and `funding` are this patch's local sidecar fields, not claimed Hookr API fields.

| Field | Meaning |
|---|---|
| `identity.chainId`, `token` | Chain 4663 and the exact card target. |
| `identity.tokenizer`, `generation` | Tokenizer address and `2026-10-29` or `pre-2026-10-29`. The caller verifies the deployment and generation. |
| `identity.observedBlock` | Must match the income window's `throughBlock`. |
| `identity.doors` | Nonempty list of verified doors belonging to this token's streams at the observation. Do not trust a door's self-description. |
| `identity.creator`, `deployer` | Verified project creator and token deployer. A shared factory or relay is not a project funding wallet. Missing identities cannot establish an external payer. |
| `identity.relatedWallets` | Known project-controlled wallets, including current and historical stream managers and agent owner/spending wallets applicable to the window. |
| `identity.relationshipsComplete` | Explicit caller assertion that those relationships were checked for the relevant history. Missing, false or malformed relations leave unrelated-looking payers unknown. |
| `income` | The `structuredContent` object from `hookr_income` for `subject.kind: token`. Account and agent aggregates are not token income. |
| `attributions[].line` | Exact match on source, token, transaction hash, block, timestamp, currency and raw amount. Duplicate matches are ambiguous. |
| `attributions[].allocations` | Portions of that line in raw units, after Hookr's leg. They must not exceed its amount. Any unassigned remainder stays unknown. |
| `funding` | Optional separately observed direct transfers or bonuses to the revenue token, with `source: direct-transfer` or `bonus`, `to: token`, and the same transaction/asset/amount/time fields as a line. These are explicitly excluded from door income. |
| `synthetic` | Set to true for constructed examples. Preserved in machine output and visibly labelled on the card. |

An allocation needs `basis: payer-trace`, `complete: true`, a positive `amount`,
and an ordered `path` of one to sixteen edges. Every edge supplies `from`, `to`,
`currency`, raw `amount`, `txHash` and `blockNumber`. Each edge must cover the
allocation in the same currency, connect to the next, and occur no later than
the observed delivery. The final edge must reach a verified door, or the token
itself for supplemental funding. Deposits can precede the delivery window.

The normalizer is responsible for proving that these edges funded this particular
delivery and that its allocations reflect the net delivery after the leg. A
historical transfer between two wallets does not by itself establish a funding
path. Router, settlement and swap attribution needs economic-payer evidence;
`tx-sender`, `claim`, and `lp-fees` bases remain unknown. Cross-currency paths are
unsupported. Do not synthesize one from matching symbols or equal dollar values.

A direct payer matching creator/deployer is the creator class. Project funds
reaching a door through an intermediary, a related agent paying its own door, and
a verified return of token/door funds are self-funded. The recorded payer is the
economic origin of the supplied path, never a settlement relay. An external
origin routed through a project collection wallet stays unknown: that wallet's
presence alone proves neither a subsidy nor commercial independence. A validated
payer path with no known project wallets is external only when relationship
coverage is explicit.
This says who paid within supplied evidence; it does not establish a customer,
commercial independence, recurring revenue, profit, or absence of hidden control.

## Accounting and silence

Door rows are already net of Hookr's leg. The check does not add API totals, daily
totals, credits, claims, recognized income or released income to them. It does
not sum currencies or price tokens. Metadata-free quantities print exact base
units and an address. Symbols are bounded, lowercased and sanitized.

Direct funding and bonuses are distinct rows, never rebranded as door income.
For the current generation, the revenue token itself is not accepted as its own
income currency: the captured docs say those transfers are refused or retired.
The underlying project's coin is a different asset. Legacy responses need the
caller to reconcile the older tokenizer's own events.

Absent evidence, unavailable income, a wrong token/chain/pin, no known stream,
missing lines, or an empty line list with no supplemental funding produces no
income text. Present payments with absent or invalid attribution produce unknown.
Malformed or duplicate rows make coverage partial; valid sibling rows survive.
`partial`, cut windows, omitted payments, excluded parts and the earliest record
time remain visible. A known creator payment still warns within partial coverage.
Coverage labelled complete applies only to supplied lines and supplied attribution.

## Captured sources and fixture limits

The offline source captures were supplied by the 20 November 2026 Hookr research:

- `https://hookr.fun/docs/integrations/income`: documented response example, raw
  units, window, 200-line cap, unavailable/partial semantics and excluded deposits.
- `https://hookr.fun/docs/revenue-tokenizer/integrate-indexers`: event emitters,
  per-door reconciliation, direct transfers, payer history and tokenizer boundary.
- `https://hookr.fun/docs/revenue-tokenizer/build-income-accounting`: bonuses,
  recognition versus release and the refusal of transfers of the token to itself.
- `https://hookr.fun/docs/mcp`: revenue identity/streams and read-tool descriptions.

The response example in the docs is itself explicitly made up. Its placeholder
token and abbreviated transaction hash are not live evidence. The fixture expands
them into synthetic identifiers and supplies synthetic payer paths. The main
case is 100 units of an underlying project's coin, funded by its creator during a
seven-day window. It is a regression scenario, not a finding about any live token.
No captured live payer traces were available. Live collection and historical
control verification remain future work.

Run the focused tests from a full checkout with:

```sh
python3 -B -m unittest discover -s tests -p test_hookr_income.py -v
```
