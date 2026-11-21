"""Brand and prompt catalog.

The catalog is configuration, not code: prompts and brands come from CSV files
(seeded into the `config` schema) so adding a company or a thousand prompts is a
data change. Nothing in the pipeline refers to a specific brand.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _split(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split("|") if v.strip())


@dataclass(frozen=True)
class Brand:
    brand_id: str
    name: str
    categories: tuple[str, ...]
    # Matched case-insensitively on word boundaries.
    aliases: tuple[str, ...]
    # Common English words ("Square", "Wise", "Sift"): matched case-sensitively only.
    strict_aliases: tuple[str, ...] = ()
    domains: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    category: str
    text: str


def load_brands(path: Path | None = None) -> list[Brand]:
    path = path or DATA_DIR / "brands.csv"
    with path.open(newline="") as f:
        return [
            Brand(
                brand_id=row["brand_id"],
                name=row["name"],
                categories=_split(row["categories"]),
                aliases=_split(row["aliases"]),
                strict_aliases=_split(row["strict_aliases"]),
                domains=_split(row["domains"]),
            )
            for row in csv.DictReader(f)
        ]


def load_prompts(path: Path | None = None) -> list[Prompt]:
    path = path or DATA_DIR / "prompts.csv"
    with path.open(newline="") as f:
        return [Prompt(row["prompt_id"], row["category"], row["prompt"]) for row in csv.DictReader(f)]
