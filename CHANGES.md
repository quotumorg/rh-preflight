## Unreleased

Make the HEY record cache configurable with hey_cache_seconds in seconds, default 600; 0 disables caching. Label cached HEY reads with their age on cards.
Cache HEY snapshot locks for at most sixty seconds or hey_cache_seconds, whichever is shorter; 0 disables cache reads and writes.
Keep card digests unchanged by the cached HEY read age while retaining the label on displayed and stored cards.

## 0.5.4

- Drop private Hermes approval imports. Only public cron identity triggers the cron stop or allow_unattended exception. Outside detected cron, asks now return Hermes approve directives instead of a plugin stop for absent or unreadable presence. Hermes fails closed without a human or approval bridge unless its configured unattended or single-query policy permits the call. YOLO and approvals.mode: off now bypass these asks, even with allow_unattended: false; hard stops remain unconditional.

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
Internal errors stop calls in every mode; the README explains Hermes directives and unattended behavior.

With toll_coin_findings: line, a token address that is not a Robinhood Stock Token gets Toll's free `check_coin` read: fixed Figures lines only (launch venue and age, maker's launch count, wallets on Toll's flag list and their share, what the maker holds now). Verdict words and finding prose are never copied. A still-reading answer, a timeout, 429, 5xx or a used-up daily quota prints nothing; a whole answer is kept ten minutes per address, maximum 512, independently of check_tx. Silent skips the free coin read. Stock Tokens never spend coin quota.

## 0.5.3

Cards added to JSON tool results keep the result valid JSON, so Hermes Desktop still shows the command output.

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
