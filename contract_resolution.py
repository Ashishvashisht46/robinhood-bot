"""Resolve token identity from the call; chart pool IDs are not token IDs."""
import json
import re
import urllib.request
from urllib.parse import unquote, urlsplit

ADDRESS = r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])"
SYSTEM = {
    "0x" + "0" * 40,
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
    "0x8876789976decbfcbbbe364623c63652db8c0904",
    "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e",
}


def _unique(addresses):
    values = {a.lower(): a for a in addresses if a.lower() not in SYSTEM}
    return next(iter(values.values())) if len(values) == 1 else None


def resolve_pair(pair_id, ticker):
    """Lookup only the exact pair explicitly linked by the channel."""
    request = urllib.request.Request(
        f"https://api.dexscreener.com/latest/dex/pairs/robinhood/{pair_id}",
        headers={"User-Agent": "copytrader/2"})
    with urllib.request.urlopen(request, timeout=3) as response:
        pairs = json.load(response).get("pairs") or []
    candidates = []
    for pair in pairs:
        if pair.get("chainId", "").lower() not in ("robinhood", "robinhoodchain"):
            continue
        if pair.get("pairAddress", "").lower() != pair_id.lower():
            continue
        for side in ("baseToken", "quoteToken"):
            token = pair.get(side) or {}
            address = token.get("address", "")
            if token.get("symbol", "").upper() == ticker.upper() and re.fullmatch(ADDRESS, address):
                candidates.append(address)
    return _unique(candidates)


def extract_contract(text, entities, buttons, reply_markup, ticker):
    """Return (address, source). Ambiguity is explicit and cannot fall back to ticker."""
    tagged = re.findall(r"\b(?:ca|contract(?: address)?|token(?: address)?|address)\s*[:=]\s*(" + ADDRESS + r")", text, re.I)
    if tagged:
        return _unique(tagged), "explicit" if _unique(tagged) else "ambiguous"
    links = []
    for row in buttons or []:
        for button in row if isinstance(row, (list, tuple)) else [row]:
            value = getattr(button, "url", None) or getattr(button, "data", None)
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if isinstance(value, str):
                links.append(value)
    for row in getattr(reply_markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            value = getattr(button, "url", None)
            if value:
                links.append(value)
    links += [e.url for e in entities or [] if getattr(e, "url", None)]
    links += re.findall(r"https?://[^\s<>]+", text)
    tokens, pools = [], []
    for link in links:
        url = urlsplit(unquote(link))
        host = (url.hostname or "").lower().removeprefix("www.")
        path = url.path.lower()
        addresses = re.findall(ADDRESS, url.path)
        if host == "dexscreener.com" and path.startswith("/robinhood/"):
            pools.extend(addresses)
        elif ((host == "gmgn.ai" and "/robinhood/token/" in path)
              or (host == "geckoterminal.com" and "/robinhood/tokens/" in path)
              or (host == "robinhoodchain.blockscout.com" and path.startswith("/token/"))):
            tokens.extend(addresses)
    if tokens:
        return _unique(tokens), "token_link" if _unique(tokens) else "ambiguous"
    if pools:
        resolved = []
        for pool in dict.fromkeys(pools):
            try:
                address = resolve_pair(pool, ticker)
                if address:
                    resolved.append(address)
            except Exception:
                pass
        return _unique(resolved), "pair_lookup" if _unique(resolved) else "unresolved_pair"
    # Accept a standalone CA line. Wallets in the 'Live buys' section and
    # block-explorer /address links cannot become a token by accident.
    standalone = re.findall(r"^\s*`?(" + ADDRESS + r")`?\s*$", text, re.M)
    return _unique(standalone), "standalone" if _unique(standalone) else "missing"
