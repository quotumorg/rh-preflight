## 0.5.14

the Hermes Agent hook now stays under 25 seconds, below the host's 30-second cutoff, so one slow answer no longer blocks later tool calls: network reads and any toll before-buy WAIT end within 20 seconds of hook entry, and the ledger waits for a busy sqlite writer at most 2 seconds and never past 24; a ledger still locked skips the history row and keeps the decision. `builder_card` and the mcp server keep the full `toll_wait_max_seconds`. `builder_card` rejects an `address` that is not 40 hex characters instead of reading it as a project name, and the tls warning names the unreadable `SSL_CERT_FILE` or `REQUESTS_CA_BUNDLE` variable without printing its path.

## 0.5.13

packaging only, no code change. the README carries the `mcp-name` line the official mcp registry checks, and a hand-run publish workflow uploads `rh-preflight-mcp` to pypi and the server to the registry through github oidc, with no stored tokens.

## 0.5.12

add a read-only mcp server over stdio: `python -m rh_preflight.mcp` or the `rh-preflight-mcp` command. it runs from any mcp client without Hermes Agent and has no runtime dependencies.
two tools: `builder_card` returns the same public card as the plugin tool; `tx_check` takes an unsigned `to` plus `data` or `value`, or a `tx_hash` on chain 4663, and returns `action` (`pass`, `ask`, `block` or `note`), `advisory: true`, the card and the rules. it never approves, signs or sends anything and keeps no ledger.
the mcp path and the transfer hook share one check, so both give the same card and decision. settings come from `RH_PREFLIGHT_<SETTING_NAME>` or `--setting name=value` with the plugin's validation. the optional Toll key is read only from `TOLL_API_KEY` in the server environment.
`server.json` is an unsubmitted registry draft; no package is published.

## 0.5.11

print Toll's own before-buy word beside coin reads for a bought non-Stock-Token address and builder/direct-address token cards, with a validated first reason, remaining count, WAIT seconds, read age and same-token report link.
send the optional partner key as `x-toll-pass`, or `Authorization: Bearer <key>` when configured, on tx checks and before-buy MCP reads. other header settings use `x-toll-pass`; doctor says so without printing the key. Toll confirmed both forms on 2026-11-19.
on WAIT, wait the stated seconds and read again, at most three calls in all. `toll_wait_max_seconds` defaults to 30, accepts 0 to 120 seconds and bounds both total sleeps and the extra read time; 0 keeps the previous WAIT line. when the next wait does not fit, print WAIT with its seconds. cards say how many waits happened. only the before-buy read gets extra time, including unattended runs; other sources keep their deadlines.
keep one before-buy reader per address in flight and parsed non-WAIT answers ten minutes per address separately from check_coin. concurrent callers get a whole answer or no line; WAIT is not cached. Stock Tokens never call it.
print validated tx actions as `Toll: ALLOW`, `Toll: WARN` or `Toll: BLOCK`. all words remain informational; no approval rule changes, including BLOCK.
concurrent reads of different addresses no longer share a striped lock, so one slow read cannot make another source look unreachable. HEY, Hookr and Toll in-flight reads are now keyed per address.

## 0.5.10

relay Trnch's verified flag and one-line summary with a validated token-page link per its licence; retain first-seen dates, kept-out categories and independent rugged or dumped facts.
keep the legacy line when new fields are absent; unknown stays unlisted even when verified. no approval rule changes.
batch requested token checks in groups of twenty under the existing source switch and deadline, matched by address; fall back to single checks on a failed batch and stop remaining reads on a rate limit. keep Trnch uncached.

## 0.5.9

read HoodLock's keyless LP API under the existing source switch, deadline and cache, preserving released token-lock paging and approval rules.
print informational future locked v2/v3/v4 positions grouped by pool, API USD when complete, earliest unlock and the validated proof link of the position that unlocks first.
remove the LP-not-read caveat after successful complete or labelled partial reads; keep it when skipped, failed or timed out.
accept v4's 32-byte pool ids and retain independent facts across malformed optional LP data, nested RPC responses and date failures.
say how many locked positions are in range and, when the API's pool liquidity agrees, what share of the pool's active liquidity they hold (rounded down); a pool with none in range says so.
name HoodLock's own v4 hook class on the line when a pool's hook is not reviewed by HoodLock and can act when liquidity is removed, or when the class is not one HoodLock documents.
count only positions held by HoodLock's Liquidity Locker, drop the LP lines when the API names another locker, and date an API snapshot older than an hour.

