"""Money is integer **nano-USD** (1 USD = 1_000_000_000 nano-USD = 10⁻⁹ USD).

Integer fixed-point: exact, no float drift, trivial for atomic SQL `UPDATE`s.
Provider rates are published per-million-tokens at cent granularity (e.g.
$3.00/1M = 3,000 nano-USD/token), so they're exactly representable and
`cost = tokens * rate_nanos` is an exact integer — no rounding, no under-billing.

All money columns are `BigInteger` (range about +/-9.2e18 nano, i.e. +/-$9.2B);
the Python type is always `int` (never `float`). USD conversion happens only at
the display/invoice boundary, via the helpers here.

These are units and readings, not billing policy: a provider's hourly price,
a usage figure on a chat, an amount in a sentence all need them whether or not
a deployment bills, so they live beside the platform rather than inside the
billing package (which re-exports them for its own callers).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction

NANOS_PER_USD = 1_000_000_000
"""1 USD expressed in nano-USD."""

NANOS_PER_CENT = NANOS_PER_USD // 100
"""1 US cent in nano-USD (10,000,000). Stripe's money unit is the integer cent
(``amount_total`` / ``unit_amount``), so the prepaid top-up path mints credits at
this rate. The single source — the displayed credit count and the granted balance
both flow through :func:`cents_to_nanos`, so they can never drift."""

CREDITS_PER_USD = 1_000
"""User-facing display credits per USD (1 credit = $0.001). A scaled *display*
unit over the same nano-USD balances — never a storage or billing unit."""

MAX_BUDGET_USD = 1_000_000_000
"""The largest budget any allowance or cap field accepts, in whole USD: one
billion dollars per cycle. Far past any real budget, and an order of magnitude
inside the BigInteger the columns hold, so a figure the schema admits can never
overflow the row it lands in — the bound is a validation answer, not a 500."""

MAX_BUDGET_NANOS = MAX_BUDGET_USD * NANOS_PER_USD
""":data:`MAX_BUDGET_USD` in nano-USD, the unit the allowance routes take."""

_NANOS = Decimal(NANOS_PER_USD)
_PER_MILLION = Decimal(1_000_000)
_NANOS_PER_CREDIT = Decimal(NANOS_PER_USD // CREDITS_PER_USD)  # 1_000_000


def usd_to_nanos(usd: Decimal | int | str) -> int:
    """Convert a USD amount to integer nano-USD (exact to 9 decimal places)."""
    return int((Decimal(usd) * _NANOS).to_integral_value(rounding=ROUND_HALF_UP))


def cents_to_nanos(cents: int) -> int:
    """Convert integer US cents (Stripe's money unit) to exact nano-USD. The one
    place cents → nano-USD happens, so the prepaid amount shown on Checkout and the
    amount granted on fulfilment are arithmetically identical."""
    return cents * NANOS_PER_CENT


def per_million_to_nanos_per_token(usd_per_million: Decimal | int | str) -> int:
    """Convert a published per-1M-tokens USD price to nano-USD per token.

    e.g. ``per_million_to_nanos_per_token("3.00") == 3000`` ($3/1M).
    """
    return int(
        (Decimal(usd_per_million) * _NANOS / _PER_MILLION).to_integral_value(rounding=ROUND_HALF_UP)
    )


_MINUTES_PER_HOUR = 60


def usd_per_hour_to_nanos_per_minute(usd_per_hour: Decimal | int | str | float) -> int:
    """Convert a provider's hourly USD price to integer nano-USD per minute.

    e.g. ``usd_per_hour_to_nanos_per_minute("0.69") == 11_500_000``.

    Exact, and rounded half-up like :func:`usd_to_nanos`, so ``"2.15596251"``
    (35,932,708.5 nanos a minute) reads 35,932,709. Pass the provider's own
    string where there is one. A ``float`` is read from its shortest decimal
    ``str`` (``0.69`` reads ``"0.69"``), never from its binary value, which can
    sit a hair below a half. The division runs on a ``Fraction`` of the decimal,
    so no precision limit can move a value across a half. Invalid input raises
    what :func:`usd_to_nanos` raises (``InvalidOperation`` for text that is not
    a number or a signaling NaN, ``ValueError`` for a quiet NaN,
    ``OverflowError`` for an infinity).
    """
    usd = Decimal(str(usd_per_hour)) if isinstance(usd_per_hour, float) else Decimal(usd_per_hour)
    if usd.is_snan():
        # Decimal arithmetic traps a signaling NaN; Fraction would call it a plain NaN.
        raise InvalidOperation(f"signaling NaN is not a price: {usd_per_hour!r}")
    nanos = Fraction(usd) * NANOS_PER_USD / _MINUTES_PER_HOUR
    whole, remainder = divmod(abs(nanos.numerator), nanos.denominator)
    if 2 * remainder >= nanos.denominator:
        whole += 1
    return -whole if nanos < 0 else whole


def nanos_to_usd_str(nanos: int, *, places: int = 6) -> str:
    """Render nano-USD as a human USD string — display/invoice boundary only."""
    quantum = Decimal(10) ** -places
    usd = (Decimal(nanos) / _NANOS).quantize(quantum, rounding=ROUND_HALF_UP)
    return f"{usd:.{places}f}"


BELOW_ONE_CENT = "<$0.01"
"""What a nonzero amount smaller than half a cent reads as."""

NO_AMOUNT = "\u2014"
"""What an amount that is not a number reads as."""

_CENT = Decimal("0.01")


def usd_display(usd: Decimal | int | str | float) -> str:
    """A USD amount as a person reads it — the one user-facing money rule.

    Exactly two decimals grouped by thousands (``"$2,999.34"``), rounded half-up
    (ties away from zero) from the exact decimal value: a ``float`` is read from
    its shortest decimal ``repr`` (``1.005`` reads ``"$1.01"``), never from its
    binary value. A nonzero amount that rounds to zero cents reads ``"<$0.01"``
    (``"-<$0.01"`` when negative) so a real charge never reads as ``$0.00``;
    zero reads ``"$0.00"``; a value that is not a number reads ``"—"``. The web
    formatter (``packages/chat-model/src/money.ts``) applies the same rule, and
    both are held to ``packages/chat-model/src/moneyVectors.json``.
    """
    try:
        exact = Decimal(repr(usd)) if isinstance(usd, float) else Decimal(usd)
    except InvalidOperation:
        return NO_AMOUNT
    if not exact.is_finite():
        return NO_AMOUNT
    cents = exact.quantize(_CENT, rounding=ROUND_HALF_UP)
    if cents == 0:
        if exact == 0:
            return "$0.00"
        return f"-{BELOW_ONE_CENT}" if exact < 0 else BELOW_ONE_CENT
    return f"-${-cents:,.2f}" if cents < 0 else f"${cents:,.2f}"


def usd_label(nanos: int) -> str:
    """Nano-USD as the figure a sentence names — ``"$1,250.00"`` — for a
    refusal or a caption, never for arithmetic. See :func:`usd_display`."""
    return usd_display(Decimal(nanos) / _NANOS)


def nanos_to_credits(nanos: int) -> int:
    """Render nano-USD as integer user-facing display credits (ROUND_HALF_UP).

    The user-facing boundary only — real billing stays exact in nano-USD. Sub-
    credit amounts round, so this is lossy and must never feed back into billing.
    """
    return int((Decimal(nanos) / _NANOS_PER_CREDIT).to_integral_value(rounding=ROUND_HALF_UP))


def credits_to_nanos(credits: int) -> int:
    """Convert whole display credits to exact integer nano-USD (1 credit = $0.001).

    The inverse of :func:`nanos_to_credits` for whole-credit amounts — tier
    allotments, prepaid packs — and exact, since one credit is exactly
    ``NANOS_PER_USD // CREDITS_PER_USD`` nano-USD. Use it to DEFINE credit amounts
    that become real balances; never round user spend back through it.
    """
    return credits * (NANOS_PER_USD // CREDITS_PER_USD)


def usd_to_credits(usd: float | Decimal | int | str) -> int:
    """Render a USD amount as integer display credits (ROUND_HALF_UP).

    The display boundary for a client-side USD figure (e.g. a chat manifest's
    running ``cost_total``) on the credits-only user surface. A ``float`` is
    routed through ``str`` so its decimal value (``1.2339``), not its binary
    repr, is converted. Lossy display only — never feed back into billing.
    """
    return nanos_to_credits(usd_to_nanos(Decimal(str(usd))))
