# Hookr income-source check

The offline classifier accepts supplied income evidence. The optional live reader
collects bounded, read-only observations on chain 4663. Neither changes a user's
approval policy, signs, submits a transaction, or proves customer revenue or profit.
Use Python 3.11 or newer. This release is version 0.5.16.

```sh
python3 -m rh_preflight.income --token <token> < private-evidence.json
python3 -m rh_preflight.hookr_live --token <token> --days 7 --public
python3 -m rh_preflight.hookr_live --token <token> --days 7 --public --card
python3 -m rh_preflight.hookr_live --token <token> --days 7 --json
```

Live collection uses only the existing `ETHERSCAN_API_KEY` environment variable.
`--json` is explicitly private and takes precedence over `--public`. Text and
argument errors omit supplied wallet addresses and keys. Text may show the
requested token on a base card and transaction hashes. Upstream error prose is
never echoed. The offline CLI accepts at most 32 million input characters.

Ordinary cards do not schedule this reader. Pass evidence explicitly:

```python
from rh_preflight.card import token_card
card = token_card(token_address, hey_payload, hookr_income=evidence)
print(card.render())
```

`demo/live_card.py <token> --hookr-income` also opts in. Its other source reads
are outside these budgets. `read_income` and `classify` return private Python
objects; use the public projection for text or card JSON. CLI JSON stores the
raw sidecar under `private.evidence` and payer identities under `private`.

## Classes and warnings

- `creator or deployer wallet`: a literal creator/deployer/stream-manager sender,
  or authenticated prior direct native funding from such an EOA. A complete
  authenticated supplemental native payment can warn. A transfer relationship alone does not allocate
  a particular LP delivery.
- `external payer (approximate)`: a scoped negative observation, defined below.
  This approximate direct-transfer observation does not establish commercial
  independence or the economic owner of a router's input.
- `unknown`: incomplete reads, unverified origin, unsupported ABI/asset semantics,
  missing reconciliation, or graph-only return paths.
- `self-funded loop`: retained as a schema/class name for compatibility. This release
  does not issue this class from historical graph connectivity. Intervening
  spending, mixed balances, reused funds and distributor paths need a separate
  balance proof that this implementation does not supply.

All pool fee estimates are INFO, including literal creator swap senders.
`tx.from` is not proof of the economic input to a batch executor or router.
Only verified direct creator payment paths can produce `WARN` and `warn:`.
The shared offline classifier requires a single native `txlist` edge with
`nativeFundingVerified: true`, `complete: true`, `directFunding: true` and a
matching `destination`. Its transaction, block, timestamp, currency, destination
and full amount must bind the supplemental line. One native transaction can
support at most one exact allocation. These flags assert the collector's
independent transaction, success and historical-code checks; the offline
classifier performs no chain authentication itself.

Ordinary native history rows are deduplicated by transaction hash, ignoring
unsolicited trace/log indices. Conflicting copies remain unknown, never additive.
Internal traces use trace identity; token events use transaction/log identity.
Neither can establish an authenticated native payment. Old `complete: true`
paths, non-native paths and historical creator deposits reused for door income
are unknown with the fixed public reason `exact payment provenance unavailable`.
No origin label enables door balance attribution. CLI and cards use this same
policy; agent balance allocation remains unsupported.
Unallocated, unresolved and rounding amounts remain unknown. No result is PASS.

## Bounded direct funding rule

For each traced sender, let F and L be its first and last selected swaps.
Check `[F.time - N days, L.time]`, clamped at timestamp zero. N is
`--funding-lookback-days` (default 7). Resolve the lower block with Etherscan
`getblocknobytime`, `closest=after`. The descending history ends at L.block,
an actual selected swap block, and covers the whole bounded interval for that
sender. Funding must precede F to establish a prior creator relationship;
later funding can only prevent externality. Reuse this complete bounded scope
for that sender's selected swaps rather than reading a history per transaction.
Missing time/block bounds remain unknown. Separate supplemental payments end
at their own payment block.

The external payer (approximate) rule requires:

1. Known project identities and a sender with empty code at every selected swap
   block and its preceding block. v1 sealed streams do not require a nonexistent
   stream-manager field. Missing code remains unknown.
2. The sender is neither a literal project identity, known infrastructure, an
   identity hub, an observed swap transaction target nor a contract. Observed
   high fan-out remains hub evidence. Truncated sender history alone does not
   make an otherwise observed EOA's hub status unknown.
