"""Exact, currency-aware money for the canonical accounting domain."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Dict, FrozenSet


# Active ISO 4217 currency codes used for monetary accounting.  This excludes
# testing, precious-metal, and unit-of-account codes: those are valid ISO codes
# but are not currencies accepted by this Money value object.
_ISO_CURRENCIES: FrozenSet[str] = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD
    BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE
    CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF
    GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS
    KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD
    MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR
    PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK
    SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD
    TWD TZS UAH UGX USD UYU UZS VED VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR
    ZMW ZWG
    """.split()
)

_ZERO_MINOR_UNITS: FrozenSet[str] = frozenset(
    "BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split()
)
_THREE_MINOR_UNITS: FrozenSet[str] = frozenset("BHD IQD JOD KWD LYD OMR TND".split())
_FOUR_MINOR_UNITS: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class Money:
    """An exact Decimal amount denominated in an active ISO 4217 currency.

    Values are normalized to the currency's minor-unit scale.  The class does
    not round: excess precision is refused because silently changing a journal
    amount is unsafe.
    """

    amount: Decimal
    currency: str

    ISO_CURRENCIES: ClassVar[FrozenSet[str]] = _ISO_CURRENCIES
    MINOR_UNITS: ClassVar[Dict[str, int]] = {
        **{code: 0 for code in _ZERO_MINOR_UNITS},
        **{code: 3 for code in _THREE_MINOR_UNITS},
        **{code: 4 for code in _FOUR_MINOR_UNITS},
    }

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("Money amount must be Decimal; floats and implicit coercion are refused")
        if not self.amount.is_finite():
            raise ValueError("Money amount must be finite")
        if not isinstance(self.currency, str):
            raise TypeError("Money currency must be an ISO 4217 string")
        if self.currency not in self.ISO_CURRENCIES:
            raise ValueError("Money currency must be an uppercase active ISO 4217 currency code")

        places = self.MINOR_UNITS.get(self.currency, 2)
        quantum = Decimal(1).scaleb(-places)
        try:
            normalized = self.amount.quantize(quantum)
        except InvalidOperation as exc:
            raise ValueError("Money amount cannot be represented at its currency scale") from exc
        if normalized != self.amount:
            raise ValueError(
                f"{self.currency} supports {places} minor-unit decimal places; rounding is refused"
            )
        object.__setattr__(self, "amount", normalized)

    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(Decimal(0), currency)

    @property
    def minor_units(self) -> int:
        return self.MINOR_UNITS.get(self.currency, 2)

    def to_canonical_dict(self) -> dict:
        return {"amount": format(self.amount, "f"), "currency": self.currency}


__all__ = ["Money"]
