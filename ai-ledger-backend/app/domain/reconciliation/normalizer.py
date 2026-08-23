import unicodedata
import re
from typing import Optional

def normalize_description(text: Optional[str]) -> str:
    """
    Deterministic description normalizer according to frozen reconciliation specifications:
    1. Unicode NFKC normalization
    2. Lowercase Latin characters
    3. Remove punctuation / symbols without merchant meaning (replace with space)
    4. Normalize full-width / half-width characters
    5. Collapse whitespace and strip
    6. Preserve meaningful digits (e.g. 'STARBUCKS 001' vs 'STARBUCKS 002')
    """
    if not text:
        return ""
    # 1. Unicode NFKC
    nfkc = unicodedata.normalize("NFKC", text)
    # 2. Lowercase
    lowered = nfkc.lower()
    # 3. Replace punctuation / symbols with space
    cleaned = []
    for ch in lowered:
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("S") or ch == "_":
            cleaned.append(" ")
        else:
            cleaned.append(ch)
    joined = "".join(cleaned)
    # 4. Collapse whitespace
    collapsed = re.sub(r"\s+", " ", joined).strip()
    return collapsed
