"""The BRSR environmental capture taxonomy — categories, flows, units.

One definition, three consumers: the capture form renders from it, the seed
creates `BrsrEnvMetricLine` slots from it, and the rollup services aggregate
over it. Keeping it here rather than in three places is what stops the form
offering a category the rollup does not know how to total.

Category codes mirror the BRSR Principle 6 disclosure structure — energy split
by renewable/non-renewable and by carrier, water withdrawal by source and
discharge by destination, waste by SEBI's eight categories crossed with the
recovery and disposal operations. The `label` on each entry is the wording that
reaches the operator and the report.

⚠ These labels are drafted from the SEBI BRSR format as understood at build
time. They are data, not code — a wording correction is an UPDATE on
`BrsrIndicator` / a re-run of the seed, not a migration. Before a live filing,
have someone reconcile them against the current SEBI circular; the module
records `sebiFormatVersion` on every indicator precisely so that reconciliation
has something to compare against.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.brsr import (
    SLOT_NA,
    STREAM_EMISSIONS,
    STREAM_ENERGY,
    STREAM_WASTE,
    STREAM_WATER,
)

# ── flow types ──────────────────────────────────────────────────────────────
FLOW_WITHDRAWAL = "WITHDRAWAL"
FLOW_DISCHARGE = "DISCHARGE"
FLOW_CONSUMPTION = "CONSUMPTION"
FLOW_RECYCLED = "RECYCLED"

WASTE_GENERATED = "GENERATED"
WASTE_RECYCLED = "RECYCLED"
WASTE_REUSED = "REUSED"
WASTE_RECOVERED = "RECOVERED"
WASTE_INCINERATED = "INCINERATED"
WASTE_LANDFILLED = "LANDFILLED"
WASTE_OTHER_DISPOSAL = "OTHER_DISPOSAL"

# Recovery vs disposal, as BRSR groups them. Used by the waste-diversion
# calculation: diverted = recovered / generated.
WASTE_RECOVERY_FLOWS = (WASTE_RECYCLED, WASTE_REUSED, WASTE_RECOVERED)
WASTE_DISPOSAL_FLOWS = (WASTE_INCINERATED, WASTE_LANDFILLED, WASTE_OTHER_DISPOSAL)


@dataclass(frozen=True)
class CategoryDef:
    code: str
    label: str
    stream: str
    unit: str
    # Which flow types are valid for this category. (SLOT_NA,) for energy and
    # emissions, where the category alone is the full key.
    flows: tuple[str, ...] = (SLOT_NA,)
    # Only set for energy: whether this carrier counts toward the renewable
    # total. The rollup needs it; a label match would be fragile.
    isRenewable: bool | None = None
    # Only set for emissions categories that a factor can be applied to.
    factorType: str | None = None
    scope: str | None = None
    guidance: str | None = None


# ── ENERGY (P6 Essential Indicator 1) ───────────────────────────────────────
# Renewable / non-renewable × electricity / fuel / other, as the amended format
# splits it. Totals A/B/C in the disclosure are sums over these.
ENERGY_CATEGORIES: tuple[CategoryDef, ...] = (
    CategoryDef(
        "ELECTRICITY_RENEWABLE", "Electricity consumption from renewable sources",
        STREAM_ENERGY, "GJ", isRenewable=True,
    ),
    CategoryDef(
        "FUEL_RENEWABLE", "Fuel consumption from renewable sources",
        STREAM_ENERGY, "GJ", isRenewable=True,
    ),
    CategoryDef(
        "OTHER_RENEWABLE", "Energy consumption through other renewable sources",
        STREAM_ENERGY, "GJ", isRenewable=True,
    ),
    CategoryDef(
        "ELECTRICITY_NON_RENEWABLE", "Electricity consumption from non-renewable sources",
        STREAM_ENERGY, "GJ", isRenewable=False,
    ),
    CategoryDef(
        "FUEL_NON_RENEWABLE", "Fuel consumption from non-renewable sources",
        STREAM_ENERGY, "GJ", isRenewable=False,
    ),
    CategoryDef(
        "OTHER_NON_RENEWABLE", "Energy consumption through other non-renewable sources",
        STREAM_ENERGY, "GJ", isRenewable=False,
    ),
)

# ── WATER (P6 Essential Indicators 3 & 4) ───────────────────────────────────
WATER_CATEGORIES: tuple[CategoryDef, ...] = (
    CategoryDef("SURFACE_WATER", "Surface water", STREAM_WATER, "kL",
                flows=(FLOW_WITHDRAWAL, FLOW_DISCHARGE)),
    CategoryDef("GROUNDWATER", "Groundwater", STREAM_WATER, "kL",
                flows=(FLOW_WITHDRAWAL, FLOW_DISCHARGE)),
    CategoryDef("THIRD_PARTY_WATER", "Third party water", STREAM_WATER, "kL",
                flows=(FLOW_WITHDRAWAL, FLOW_DISCHARGE)),
    CategoryDef("SEAWATER_DESALINATED", "Seawater / desalinated water", STREAM_WATER, "kL",
                flows=(FLOW_WITHDRAWAL, FLOW_DISCHARGE)),
    CategoryDef("OTHER_WATER", "Others", STREAM_WATER, "kL",
                flows=(FLOW_WITHDRAWAL, FLOW_DISCHARGE)),
    # Consumption and recycling are reported against the site as a whole rather
    # than per source, so they carry no source category of their own.
    CategoryDef("TOTAL_CONSUMPTION", "Total water consumption", STREAM_WATER, "kL",
                flows=(FLOW_CONSUMPTION,)),
    CategoryDef("RECYCLED_REUSED", "Water recycled / reused", STREAM_WATER, "kL",
                flows=(FLOW_RECYCLED,)),
)

# Water-discharge destinations. Paired with a WATER + FLOW_DISCHARGE line; the
# treatment level is captured per line as free text on `treatmentLevel`.
WATER_DISCHARGE_DESTINATIONS: tuple[tuple[str, str], ...] = (
    ("TO_SURFACE_WATER", "To surface water"),
    ("TO_GROUNDWATER", "To groundwater"),
    ("TO_SEAWATER", "To seawater"),
    ("TO_THIRD_PARTIES", "Sent to third parties"),
    ("TO_OTHERS", "Others"),
)

# ── WASTE (P6 Essential Indicator 9) ────────────────────────────────────────
# SEBI's eight categories (A–H). Each is captured as generated, then across the
# recovery and disposal operations.
_WASTE_FLOWS = (WASTE_GENERATED, *WASTE_RECOVERY_FLOWS, *WASTE_DISPOSAL_FLOWS)

WASTE_CATEGORIES: tuple[CategoryDef, ...] = (
    CategoryDef("PLASTIC_WASTE", "Plastic waste (A)", STREAM_WASTE, "MT", flows=_WASTE_FLOWS),
    CategoryDef("E_WASTE", "E-waste (B)", STREAM_WASTE, "MT", flows=_WASTE_FLOWS),
    CategoryDef("BIO_MEDICAL_WASTE", "Bio-medical waste (C)", STREAM_WASTE, "MT", flows=_WASTE_FLOWS),
    CategoryDef("CONSTRUCTION_DEMOLITION_WASTE", "Construction and demolition waste (D)",
                STREAM_WASTE, "MT", flows=_WASTE_FLOWS),
    CategoryDef("BATTERY_WASTE", "Battery waste (E)", STREAM_WASTE, "MT", flows=_WASTE_FLOWS),
    CategoryDef("RADIOACTIVE_WASTE", "Radioactive waste (F)", STREAM_WASTE, "MT", flows=_WASTE_FLOWS),
    CategoryDef("OTHER_HAZARDOUS_WASTE", "Other Hazardous waste (G)", STREAM_WASTE, "MT",
                flows=_WASTE_FLOWS),
    CategoryDef("OTHER_NON_HAZARDOUS_WASTE", "Other Non-hazardous waste (H)", STREAM_WASTE, "MT",
                flows=_WASTE_FLOWS),
)

# ── EMISSIONS (P6 Essential Indicators 5–7) ─────────────────────────────────
# Scope 1 and 2 are computed from an activity quantity × an emission factor
# wherever a factor applies, so the figure carries a citation rather than being
# a bare number someone typed. A site that only has a total can still enter it
# directly against the DIRECT_* categories with no factor.
EMISSION_CATEGORIES: tuple[CategoryDef, ...] = (
    CategoryDef("GRID_ELECTRICITY", "Purchased grid electricity", STREAM_EMISSIONS, "kWh",
                factorType="ELECTRICITY_GRID", scope="SCOPE_2",
                guidance="Scope 2. Multiplied by the CEA grid emission factor."),
    CategoryDef("DIESEL", "Diesel (DG sets, owned vehicles)", STREAM_EMISSIONS, "litre",
                factorType="LIQUID_FUEL", scope="SCOPE_1"),
    CategoryDef("PETROL", "Petrol (owned vehicles)", STREAM_EMISSIONS, "litre",
                factorType="LIQUID_FUEL", scope="SCOPE_1"),
    CategoryDef("FURNACE_OIL", "Furnace oil", STREAM_EMISSIONS, "litre",
                factorType="LIQUID_FUEL", scope="SCOPE_1"),
    CategoryDef("LPG", "LPG", STREAM_EMISSIONS, "kg",
                factorType="GASEOUS_FUEL", scope="SCOPE_1"),
    CategoryDef("NATURAL_GAS", "Natural gas / PNG", STREAM_EMISSIONS, "scm",
                factorType="GASEOUS_FUEL", scope="SCOPE_1"),
    CategoryDef("COAL", "Coal / lignite", STREAM_EMISSIONS, "tonne",
                factorType="SOLID_FUEL", scope="SCOPE_1"),
    CategoryDef("BIOMASS", "Biomass / briquettes", STREAM_EMISSIONS, "tonne",
                factorType="SOLID_FUEL", scope="SCOPE_1"),
    CategoryDef("REFRIGERANT_R22", "Refrigerant top-up (R22)", STREAM_EMISSIONS, "kg",
                factorType="REFRIGERANT", scope="SCOPE_1"),
    CategoryDef("REFRIGERANT_R410A", "Refrigerant top-up (R410A)", STREAM_EMISSIONS, "kg",
                factorType="REFRIGERANT", scope="SCOPE_1"),
    # Escape hatches: a site that already has an assured total enters it here.
    CategoryDef("DIRECT_SCOPE1_TOTAL", "Scope 1 total (entered directly)", STREAM_EMISSIONS,
                "tCO2e", scope="SCOPE_1",
                guidance="Use only when Scope 1 is already computed elsewhere. No factor applied."),
    CategoryDef("DIRECT_SCOPE2_TOTAL", "Scope 2 total (entered directly)", STREAM_EMISSIONS,
                "tCO2e", scope="SCOPE_2",
                guidance="Use only when Scope 2 is already computed elsewhere. No factor applied."),
)

ALL_CATEGORIES: tuple[CategoryDef, ...] = (
    *ENERGY_CATEGORIES,
    *WATER_CATEGORIES,
    *WASTE_CATEGORIES,
    *EMISSION_CATEGORIES,
)

BY_CODE: dict[str, CategoryDef] = {c.code: c for c in ALL_CATEGORIES}

# Categories whose value IS already tCO2e and must therefore never be multiplied
# by a factor.
DIRECT_TOTAL_CATEGORIES = frozenset({"DIRECT_SCOPE1_TOTAL", "DIRECT_SCOPE2_TOTAL"})


def categories_for_stream(stream: str) -> tuple[CategoryDef, ...]:
    return tuple(c for c in ALL_CATEGORIES if c.stream == stream)


def get(code: str) -> CategoryDef | None:
    return BY_CODE.get(code)


def is_valid_slot(code: str, flow_type: str, destination: str) -> bool:
    """Whether (category, flow, destination) is a slot the taxonomy defines.

    Enforced server-side at write. Without it the unique constraint would still
    hold but the form could persist a nonsense combination — waste 'landfilled'
    against an energy category — that no rollup would ever read again.
    """
    cat = BY_CODE.get(code)
    if cat is None:
        return False
    if flow_type not in cat.flows:
        return False
    if destination != SLOT_NA:
        # Only a water DISCHARGE line carries a destination.
        if cat.stream != STREAM_WATER or flow_type != FLOW_DISCHARGE:
            return False
        if destination not in {d for d, _ in WATER_DISCHARGE_DESTINATIONS}:
            return False
    return True


__all__ = [
    "CategoryDef",
    "ENERGY_CATEGORIES",
    "WATER_CATEGORIES",
    "WASTE_CATEGORIES",
    "EMISSION_CATEGORIES",
    "ALL_CATEGORIES",
    "BY_CODE",
    "DIRECT_TOTAL_CATEGORIES",
    "WATER_DISCHARGE_DESTINATIONS",
    "FLOW_WITHDRAWAL",
    "FLOW_DISCHARGE",
    "FLOW_CONSUMPTION",
    "FLOW_RECYCLED",
    "WASTE_GENERATED",
    "WASTE_RECYCLED",
    "WASTE_REUSED",
    "WASTE_RECOVERED",
    "WASTE_INCINERATED",
    "WASTE_LANDFILLED",
    "WASTE_OTHER_DISPOSAL",
    "WASTE_RECOVERY_FLOWS",
    "WASTE_DISPOSAL_FLOWS",
    "categories_for_stream",
    "get",
    "is_valid_slot",
]
