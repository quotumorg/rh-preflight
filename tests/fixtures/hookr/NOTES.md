# Hookr MCP live captures (call, 2026-11-12 ~20:3xZ)

Endpoint `POST https://hookr.fun/api/mcp`, JSON-RPC, no key. Sources hq relayed from Hookr Discord: SDK hookr.fun/docs/pools/sdk (npm `hookr-sdk`), hookr.fun/integrations, hookr.fun/agents/SKILL.md. 24 tools (`tools.json`).

| file | call | time |
|---|---|---|
| init.json | initialize | 0.49 s |
| tools.json | tools/list | 0.66 s |
| hookr_hook.json | hookr_hook(address=0x89c9…6acc, Hookr shared root) | 1.11 s |
| hookr_scan.json | hookr_scan(address=0x89c9…6acc) | 4.13 s |
| pools-clear.json | hookr_pools(token=$CLEAR 0x41b5…bba0) → poolId 0xc3dc7a1c…8592 | 2.10 s |
| check-trade-0xdc36d5.json | hookr_check_trade(poolId, buy, 0.00048 ETH, wallet 0xdc36… = Toll clean-swap sender) → status ok, pays/gets with symbol+decimals (45650.58 CLEAR) | 0.42 s |
| check-trade-0x7fbd93.json | same, wallet 0x7fbd… (Toll payout fixture sender) → status refused, agent gate (no ERC-8004 identity) | 0.41 s |

Use:
- hookr_scan: print score number with «automated screening» next to it, findings `title` as facts. Drop `verdict`, `headline`, `score.band` (verdict words). `page` is the public link.
- hookr_hook: `kind` (shared-root / own hook from factory / other) decides catalog vs factory vs unknown.
- hookr_check_trade: second source for swaps through HookrRouter (0xc2bc…93c9): `refused` + reason = fact «Hookr: this pool's rules refuse this wallet (<reason first sentence>)» → ask; `ok` gives symbol and decimals → readable amounts. Needs poolId: from the swap calldata PoolKey if decodable, else hookr_pools by bought token.
