"""Week 12: deductible, sub-limit and policy limit -- three different numbers, never one.

Juniors confuse them weekly (the brief), and so does a language model reading prose: the pack says
"sub-limit ... GBP 5,000,000", "deductible of GBP 250,000" and "policy limit of GBP 40,000,000" within sixty words of
each other. So the figures are not left to be *read out of* a passage by the model. They are a small table
(``data/limits_table.json``) resolved by code and returned as three separately named fields, each with its basis
("each and every loss", "annual aggregate", "any one occurrence"), the clause it comes from and the dates it applied.
``tests/test_limits.py`` checks every figure against the wording itself, so the table cannot drift from its source.

Three rules the resolver keeps:

* **date-aware** -- the flood sub-limit was GBP 2,500,000 until 31 May 2024 and GBP 5,000,000 from 1 June 2024;
* **no guessing a missing fact** -- the flood deductible depends on whether the site is in a Flood Zone. If the caller
  does not know, the answer is ``deductible = None`` with both candidates and ``unresolved = ["in_flood_zone"]``, not a pick;
* **no invented figure** -- where the wording on file states nothing for that date or peril the figure is ``None`` with
  ``stated = "not_stated"``, which a person sees as "ask", rather than a plausible number.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

__all__ = ["LimitFigure", "LimitsResult", "LimitsTable", "default_limits_path"]

Peril = Literal["flood", "other"]
Stated = Literal["stated", "inferred", "not_stated"]


def default_limits_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "limits_table.json"


@dataclass(frozen=True, slots=True)
class LimitFigure:
    name: Literal["deductible", "sub_limit", "policy_limit"]
    amount: int | None
    currency: str = "GBP"
    basis: str | None = None
    source: str | None = None
    stated: Stated = "not_stated"
    effective_from: date | None = None
    effective_to: date | None = None
    note: str = ""
    #: When a missing fact stopped a pick: every figure that might apply.
    candidates: tuple[LimitFigure, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "amount": self.amount,
            "currency": self.currency,
            "basis": self.basis,
            "source": self.source,
            "stated": self.stated,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "note": self.note,
            "candidates": [c.to_dict() for c in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class LimitsResult:
    product: str
    loss_date: date
    peril: str
    deductible: LimitFigure
    sub_limit: LimitFigure
    policy_limit: LimitFigure
    #: Facts the caller did not supply that the answer depends on (e.g. ``in_flood_zone``).
    unresolved: tuple[str, ...] = field(default_factory=tuple)


def _in(day: date, start: date | None, end: date | None) -> bool:
    return (start is None or start <= day) and (end is None or day <= end)


class LimitsTable:
    def __init__(self, products: dict[str, dict]) -> None:
        self._products = products

    @classmethod
    def from_file(cls, path: Path | None = None) -> LimitsTable:
        payload = json.loads((path or default_limits_path()).read_text(encoding="utf-8"))
        return cls(payload["products"])

    def products(self) -> list[str]:
        return sorted(self._products)

    def raw_figures(self, product: str) -> list[dict]:
        return list(self._products.get(product, {}).get("figures", []))

    @staticmethod
    def _figure(raw: dict, currency: str) -> LimitFigure:
        return LimitFigure(
            name=raw["name"],
            amount=int(raw["amount"]),
            currency=currency,
            basis=raw.get("basis"),
            source=raw.get("source"),
            stated=raw.get("stated", "stated"),
            effective_from=date.fromisoformat(raw["effective_from"]) if raw.get("effective_from") else None,
            effective_to=date.fromisoformat(raw["effective_to"]) if raw.get("effective_to") else None,
            note=raw.get("note", ""),
        )

    def resolve(
        self,
        product: str,
        loss_date: date,
        peril: Peril,
        in_flood_zone: bool | None = None,
        documents: Collection[str] | None = None,
    ) -> LimitsResult:
        """The three figures for a loss. ``documents`` are the wording files the policy was issued on: a figure is offered
        only if the document that states it is one of them (a policy issued on other wording gets ``not_stated``, not
        this product's numbers)."""
        entry = self._products.get(product)
        if entry is None:
            absent = [LimitFigure(name, None, note=f"no limits table for product {product!r}") for name in ("deductible", "sub_limit", "policy_limit")]
            return LimitsResult(product, loss_date, peril, *absent)
        currency = entry.get("currency", "GBP")
        unresolved: list[str] = []

        def pick(name: str, *, conditional: bool = False) -> LimitFigure:
            matches = [
                raw
                for raw in entry["figures"]
                if raw["name"] == name
                and (documents is None or raw.get("source_document") in documents)
                and raw.get("peril", "*") in ("*", peril)
                and _in(loss_date, date.fromisoformat(raw["effective_from"]) if raw.get("effective_from") else None,
                        date.fromisoformat(raw["effective_to"]) if raw.get("effective_to") else None)
            ]
            if conditional and any(raw.get("condition") for raw in matches):
                wanted = {True: "in_flood_zone", False: "not_in_flood_zone"}.get(in_flood_zone) if in_flood_zone is not None else None
                if wanted is None:
                    unresolved.append("in_flood_zone")
                    candidates = tuple(self._figure(raw, currency) for raw in matches if raw.get("condition"))
                    return LimitFigure(
                        name, None, currency,
                        note="depends on whether the insured location is within a Flood Zone, which was not supplied",
                        candidates=candidates,
                    )
                matches = [raw for raw in matches if raw.get("condition") in (None, wanted)]
            if not matches:
                return LimitFigure(
                    name, None, currency,
                    note=f"the wording on file states no {name.replace('_', ' ')} for a {peril} loss on {loss_date.isoformat()}",
                )
            # Several matches should not happen with a well-formed table; the most recent start date wins, deterministically.
            raw = sorted(matches, key=lambda r: r.get("effective_from") or "")[-1]
            return self._figure(raw, currency)

        deductible = pick("deductible", conditional=True)
        sub_limit = pick("sub_limit")
        policy_limit = pick("policy_limit")
        return LimitsResult(product, loss_date, peril, deductible, sub_limit, policy_limit, tuple(dict.fromkeys(unresolved)))