3. The project outgoing side is completely read over the bounded interval:
   native `txlist`, `txlistinternal` and `tokentx` for creator, deployer, stream
   manager, deployment sender, token, every door and drop box. Reads use the
   union of candidate ranges; every transfer is filtered to its sender's range.
   Etherscan has no destination filter, so filtering occurs locally. A capped,
   malformed or failed project history leaves the result unknown.
4. No transfer from any of those project origins to the sender in that range.
   Sender pages still supply leads, including common funders and project-origin
   paths. Any observed project-origin edge blocks externality, even if native
   authentication fails. Sender history completeness is no longer required for
   this depth-1 rule. `relationshipsComplete` is a result, not a second gate
   requiring full sender history.

The public rule wording is exactly:

`external payer (approximate): no direct transfer from the creator, deployer, door or token wallets to this payer in 7 days, all of their outgoing transfers read`

The same bounded rule applies to swap-fee senders and exact recapture payers.
An external payer (approximate) result requires at least seven days of project
history. Shorter configured lookbacks cannot establish this public rule.
Historical code observations apply only at the checked blocks. Amount
attribution also requires matched pool yield reconciliation; a relationship
cannot bypass that check. This is an approximate direct-transfer check.

Only independently matched successful native transactions establish a direct
creator-funding relationship: hash, block, sender, destination, value and success
must agree, with empty code at the funding block and preceding block. Arbitrary
ERC-20 Transfer events, internal traces, approvals or synthetic token logs cannot
promote a creator relationship. Any project-origin observation still prevents
an external payer (approximate) classification even when it cannot authenticate creator funding.
Same-block, later or expired funding does not establish funding before a swap.

No recursive upstream histories are required by this rule. Common funders and
old graph paths remain private leads. Depth zero disables external (approximate) inference;
positive `--depth` values retain CLI compatibility but use this one-hop rule.

## Income records and rHOOKR v1

The local cap is 5,000 income lines, 5,200 attribution records including
supplemental rows, and 400 allocations per line, or up to 20,400 when exact Donate legs are present
(20,000 scanned events plus 400 fee allocations). Duplicate Donate currency legs
cannot be attributed twice, including across different income lines. Projection above 5,000 adds
explicit omissions and marks partial; the offline classifier refuses larger
unprojected records. No currency totals are combined or converted through USD.

A response with exactly 200 lines or positive omissions triggers automatic narrowing. The reader
uses `tools/list` to discover `hookr_income`:

- If `from` and `through` are advertised, request consecutive one-day windows,
  validate their exact timestamps, token, omission/partial flags and row bounds,
  then merge and deduplicate into the original seven-day window.
- If anchored dates are not advertised, request the rolling `days: 1` window,
  retain any additional valid lines in the original range, and explicitly mark
  the seven-day record partial. Repeating a rolling-day query cannot recover
  earlier days. No cursor, offset or timestamp argument is invented.
- A daily response at the 200-line boundary, unsupported schema, failed page,
  local cap or call budget leaves daily coverage explicitly incomplete.

`live.incomePages` records the cap, requested subwindows and completion status.
Public text reports `income record capped at 200`, remaining omissions, and
`income daily coverage incomplete` when necessary. A ready seven-day aggregate
is never mistaken for complete line-level coverage.

Verified v1 ABIs were supplied for tokenizer
`0xfb3c1501c727f30883cd7b36ec380381af7412ef` and rHOOKR. The adapter derives all
selectors/topics from the deployed verified ABI, reads `streamsOf(token)`,
`doorCount()` and `doorAt(i)`, and matches StreamRecord.target to Door.target.
The latter tuple is `(kind, target, gas)`. The MCP `streams.streams[]` shape has
`kind`, `label`, `target`, `end` and no `door` or `count`; it is normalized without
assuming those missing fields. The supported static ABI decoder does not guess
nested dynamic layouts or proxy implementations.

Each stream carries `v1VerifiedView`. Failed views retain descriptions and
unknown income facts; they do not enable payer allocation. Token-emitted
`DoorPulled` maps verified door indexes, `IncomeRecognized` and `IncomeReleased`
remain ledger events, and `Synced` identifies synchronization including syncSome.
These events are never counted as additional transfers. Actual forwarding must
still be read from successful transactions. ABI views are current (`latest`),
not historical stream-ownership proof.

