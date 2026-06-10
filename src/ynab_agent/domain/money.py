"""The ``Money`` value object.

YNAB encodes every amount as an integer count of *milliunits* — thousandths of
the currency unit, so ``$1.00`` is ``1000`` and ``-$4.73`` is ``-4730``. We
speak milliunits everywhere in the domain: integer arithmetic is exact, there is
no float to round, and there is zero conversion at the YNAB boundary.

Outflows are negative and inflows positive, mirroring YNAB.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

_MILLIUNITS_PER_UNIT = 1000


class Money(BaseModel):
    """An exact monetary amount in YNAB milliunits.

    Frozen and hashable, so it is safe to share and to use as a dict key. Prefer
    the constructors :meth:`from_milliunits` and :meth:`from_currency` over the
    raw initializer at call sites for readability.
    """

    model_config = ConfigDict(frozen=True)

    milliunits: int

    @classmethod
    def zero(cls) -> Money:
        """Return the zero amount."""
        return cls(milliunits=0)

    @classmethod
    def from_milliunits(cls, milliunits: int) -> Money:
        """Build from a raw milliunit count (the YNAB wire form)."""
        return cls(milliunits=milliunits)

    @classmethod
    def from_currency(cls, amount: Decimal | int | str) -> Money:
        """Build from a major-unit amount (e.g. dollars).

        The amount is scaled by 1000 and rounded half-to-even to the nearest
        milliunit, so fractional cents never silently accumulate.

        Args:
            amount: A major-unit value. ``str``/``int`` are parsed as exact
                decimals; never pass a binary ``float``.

        Returns:
            The corresponding :class:`Money`.
        """
        scaled = Decimal(amount) * _MILLIUNITS_PER_UNIT
        rounded = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
        return cls(milliunits=int(rounded))

    @property
    def currency_amount(self) -> Decimal:
        """The amount in major units (e.g. dollars), as an exact Decimal."""
        return Decimal(self.milliunits) / _MILLIUNITS_PER_UNIT

    @property
    def is_zero(self) -> bool:
        """Whether the amount is exactly zero."""
        return self.milliunits == 0

    @property
    def is_outflow(self) -> bool:
        """Whether the amount is negative (money leaving an account)."""
        return self.milliunits < 0

    def __add__(self, other: Money) -> Money:
        return Money(milliunits=self.milliunits + other.milliunits)

    def __sub__(self, other: Money) -> Money:
        return Money(milliunits=self.milliunits - other.milliunits)

    def __neg__(self) -> Money:
        return Money(milliunits=-self.milliunits)

    def __abs__(self) -> Money:
        return Money(milliunits=abs(self.milliunits))

    def __lt__(self, other: Money) -> bool:
        return self.milliunits < other.milliunits

    def __le__(self, other: Money) -> bool:
        return self.milliunits <= other.milliunits

    def __gt__(self, other: Money) -> bool:
        return self.milliunits > other.milliunits

    def __ge__(self, other: Money) -> bool:
        return self.milliunits >= other.milliunits

    def __str__(self) -> str:
        # Accounting style: the sign leads the symbol (-$13.07, never $-13.07).
        sign = "-" if self.milliunits < 0 else ""
        return f"{sign}${abs(self.currency_amount):.2f}"
