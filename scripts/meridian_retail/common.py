"""Shared bits for the Meridian Retail demo-tenant seed (scripts/meridian_retail).

Meridian Retail is modelled as a plant group in this database: 40 stores and
3 distribution centres, every row keyed by an `MR-` plant code or an
`@meridian-retail.in` email so the whole tenant can be found, re-seeded or
purged without touching Meridian Manufacturing (NW/SW) or anyone else.
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))

EMAIL_DOMAIN = "meridian-retail.in"
PASSWORD = "demo123"
PROFILE = "RETAIL"
PREFIX = "MR-"

# Deterministic randomness: re-running the seed yields the same demo story.
RNG = random.Random(20260923)


def conn():
    url = os.environ.get("DATABASE_URL_SYNC") or os.environ["DATABASE_URL"]
    for p in ("postgresql+psycopg2://", "postgresql+asyncpg://"):
        url = url.replace(p, "postgresql://")
    c = psycopg2.connect(url)
    psycopg2.extras.register_default_jsonb(c)
    return c


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(timezone.utc)


def days_ago(n: float) -> datetime:
    return now() - timedelta(days=n)


_HASH: str | None = None


def password_hash() -> str:
    global _HASH
    if _HASH is None:
        _HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=10)).decode()
    return _HASH


# ── Sites ───────────────────────────────────────────────────────────────────
# (locality, city, state, format)
STORES: list[tuple[str, str, str, str]] = [
    ("Andheri West", "Mumbai", "Maharashtra", "Hypermarket"),
    ("Powai", "Mumbai", "Maharashtra", "Supermarket"),
    ("Thane West", "Thane", "Maharashtra", "Hypermarket"),
    ("Vashi", "Navi Mumbai", "Maharashtra", "Supermarket"),
    ("Kothrud", "Pune", "Maharashtra", "Supermarket"),
    ("Viman Nagar", "Pune", "Maharashtra", "Hypermarket"),
    ("Wakad", "Pune", "Maharashtra", "Express"),
    ("Dharampeth", "Nagpur", "Maharashtra", "Supermarket"),
    ("Koramangala", "Bengaluru", "Karnataka", "Hypermarket"),
    ("Whitefield", "Bengaluru", "Karnataka", "Hypermarket"),
    ("Jayanagar", "Bengaluru", "Karnataka", "Supermarket"),
    ("HSR Layout", "Bengaluru", "Karnataka", "Express"),
    ("Hebbal", "Bengaluru", "Karnataka", "Supermarket"),
    ("Kuvempunagar", "Mysuru", "Karnataka", "Express"),
    ("Anna Nagar", "Chennai", "Tamil Nadu", "Hypermarket"),
    ("Velachery", "Chennai", "Tamil Nadu", "Supermarket"),
    ("OMR Sholinganallur", "Chennai", "Tamil Nadu", "Express"),
    ("RS Puram", "Coimbatore", "Tamil Nadu", "Supermarket"),
    ("Gachibowli", "Hyderabad", "Telangana", "Hypermarket"),
    ("Kukatpally", "Hyderabad", "Telangana", "Supermarket"),
    ("Banjara Hills", "Hyderabad", "Telangana", "Express"),
    ("Kakkanad", "Kochi", "Kerala", "Supermarket"),
    ("Sector 29", "Gurugram", "Haryana", "Hypermarket"),
    ("Golf Course Road", "Gurugram", "Haryana", "Express"),
    ("Sector 18", "Noida", "Uttar Pradesh", "Hypermarket"),
    ("Indirapuram", "Ghaziabad", "Uttar Pradesh", "Supermarket"),
    ("Saket", "New Delhi", "Delhi", "Hypermarket"),
    ("Rajouri Garden", "New Delhi", "Delhi", "Supermarket"),
    ("Dwarka Sector 12", "New Delhi", "Delhi", "Express"),
    ("Gomti Nagar", "Lucknow", "Uttar Pradesh", "Supermarket"),
    ("Malviya Nagar", "Jaipur", "Rajasthan", "Supermarket"),
    ("Vijay Nagar", "Indore", "Madhya Pradesh", "Hypermarket"),
    ("Arera Colony", "Bhopal", "Madhya Pradesh", "Express"),
    ("Satellite", "Ahmedabad", "Gujarat", "Hypermarket"),
    ("Vesu", "Surat", "Gujarat", "Supermarket"),
    ("Alkapuri", "Vadodara", "Gujarat", "Express"),
    ("Salt Lake Sector V", "Kolkata", "West Bengal", "Hypermarket"),
    ("Ballygunge", "Kolkata", "West Bengal", "Supermarket"),
    ("Sector 17", "Chandigarh", "Chandigarh", "Supermarket"),
    ("Patia", "Bhubaneswar", "Odisha", "Express"),
]

DCS: list[tuple[str, str, str, str]] = [
    ("Bhiwandi", "Thane", "Maharashtra", "West"),
    ("Hoskote", "Bengaluru Rural", "Karnataka", "South"),
    ("Farrukhnagar", "Gurugram", "Haryana", "North"),
]

STORE_AREAS = ["Sales Floor", "Stockroom", "Checkout & Entrance", "Back Office", "Cold Room"]
DC_AREAS = ["Racking Aisles", "Loading Dock", "HVAC Plant Room", "Electrical Room", "Pump Room"]


def store_code(i: int) -> str:
    return f"MR-S{i:03d}"


def dc_code(i: int) -> str:
    return f"MR-DC{i:02d}"


# Names follow the platform convention "<short name> — <description>": the
# Daily Brief strip and shortPlantName() show only the part before " — ".
def store_name(i: int) -> str:
    loc, city, _, fmt = STORES[i - 1]
    return f"Store {i:03d} {loc} — Meridian Retail {fmt}, {city}"


def dc_name(i: int) -> str:
    loc, city, _, region = DCS[i - 1]
    return f"{region} DC {loc} — Meridian Retail {region} Distribution Center"


def plant_map(cur) -> dict[str, str]:
    """code → id for every Meridian Retail plant."""
    cur.execute('select code, id from "Plant" where code like %s', (PREFIX + "%",))
    return dict(cur.fetchall())


def user_map(cur) -> dict[str, str]:
    """email → id for every Meridian Retail user."""
    cur.execute('select email, id from "User" where email like %s', ("%@" + EMAIL_DOMAIN,))
    return dict(cur.fetchall())
