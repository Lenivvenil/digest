"""Article identity and feed-independent catalog values."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Article:
    title: str
    link: str
    description: str
    source: str
    category: str
    pub_date: datetime | None


def article_hash(title: str, link: str) -> str:
    return hashlib.md5(f"{title}|{link}".encode(), usedforsecurity=False).hexdigest()