## 0.5.8

send Toll's tx check the time left before the tx deadline as `budgetMs` (whole milliseconds, less a 0.4 second round trip, within Toll's 300 to 30000 bounds), on the first attempt and on the one retry. Toll answers inside it, so a coin it has not read by then arrives as the existing `Toll has not read ... yet` line beside the completed simulation, where a cut connection gave `Toll: not simulated`. No setting, default, rule or dependency changes; with too little time left the request is sent as before.

## 0.5.7

print signature take amounts from Toll with exact base-unit conversion, signed limits, optional USD and recorded blocks; retain validated take facts in the ledger.
accept moved pulls across every validated take row as a completed signature simulation; label simulation blocks separately from take-read blocks and keep independent incomplete reads and Permit rules.
ask by default for positive signature take exposure, recorded as signature_take; an optional take_above_usd threshold replaces that ask and compares strictly above the summed USD value. Zero amounts, expired signatures and used nonces do not fire the rule; malformed reads use on_unchecked. Keep personal_sign, note mode, cron handling and hard stops.
check HEY's project dossier before attributing a resolved address; contextOnly shared contracts carry no project name, badge or ships, and failed dossier reads keep the builder card with an ownership-not-confirmed line.
page HoodLock token-lock details in batches of 20, at most 10 pages and 200 locks under one deadline; label capped totals, keep unobserved lock presence unknown and count the complete vesting index.
probe the readers' actual paths in doctor, including chain-id POSTs, Toll stock membership and HEY and Hookr fallbacks; leave Trnch unprobed without a token.
warn once per startup environment configuration about missing or unreadable TLS trust overrides, fall back to certifi or system trust with verification enabled and report the fix in doctor.
on Hermes Agent 0.23.0/0.23.1 with Tool Search on, a deferred plugin action tool invoked through the tool_call bridge skips pre_tool_call, so the rh-preflight gate does not see the real tool; run `hermes config set tools.tool_search.enabled off` or use a newer Hermes Agent with the fix (canary >= 20261115, PR #155527).

## 0.5.6

Send the optional Toll profile secret key as x-toll-pass by default.

Ask before Stock Token swaps above your stock_swap_above_usd threshold, off by default, using unambiguous base-unit amounts and Toll chain prices already read for the call.
Print the computed swap value and price read time; missing or ambiguous amount, decimals, price or read time adds a not-read line without firing the rule. Value only explicit root actions and direct calls entries, including supported transaction envelopes with decoded swap raw sides or captured exact-input calldata; nested examples, simulation hints and conflicting pair identities cannot supply a value.
Keep note mode, cron handling and hard stops unchanged; record stock_swap_above_usd with your triggered rules.

## 0.5.5

Make the HEY record cache configurable with hey_cache_seconds in seconds, default 600; 0 disables caching. Label cached HEY reads with their age on cards.
Cache HEY snapshot locks for at most sixty seconds or hey_cache_seconds, whichever is shorter; 0 disables cache reads and writes.
Keep card digests unchanged by the cached HEY read age while retaining the label on displayed and stored cards.

## 0.5.4

- Drop private Hermes Agent approval imports. Only public cron identity triggers the cron stop or allow_unattended exception. Outside detected cron, asks now return Hermes Agent approve directives instead of a plugin stop for absent or unreadable presence. Hermes Agent fails closed without a human or approval bridge unless its configured unattended or single-query policy permits the call. YOLO and approvals.mode: off now bypass these asks, even with allow_unattended: false; hard stops remain unconditional.

- Document the 0.5.3 upgrade command, preserved settings and ledger, new defaults and pinned-install behavior.

- Bound detector work and route unreadable or malformed writes through on_unchecked; normalize content-read names consistently and document the detector contract in one table.

- Preserve execution flags, structured quote-tool transactions and nested writes across read exclusions; explicit raw broadcasts use on_unchecked without retaining signed bytes.

- Keep read-only quotes, allowance lookups, research examples and call/estimate RPC payloads out of transaction approval checks; preserve explicit sends/signatures and existing builder checks.

Unsigned transactions and wallet requests on chain 4663 receive a Toll simulation card before builder cards, with sender balance changes and our approval rules.
Local EIP-712 decoding identifies Permit, Permit2 and signing domains on another chain before any Toll request. Missing signing wallets use on_unchecked even if simulation succeeds.
Swap, approval and payment argument shapes without calldata retain local facts and use on_unchecked instead of reporting a completed check.
Unlimited approvals to spenders with no code stop the call, including unattended calls.
Transaction simulation defaults to on; payout prompts and incomplete-check handling have explicit settings.
Toll tx checks default to a separate 15-second budget, configurable up to 25, while builder reads run in parallel with their existing timeout.
Healthy tx requests get the full budget before a transport retry; rate-limit cooldowns never retry.
Fourteen stable Toll codes project fixed facts and local rules, including approval-for-all stops, delegation prompts and incomplete signature handling.
Coin reads print neutral completeness facts; unread coins add no default rule and partial reads use on_unchecked.
Sender balance changes use structured token symbols and decimals, with short addresses and base units as fallbacks.
Explicit and Toll-decoded v4 hooks use Hookr MCP for hook kind, a numeric automated screen and up to three fixed finding labels; score verdicts and prose never enter cards or rules.
Hookr scans cache each hook for ten minutes and permit at most six fresh scans per minute; rate limits leave only the public scan link.
HookrRouter exact-input trades decode PoolKey and poolId locally and check the sender alongside Toll within tx_timeout; unsupported selectors can resolve the first pool by bought token.
Hookr pool refusals ask by default under ask_hookr_refused; successful reads supply token symbols and decimals without replacing Toll simulation amounts.
The toll_coin_findings setting defaults to silent; line permits neutral attribution, fixed Toll figures and an available public coin link.
Failed or unknown started Hookr wallet checks use on_unchecked; Toll metadata takes precedence over Hookr metadata.
Transaction cards name every fired rule once, in the answer or below it, including rules summarized by +N more.
Internal errors stop calls in every mode; the README explains Hermes Agent directives and unattended behavior.

With toll_coin_findings: line, a token address that is not a Robinhood Stock Token gets Toll's free `check_coin` read: fixed Figures lines only (launch venue and age, maker's launch count, wallets on Toll's flag list and their share, what the maker holds now). Verdict words and finding prose are never copied. A still-reading answer, a timeout, 429, 5xx or a used-up daily quota prints nothing; a whole answer is kept ten minutes per address, maximum 512, independently of check_tx. Silent skips the free coin read. Stock Tokens never spend coin quota.

## 0.5.3

Cards added to JSON tool results keep the result valid JSON, so Hermes Agent Desktop still shows the command output.

## 0.5.2

Desktop colors the answer by outcome, draws an eleven-segment coverage bar and lists every source with its bucket and source link.
The CLI card colors the outcome word on a terminal, respecting `NO_COLOR` and redirected output.

## 0.5.1

Trnch joins as the tenth project, read live per check with a link to its token page.

Multi-target checks identify the target that triggered each rule.
rh-preflight by quotum with HEY Research Lab credits the co-author and distinguishes its builder record from rh-preflight decisions under the owner's rules.
Official Stock Token membership requires Toll's response address to match the requested address.
Desktop shows the recorded human response and keeps source details in a native disclosure.
A socket-free walkthrough shows a denied check, a fresh request on retry and an unattended stop.
The card says "Source published" for HEY's source flag; "verified" stays reserved for Verified Builder.
Desktop keeps each checked target visible when source details are closed.
Coverage distinguishes payment-required, rate-limited and refused source reads from unreachable reads.
