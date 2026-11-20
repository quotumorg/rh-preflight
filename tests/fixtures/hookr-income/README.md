# Synthetic Hookr income fixture

`synthetic.json` is wholly synthetic test evidence. It is not a captured API reply
or a verified live transaction. Its `income` structure was derived from the
explicitly made-up example in the locally captured Hookr income documentation.

Changes to that example: complete synthetic addresses and transaction hashes,
a seven-day window, and 100 units of a synthetic underlying project coin (`OWN`).
The revenue token and income currency have different addresses. The tokenizer
address is synthetic; the generation boundary comes from the captured indexer docs.
Identity and payer-path objects use the local contract in `docs/HOOKR_INCOME.md`.
The fixture uses a generic stream door; it does not assert a deployed agent-door
fee configuration or a live 100-unit net delivery.

The tests make explicitly synthetic variants for external payments, creator and
deployer payments, intermediary loops, related agent wallets, returned funds,
mixed sources, missing data, LP/claim ambiguity, direct funding and bonuses.
No keys, controlled wallets, live requests or monetary actions are involved. A test result says the classifier behaves as designed on those inputs.
