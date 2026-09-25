import re

_NOISE_TOKENS = [
    r"\bDES:\S*",
    r"\bID:\S*",
    r"\bINDN:[A-Z ]+",
    r"\bCO ID:\S*",
    r"\bCONF#\S*",
    r"\bCONFIRMATION#\S*",
    r"\bPPD\b",
    r"\bWEB\b",
    r"\bEPAY\b",
]
_TRAILING_STORE_NUMBER = re.compile(r"\s*#\d+$")
_TRAILING_REFERENCE_SUFFIX = re.compile(r"[*][A-Z0-9]{4,}$")
_WHITESPACE = re.compile(r"\s+")

# Curated aliases for cases generic cleanup can't unify — lexically unrelated
# strings for the same real-world merchant/obligation. Matched against the
# uppercased raw name/merchant_name and (optionally) the institution name.
# Patterns are deliberately narrow: e.g. the Amazon rule only catches
# marketplace order-line noise ("AMAZON MKTPL*...", "Amazon.com*..."), not a
# clean "Amazon Prime" (a distinct, flat-price subscription that should stay
# its own merchant so it can be recognized as a subscription, not lumped in
# with variable-amount marketplace purchases).
_ALIASES: list[tuple[str | None, re.Pattern, str]] = [
    (
        # Text-only match (no institution gate): Bilt's rent flow shows up under
        # three lexically different descriptions, and one of them — the real ACH
        # debit that actually moves cash — posts on the *bank's* account (e.g.
        # Bank of America), not the Bilt-issued card, so an institution filter
        # would silently miss exactly the transaction that matters most here.
        None,
        re.compile(r"BILT.*HOUSING|HOUSING.*BILT"),
        "Bilt Rent",
    ),
    (
        None,
        re.compile(r"\bUBER\b"),
        "Uber",
    ),
    (
        None,
        re.compile(r"AMAZON\.COM\*|AMAZON MKTPL"),
        "Amazon",
    ),
]


def _title_case(text: str) -> str:
    # str.title() capitalizes after apostrophes ("GELSON'S" -> "Gelson'S");
    # capitalizing per word (and per hyphenated part) avoids that.
    cap = lambda part: part[:1].upper() + part[1:].lower()
    return " ".join("-".join(cap(p) for p in word.split("-")) for word in text.split(" "))


_ZELLE = re.compile(
    r"^ZELLE (?:TRANSFER|PAYMENT (?:FROM|TO))\s+(?:CONF# ?\S+;\s*)?(?P<name>.+?)(?:\s+FOR \".*|;.*|\s+CONF#.*)?$",
    re.IGNORECASE,
)
_EFT_WITHDRAWAL = re.compile(r"^EFT \d{2}/\d{2} #\S+ WITHDRWL EFT", re.IGNORECASE)


def _special_formats(raw: str) -> str | None:
    """Bank-generated descriptions whose useful part generic cleanup can't find."""
    zelle = _ZELLE.match(raw.strip())
    if zelle:
        return f"Zelle · {_title_case(zelle.group('name').strip().upper())}"
    if "APPLE CASH" in raw.upper():
        return "Apple Cash"
    if _EFT_WITHDRAWAL.match(raw.strip()):
        return "ATM fee" if raw.strip().upper().endswith(" FEE") else "ATM withdrawal"
    return None


def _generic_cleanup(raw: str) -> str:
    text = raw.upper()
    # ACH descriptions are "<ORIGINATOR> DES:<type> ID:... INDN:<payee> CO ID:...";
    # the originator before DES: is the only part that names the counterparty.
    if " DES:" in text:
        text = text.split(" DES:", 1)[0]
    for pattern in _NOISE_TOKENS:
        text = re.sub(pattern, "", text)
    text = _TRAILING_REFERENCE_SUFFIX.sub("", text)
    text = _TRAILING_STORE_NUMBER.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return _title_case(text) if text else raw.strip()


def normalize_merchant(name: str, merchant_name: str | None, institution_name: str = "") -> str:
    raw = (merchant_name or name or "").strip()
    if not raw:
        return "Unknown"

    upper_raw = raw.upper()
    for institution_match, pattern, canonical in _ALIASES:
        if institution_match and institution_match.upper() not in institution_name.upper():
            continue
        if pattern.search(upper_raw):
            return canonical

    # Plaid's merchant_name is already clean and correctly cased ("OpenAI",
    # "CVS"); only the raw bank description needs generic cleanup.
    if merchant_name and merchant_name.strip():
        return _TRAILING_STORE_NUMBER.sub("", merchant_name.strip())
    return _special_formats(raw) or _generic_cleanup(raw)