Fee-NFT `Pulled` fields are decoded only with that emitter's verified ABI.
The pools.trade `feeb` stream is deliberately excluded from market-LP estimates:
`fee nft upstream payer unavailable` identifies the missing NFT/beneficiary
accounting. Rules credit with a matching PoolManager-to-token movement is kept
separate from swap income. Its economic payer is unknown. No v2 delivery-leg
subtraction is applied to v1, whose Hookr leg was at mint.

## Swap coverage and reconciliation

Swap fees are approximate allocations by fee weight. Recapture is exact from
Donate in the same swap transaction.

Read PoolManager Swap logs over the report block range, splitting saturated
ranges and paging single saturated blocks. Each page has 1,000 rows; each scan
has a 20,000-row limit. Validate emitter, pool, hash, block, log index, signed
input and fee. Currency metadata comes from `hookr_trades({poolId})` or the
verified Initialize event; trade rows themselves do not supply swap history.

Sort income lines separately per currency by integer amount, largest first.
`--trace-share-bps` defaults to 8000. Select the shortest prefix covering at
least that fraction of the currency's reported window income. All delivery
transactions are still decoded, in value order, to retain collection boundaries
and the full reconciliation denominator; tail payer histories are skipped.
The tail stays unknown with the fixed reason `below trace threshold`.

Rank swap transactions by their integer contribution to those income lines,
using exact Donate amounts for recapture and the same fee-weight allocation
and forwarding bound as the classifier for swap fees.
Stop when the selected contribution covers the configured fraction of window
income in every currency, or eligible contributions/caps run out. Relative
per-currency shares merge the rankings without adding unrelated asset units.
`--max-swap-txs` defaults to 64 per pool, maximum 400. Sender histories end at the last selected swap block and retain the first
swap as their prior-funding boundary. Supplemental payment histories end at
their own payment block. The creator, deployer, stream manager, token, doors and drop box take
a pure identity comparison before a sender history. MCP address hints are
private observations and never authenticate a transaction or direct payment.

Unselected/unresolved weight remains in the denominator. Public text and cards
show `traced X% of income by value`, exact traced/window base units, external (approximate),
creator (verified), loop and unknown allocation counts, plus the threshold note.
Each currency also reports the income share covered by the external (approximate)
rule. `project outgoing read N/N` counts distinct project wallets with all three
history kinds read across every candidate interval. Older scopes without named
root coverage do not increase this count.
Observed creator identities in pool estimates have their own approximate count;
they never contribute to the verified-payment count or WARN. Selected value is
not classified value. Histories, caps or reconciliation can leave actual traced
coverage below the requested target. No percentage display decides a threshold.

Each currency has its own total. A native-weighted approximate total is shown
only when every non-native income currency has a direct native pair and a
same-run decoded Swap price inside the report window. The latest observed
`sqrtPriceX96` supplies an exact rational base-unit conversion; display is the
only rounded step. No USD price, stale quote or fabricated missing price is
used. If any price is absent, no cross-currency total is emitted.

Eligible forwarding is bounded by both the observed net fee-door movement and
the same-currency scanned fee proxy after subtracting the exact recapture
amount from the delivery budget. The fee proxy is never expanded to absorb
recapture or pre-window accrual. If exact Donate legs exceed the eligible
delivery budget, the line remains unknown rather than rescaling those amounts. Allocations use integer products,
floor division and exact residuals. Current position ownership, liquidity/range
history, router inputs and historical accrual boundaries are not reconstructed.

`hookr_pool_yield` arguments come from `tools/list`. The real input schema
advertises only required `{poolId}`. The reader passes exactly that argument.
It parses `result.structuredContent.income.windows[]` (the Reader already
unwraps structured content), independently of `apr.status`. Select the `7d`
entry or a unique entry matching the run's since/spanSeconds. Map `amount0` and
`amount1` in `swapFees`, `recapture`, `lpRewards` and `other` to the pool's sorted
currency0/currency1, never to quote/display order.

Each rolling start/end may differ from the report by at most 3,600 seconds
(one hour, 0.60% of seven days). Both signed offsets, the source through block,
through timestamp, quoteSide, tolerance and raw window are retained privately
in `yieldEvidence`. Outside that limit the reference is unavailable. A
`sinceOpen: true` entry can use the pool's whole life only when its computed
opening `throughTimestamp - spanSeconds` is inside the report window and
matches `lifetime.since`. This never extends observed log coverage. The older
explicit raw-currency contract with exact time/block bounds remains supported.
APR, USD, lpRewards and other income are not substituted for either fee lane.

