"""App categories. A channel appears in every category whose rule matches it: by its Dispatcharr group, or by a
name pattern (so MSG shows under Basketball and Hockey, ESPN under News & TV, UEFA slots under Soccer).
Override the whole list by pointing CATEGORIES_FILE at a JSON file with the same shape."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

DEFAULT_CATEGORIES = [
    {"id": "news-tv", "name": "News & TV", "groups": ["News & TV"],
     "names": [r"^US\| (ESPN HD|ESPN 2 HD|ESPN U|ESPN NEWS HD|FOX SPORTS 1 HD|FOX SPORTS 2 HD)$"]},
    {"id": "events", "name": "Events", "groups": ["Events"], "names": [r"^US\| (WWE HD|UFC HD|FIGHT NETWORK HD)$"],
     "exclude": [r"^UEFA\b"]},
    {"id": "baseball", "name": "Baseball", "groups": ["Baseball"],
     "names": [r"YES NETWORK|SPORTSNET NEW YORK|MLB NETWORK|MLB STRIKE ZONE"], "teams": ["Yankees"]},
    {"id": "football", "name": "Football", "groups": ["Football"], "teams": ["Jets", "Giants"]},
    {"id": "basketball", "name": "Basketball", "groups": ["Basketball"], "names": [r"^US\| MSG( 2)?( PLUS)? HD$"], "teams": ["Knicks"]},
    {"id": "hockey", "name": "Hockey", "groups": ["Hockey"], "names": [r"^US\| MSG( 2)?( PLUS)? HD$"],
     "teams": ["Devils", "Rangers", "Islanders"]},
    {"id": "soccer", "name": "Soccer", "groups": ["Soccer"], "names": [r"^UEFA\b", r"BEIN SPORTS HD|FOX SOCCER PLUS"]},
    {"id": "f1", "name": "F1", "groups": ["F1"]},
    {"id": "sports", "name": "Sports Networks", "groups": ["Sports Networks"]},
    {"id": "brazil", "name": "Brazil", "groups": ["Brazil"]},
]


@dataclass
class Category:
    id: str
    name: str
    groups: list[str] = field(default_factory=list)
    names: list[re.Pattern] = field(default_factory=list)
    exclude: list[re.Pattern] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)

    def matches(self, group: str | None, name: str) -> bool:
        if any(p.search(name) for p in self.exclude):
            return False
        return (group in self.groups) or any(p.search(name) for p in self.names)

    def featured(self, title: str) -> bool:
        """A favorite team is playing (word match, so "Giants" doesn't match "Giantsville")."""
        return any(re.search(rf"\b{re.escape(t)}\b", title, re.I) for t in self.teams)

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "teams": self.teams}


def load(path: str | None = None) -> list[Category]:
    raw = DEFAULT_CATEGORIES
    if path:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    return [
        Category(
            id=c["id"], name=c["name"], groups=c.get("groups", []),
            names=[re.compile(p, re.I) for p in c.get("names", [])],
            exclude=[re.compile(p, re.I) for p in c.get("exclude", [])],
            teams=c.get("teams", []),
        )
        for c in raw
    ]
