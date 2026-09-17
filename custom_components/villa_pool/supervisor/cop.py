"""COP model and thermal cost — DIAGNOSTIC ONLY (STORY §5.4).

There is exactly ONE measured point: **COP 2.82 at ~18 °C air**, from the night
of 16->17/9 (pool covered, pump 80 %, compressor pinned at 95 Hz = full load,
8.75 kW thermal). Everything else here is a model.

The slope is a Carnot-scaled guess (~3.5 %/°C) and is **LOW confidence**. It is
exposed so the owner can watch it against reality and so sessions can be logged
and the slope later fitted — it is NOT a decision input, and nothing in the
control law reads it. When it does become one, it will be after calibration,
and that will be a deliberate change with its own STORY entry.

Note also that full load is the *worst* COP regime for an inverter heat pump,
and H08 (max heating frequency) is not writable from HA — so the measured 2.82
is a floor for this machine, not its best.
"""
from __future__ import annotations

from ..const import (
    BAND_F1,
    BAND_F2,
    BAND_F3,
    COP_MAX,
    COP_MIN,
    COP_REF,
    COP_SLOPE,
    COP_T_REF,
)


def cop_estimate(air_temp: float | None) -> float | None:
    """COP_REF + COP_SLOPE * (T_air - T_REF), clamped to a physical range.

    Indicative: 10 °C -> ~2.0 · 18 °C -> 2.8 (measured) · 28 °C -> ~3.8.
    """
    if air_temp is None:
        return None
    cop = COP_REF + COP_SLOPE * (air_temp - COP_T_REF)
    return round(min(max(cop, COP_MIN), COP_MAX), 2)


def solar_share(headroom_w: float | None, pdc_power_w: float | None) -> float:
    """Fraction of the PdC's draw currently covered by PV headroom, 0..1."""
    if not headroom_w or not pdc_power_w or pdc_power_w <= 0:
        return 0.0
    return min(max(headroom_w / pdc_power_w, 0.0), 1.0)


def band_price(band: str | None, prices: dict[str, float]) -> float | None:
    """Energy component for a tariff band, or None if the band is unknown."""
    if band not in (BAND_F1, BAND_F2, BAND_F3):
        return None
    return prices.get(band)


def thermal_cost(
    band: str | None,
    prices: dict[str, float],
    air_temp: float | None,
    headroom_w: float | None = None,
    pdc_power_w: float | None = None,
) -> float | None:
    """EUR per thermal kWh: price(band) * (1 - solar_share) / COP_est.

    Returns None when the band or the air temperature is unknown — an estimate
    built on a guessed band would be worse than no estimate.
    """
    price = band_price(band, prices)
    cop = cop_estimate(air_temp)
    if price is None or not cop:
        return None
    share = solar_share(headroom_w, pdc_power_w)
    return round(price * (1.0 - share) / cop, 4)