Read PoolManager Donate logs through the same guarded, split/page transport
as Swap logs. A Donate currency leg is recapture only when that transaction
also has a valid Swap from the same pool and block. The donor from topic2 and
Swap event sender/router are separate observations. The payer is the matched
swap transaction's `tx.from`, authenticated by hash and block. A missing payer
transaction, orphan Donate or incomplete scan remains unknown with a reason.
Etherscan's `logIndex: "0x"` means index zero only; this alias does not apply to
amounts or block numbers.

`pool-donate-recapture-exact` allocations use one exact amount per Donate
currency leg. They use neither fee weights nor pro-rata scaling. Recorded
collection boundaries and forwarding still bound which income line can carry
a leg; they do not prove historic position entitlement. Payer relationships
remain approximate. Literal creator identities in this source stay INFO, as do
fee estimates. Only the separately verified direct-payment contract can WARN.

The reconciliation basis covers every observed delivery and swap before payer
selection, including unresolved and threshold-tail shares, plus the full Donate
sum. Reconcile allocated swap-fee estimates + Donate against yield swapFees +
recapture separately for each pool/currency. Fee and combined ratios use exact
Fraction arithmetic and the configurable fee tolerance (default 10%). The
Donate sum must equal yield recapture exactly, with zero amount tolerance;
a one-base-unit difference makes affected allocations unknown. A large
recapture amount cannot mask a fee-lane mismatch. Orphan Donate legs remain
unknown even if the pool totals match. Both unavailable and mismatched
reconciliation keep affected amounts unknown. Private reconciliation records
separate fee, Donate and combined totals. Exact direct transfers remain separate.

## Direct funding and agent doors

Supplemental transfers to the token, doors and configured drop box are excluded
from door-income totals. Drop calls require a same-transaction asset movement.
Only independently verified successful native sender payments can receive exact
creator classification. Token logs or internal traces alone remain unknown.
Origin graph paths are stored privately and never reused to allocate returns.
Supplemental payments are also sorted by value per currency before provenance
reads; their own threshold tails remain unknown and excluded from income totals.

Agent doors can record a matching plain native payment, ERC-20 calldata/log
observation, or verified-ABI self-funded EIP-3009 authorization. Relayed owners
and unknown wrappers stay unresolved. Untrusted token semantics cannot establish
an exact creator payment merely from its event. Agent payments remain separate
from door delivery. No history-derived full-sweep attribution is emitted:
transaction-hash ordering is not chronology, and histories cannot exclude
forced native sends, rebases, hooks, mixed balances or unobserved changes.
`agent balance interval unresolved` states this limit. Same-block graph edges
without consistent execution ordering cannot establish an exact path.

## Limits, transport and evidence contract

Defaults: 800 Etherscan attempts, 40 MCP attempts, 900 seconds per run, 10 seconds
per request, 64 selected swap transactions per pool, seven-day funding lookback,
five workers and an 80% per-currency trace-value target.
Overrides: `--max-etherscan` 0..4000, `--max-mcp` 0..40, `--run-seconds` up to
3600, `--max-swap-txs` 0..400, `--funding-lookback-days` 1..30,
`--hub-counterparties` 1..1000, `--depth` 0..4,
`--reconciliation-tolerance-bps` 0..10000, `--etherscan-rps` 0.1..8,
`--trace-share-bps` 1..10000 and `--workers` 1..16.

Etherscan defaults to 4 calls/s with no initial burst beyond one call.
All concurrent workers and retries share one run-scoped token bucket. Attempt
reservations are atomic and identical in-flight cache keys share one transport
read. Different cache keys can overlap, up to the configured worker count. Retry
rate-limit/HTTP 429, transient transport and server errors with 1/2/4-second
backoff, at most four total attempts. Longer transport cooldowns are honored.
MCP retains its 24-call burst, one call per two seconds, and three-attempt cap.
All attempts count against budgets; waiting cannot pass the run deadline.
Reads and terminal Etherscan failures are cached per run. Exact failing module,
action, safe request parameters and fixed reason are in private `live.failures`.
Fixed origins, no redirects/proxies, response-size bounds and nesting failure
handling remain in place. No request carries a signing/submission method.

Funding history uses descending pages of 50 rows per kind, `page=1`, with
`endblock` equal to the payment/swap block. Continue only while the oldest row
is within the lookback interval, at most three pages. A short page or a page
crossing before the lower bound establishes completeness for that kind; three
full in-window pages remain incomplete. All three kinds must be complete.
A malformed/conflicting page cannot establish an empty complete history.
Cache keys include action, address, endblock and page, plus query bounds.
Public cap lines omit addresses. Missing income windows explicitly render
`hookr income: unknown · income window unavailable`, when the source supplies no window.

