"""Recognize declared actions without executing or expanding their arguments."""

from dataclasses import dataclass
import re
import shlex
from urllib.parse import urlsplit

ADDRESS = re.compile(r"(?<![a-zA-Z0-9_])0x[0-9a-fA-F]{40}(?![a-zA-Z0-9_])")
URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
TERMINAL = re.compile(r"(?:^|__|_)(?:terminal|exec_command|shell|bash|run_command)(?:$|_)", re.I)


def walk(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from walk(item)
    elif isinstance(value, str):
        yield value


def addresses(args):
    return list(dict.fromkeys(m.group().lower() for s in walk(args)
                             for m in ADDRESS.finditer(s)))[:5]


def normalize_repo(url):
    """A repository root, with no credential, tree path, query or fragment."""
    if not isinstance(url, str):
        return None
    try:
        p = urlsplit(url.strip())
        if (p.scheme.lower() not in {"http", "https"} or p.hostname != "github.com"
                or p.username or p.password or p.port not in {None, 80, 443}
                or p.query or p.fragment):
            return None
        parts = p.path.strip("/").split("/")
        if len(parts) != 2:
            return None
        owner, repo = parts
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner):
            return None
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo) or repo in {".", ".."}:
            return None
        return f"https://github.com/{owner.lower()}/{repo.lower()}"
    except ValueError:
        return None


def robinhood_rpc(text):
    for match in URL.finditer(text):
        try:
            p = urlsplit(match.group())
            if p.hostname == "rpc.mainnet.chain.robinhood.com":
                return True
            if p.hostname == "rpc.nodeflare.app" and p.path.rstrip("/") == "/robinhood/public":
                return True
        except ValueError:
            pass
    return False


def _chain(value):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        return int(str(value), 16 if str(value).lower().startswith("0x") else 10)
    except (TypeError, ValueError):
        return None


def chain_context(args, assume_chain=None, command=""):
    declared = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower().replace("_", "") in {"chain", "chainid"}:
                    declared.append(_chain(item))
                else:
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(args)
    for match in re.finditer(r"--chain(?:-id)?(?:=|\s+)([^\s]+)", command):
        declared.append(_chain(match[1].strip("\"'")))
    # A conflicting or unreadable explicit chain is never overridden by a default or RPC.
    if declared:
        return all(v == 4663 for v in declared)
    return any(robinhood_rpc(s) for s in walk(args)) or assume_chain == 4663


@dataclass(frozen=True)
class Targets:
    addresses: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()


def extract(tool_name, args, settings):
    texts = list(walk(args))
    command = " ".join(texts) if TERMINAL.search(tool_name) else ""
    is_install = bool(re.search(r"hermes\s+(?:plugins\s+install|mcp\s+add)\b", command))
    is_install |= bool(re.search(r"(?:plugin|mcp).*(?:install|add)|(?:install|add).*(?:plugin|mcp)", tool_name, re.I))
    repos = []
    if is_install:
        repos = list(dict.fromkeys(repo for s in texts for m in URL.finditer(s)
                                   if (repo := normalize_repo(m.group()))))[:5]
    action = any(re.search(p, tool_name, re.I) for p in settings.tool_patterns)
    if command:
        try:
            tokens = shlex.split(command)
            action = any(tokens[i:i + 2] in (["cast", "send"], ["cast", "call"])
                         for i in range(len(tokens) - 1))
        except ValueError:
            action = bool(re.search(r"\bcast\s+(?:send|call)\b", command))
    found = addresses(args) if action and chain_context(args, settings.assume_chain, command) else []
    return Targets(tuple(found), tuple(repos))


def objects(value, depth=0):
    """Find structured content, including JSON embedded in literal shell text."""
    if depth > 24:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from objects(item, depth + 1)
    elif isinstance(value, str):
        for parsed in json_values(value):
            yield from objects(parsed, depth + 1)


def json_values(value):
    import json
    if len(value) > 1_000_000:
        return
    decoder = json.JSONDecoder()
    position = 0
    while position < len(value):
        start = re.search(r'[\[{]', value[position:])
        if not start:
            break
        position += start.start()
        try:
            parsed, consumed = decoder.raw_decode(value[position:])
        except ValueError:
            position += 1
            continue
        yield parsed
        position += consumed
    # A double-quoted shell argument carries escaped JSON quotation marks.
    # shlex removes only literal shell quoting; it never expands or executes.
    if '\\"' in value:
        try:
            for token in shlex.split(value):
                try:
                    parsed = json.loads(token)
                except ValueError:
                    continue
                if isinstance(parsed, (dict, list)):
                    yield parsed
        except ValueError:
            pass


def tx_intent(args, assume_chain=None):
    from .transactions import tx_intents
    return tx_intents(args, assume_chain)
