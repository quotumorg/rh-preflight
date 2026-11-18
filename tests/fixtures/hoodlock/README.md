# hoodlock captures

`lp-locks-2026-11-13.json` is the unmodified LP capture from local branch `hoodlock-057`.
`lp-locks-2026-11-18.json` is the unmodified supplied `/api/v1/lp-locks` response captured on 2026-11-18: 15 positions, nine HISS v4 positions. its timestamp drives offline replay, not the machine clock.

`card-lpbranch-hiss-1118.txt` and `cards-058-lock-usdg-1118.txt` are the unmodified supplied card text captures. replay uses the HISS card and the first (LOCK) card, retaining non-HoodLock lines. no raw token RPC or other source response is reconstructed from this text. the HISS answer uses the released `if Hermes Agent swapped this now` wording.

run `python3 -m demo.r6_replay --lp HISS` or `--lp LOCK` from the repository root. these commands open no socket. new LP lines use the same projection as live lookup; token and other source facts remain captured text. tests with synthetic RPC rows are explicitly constructed by `wire` in `test_hoodlock.py`.