The private budget estimate includes delivery transactions, selected swap hashes,
distinct sender history groups and project roots. It is a lower planning estimate;
pagination, supplemental provenance, ABI reads and retries add calls. At 4 rps,
2,000 attempts need at least 499.75 seconds of spacing; 4,000 need 999.75 seconds.
With five workers and 1.5-second responses, the latency floor is calls * 1.5 / 5.
Use the greater floor, plus MCP pacing and backoff. A ten-minute run is bounded
by `--run-seconds 600`; a budget or deadline returns explicit partial results.

The schema remains `rh-preflight/hookr-income/v1`, but evidence requirements
are stricter than 0.5.15. Older flags are not migration proofs.
`income` is Hookr's response shape; `identity`, `attributions`, `funding` and
`live` are local sidecar fields. Each attribution binds the full immutable line
key (source/token/transaction/block/time/currency/amount). Duplicate matches,
invalid rows or excess allocations remain unknown. Exact paths require strictly
increasing blocks, matching currencies, connected endpoints and sufficient raw
amounts. A plain connected return path never proves a loop.

For a bounded external payer (approximate), the normalizer supplies actual evidence such as
(the names below stand for validated project addresses):

```python
sender_evidence = {
    'sender': sender, 'relationshipDepth': 1, 'hubRules': 'eoa-no-hubs/v1',
    'beforeBlock': first_swap_block, 'beforeTime': first_swap_time,
    'lookbackSeconds': 7 * 86400, 'relationshipsComplete': True,
    'accounts': {sender: {'code': '0x', 'hub': False, 'historyComplete': False,
        'codeChecks': code_by_block}},  # Empty code at every selected swap and preceding block.
    'fundingScope': {
        'rule': 'project-outgoing-window/v2',
        'fromBlock': resolved_start_block, 'throughBlock': last_swap_block,
        'fromTime': first_swap_time - 7 * 86400, 'throughTime': last_swap_time,
        'candidateHistoryComplete': False, 'projectOutgoingComplete': True,
        'projectOutgoingRoots': [creator, deployer, manager, deployment_sender,
                                 token, door, drop_box],
        'directProjectFunding': False,
    },
}
```

These fields assert completed observations; callers must not manufacture them
from absence of a path. Regenerate an old sidecar with the new reader or retain
unknown. `nativeFundingVerified` similarly requires the independent checks
above. Graph-only loops and agent sweeps have no migration shortcut. A pool
allocation also needs `reconciliationOk: true` and explicit scanned fee weight
or the exact Donate/raw Swap/transaction evidence contract;
this never upgrades INFO to an economic-payer warning.

`rh_preflight.payer_attribution` is standard-library-only: `fee_weights`,
`hub_status`, `relationship` and `path_valid` consume normalized observations
without IO, card, CLI or transport dependencies.

## Offline validation

The public suite uses synthetic fixtures and transports with fake addresses and
transaction hashes, including the direct-payment and project-outgoing rule
regressions. Chain captures and capture-dependent replays remain private.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3 -m pytest -q -p socket_guard -p no:cacheprovider
```

The socket guard skips legacy loopback/host tests; those are not passes.

## Compatibility and selection limits

`project-outgoing-window/v2` asserts all listed project origins were read, and
`projectOutgoingRoots` records their coverage. `codeChecks` records actual
block-tagged reads. Older scopes with only identity-root histories and code at
the lookback boundary cannot assert the new rule. Offline replay keeps those
cases unknown; it never invents missing code, outgoing pages or reconciliation.
`fundingLeads` survive normalization and offline revalidation, so a previously
observed project-origin edge cannot be lost when evidence is rendered again.

Sender selection diagnostics are per pool and currency. `totalWeight` is the
whole currency's reported income, `availableWeight` is the observed integer
allocation proxy for that pool, and `selectedWeight` is its selected subset.
The target combines all pools within each currency; it is not a per-pool target
and is not a guarantee. Output reports `income outside observed swap fee
basis` when the proxy cannot reach the target, and `swap sender transaction cap
reached` when the per-pool hash cap also discards value. A pool with no selected
swaps no longer repeats another pool's global selected percentage. Unobserved
income may include earlier accrual or other sources; this check does not decide
which. Neither a larger trace threshold nor a sender classification inflates the
observed fee basis or supplies a missing yield match.
