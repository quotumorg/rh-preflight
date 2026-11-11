"""User-owned policy settings."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import math
import re


DEFAULT_PATTERNS = [
    "swap|buy|sell|trade|approve|permit|transfer|pay|mint|bridge|deposit|stake|deploy|x402|liquidity|add_lp|remove_lp"
]


@dataclass(frozen=True)
class Settings:
    mode: str = "ask_when"
    quiet_days: int = 30
    ask_quiet: bool = True
    ask_unmeasured: bool = True
    ask_no_record: bool = True
    ask_not_verified: bool = False
    allow_unattended: bool = False
    assume_chain: int | None = None
    tool_patterns: list[str] = field(default_factory=lambda: DEFAULT_PATTERNS.copy())
    timeout: float = 4.0
    sources: dict = field(default_factory=dict)
    ask_when: dict = field(default_factory=dict)

    def __post_init__(self):
        known_sources = {"priors", "sherwood", "hookr", "chain", "wormhole", "toll", "dexscreener", "geckoterminal", "offrouter", "sentinelhood", "borro", "hoodlock", "trnch"}
        # Ignore retired or unknown source keys in existing user configs.
        object.__setattr__(self, "sources", {name: setting for name, setting in self.sources.items()
                                            if name in known_sources})
        for setting in self.sources.values():
            if setting not in {"on", "off"}:
                raise ValueError("sources must map source names to on or off")
        for name, setting in self.ask_when.items():
            if name not in {"payee_no_priors_record", "vault_strategy_inactive", "stock_no_hey_record"} or type(setting) is not bool:
                raise ValueError("ask_when must map rule names to booleans")
        if self.mode not in {"ask", "ask_when", "note"}:
            raise ValueError("mode must be ask, ask_when or note")
        if type(self.quiet_days) is not int or self.quiet_days < 1:
            raise ValueError("quiet_days must be a positive integer")
        if self.assume_chain not in {None, 4663}:
            raise ValueError("assume_chain must be 4663 or null")
        for name in ("ask_quiet", "ask_unmeasured", "ask_no_record",
                     "ask_not_verified", "allow_unattended"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (float, int)):
            raise ValueError("timeout must be a positive number at most 30 seconds")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 30:
            raise ValueError("timeout must be a positive number at most 30 seconds")
        if not isinstance(self.tool_patterns, list) or not all(
            isinstance(p, str) and len(p) <= 500 for p in self.tool_patterns
        ):
            raise ValueError("tool_patterns must be a list of short regex strings")
        for pattern in self.tool_patterns:
            re.compile(pattern, re.I)

    @classmethod
    def from_context(cls, ctx):
        defaults = cls()
        return cls(**{f.name: ctx.get_config(f.name, default=getattr(defaults, f.name))
                      for f in fields(cls)})
