from __future__ import annotations

import base64
import heapq
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "lost_found.db"
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 7 * 1024 * 1024

# These IDs match the default select options in the supplied frontend.
CATEGORY_IDS = {"1": "Electronics", "2": "Phone", "3": "Wallet"}
ZONE_IDS = {"1": "Library", "2": "Canteen", "3": "Main Gate"}
COLOR_IDS = {"1": "Black", "2": "Brown", "3": "Navy"}

WEIGHTS = {
    "category": 0.25,
    "description": 0.20,
    "color": 0.15,
    "brand": 0.15,
    "location": 0.15,
    "date_time": 0.10,
}
DEFAULT_TAU = 0.70
DEFAULT_K = 5

CATEGORY_ALIASES = {
    "cellphone": "phone", "cell phone": "phone", "mobile phone": "phone", "smartphone": "phone",
    "mobile": "phone", "cp": "phone", "laptop computer": "laptop", "notebook computer": "laptop",
    "headset": "headphones", "earphones": "headphones", "earbuds": "headphones",
    "school bag": "backpack", "bagpack": "backpack", "schoolbag": "backpack",
    "purse": "wallet", "coin purse": "wallet", "id card": "identification card",
    "idcard": "identification card", "document": "documents", "paper": "documents",
}
CATEGORY_PARENT = {
    "phone": "electronics", "laptop": "electronics", "tablet": "electronics", "camera": "electronics",
    "headphones": "electronics", "charger": "electronics", "computer": "electronics", "watch": "accessories",
    "wallet": "personal accessories", "bag": "personal accessories", "backpack": "personal accessories",
    "purse": "personal accessories", "umbrella": "personal accessories", "keys": "personal items",
    "identification card": "documents", "documents": "documents",
}
COLOR_ALIASES = {
    "blk": "black", "jet black": "black", "matte black": "black", "wht": "white",
    "navy blue": "navy", "dark blue": "navy", "light blue": "blue", "bluish": "blue",
    "reddish": "red", "brownish": "brown", "grey": "gray", "silver gray": "silver",
}
COLOR_FAMILY = {
    "black": "dark", "navy": "dark", "charcoal": "dark", "gray": "neutral", "grey": "neutral",
    "silver": "neutral", "white": "light", "cream": "light", "beige": "light", "brown": "earth",
    "tan": "earth", "khaki": "earth", "red": "warm", "pink": "warm", "orange": "warm",
    "yellow": "warm", "blue": "cool", "green": "cool", "purple": "cool",
}
BRAND_ALIASES = {
    "hewlett packard": "hp", "hewlett-packard": "hp", "hewlett": "hp", "samsung electronics": "samsung",
    "apple inc": "apple", "apple iphone": "apple", "jbl by harman": "jbl", "casio computer": "casio",
    "unknown brand": "unknown", "n/a": "unknown", "none": "unknown", "not known": "unknown",
}
# Adjacent/related campus zones can be configured here as the actual campus map becomes available.
LOCATION_GROUPS = {
    "academic": {"library", "computer laboratory", "computer lab", "classroom", "academic building"},
    "food": {"canteen", "cafeteria", "food court", "student lounge"},
    "entrance": {"main gate", "front gate", "entrance", "guard house"},
}
STOP_WORDS = set("a an the and or but with without of to in on at near from for is was were are it this that my i found lost item has have had its very small large color colored brand brandnew sa ng ang mga yung ito siya niya po please pair piece one two yung ng ni mga".split())
SYNONYMS = {
    "cp": "phone", "cel": "phone", "cellphone": "phone", "cell phone": "phone", "mobile": "phone",
    "mobilephone": "phone", "smart phone": "phone", "bagpack": "backpack", "schoolbag": "backpack",
    "idcard": "identification", "identificationcard": "identification", "specs": "eyeglasses",
    "glasses": "eyeglasses", "earbuds": "earphones", "headset": "headphones", "blk": "black",
    "wht": "white", "navy blue": "navy", "bluish": "blue", "reddish": "red", "brownish": "brown",
    "wrist watch": "watch", "timepiece": "watch", "raincoat": "umbrella",
}


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS lost_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, zone TEXT NOT NULL,
            color TEXT NOT NULL, brand TEXT NOT NULL, event_datetime TEXT NOT NULL,
            description_raw TEXT NOT NULL, photo_data TEXT, photo_name TEXT, created_at TEXT NOT NULL,
            user_id TEXT, owner_name TEXT, contact_info TEXT, status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS found_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, zone TEXT NOT NULL,
            color TEXT NOT NULL, brand TEXT NOT NULL, event_datetime TEXT NOT NULL,
            description_raw TEXT NOT NULL, photo_data TEXT, photo_name TEXT, created_at TEXT NOT NULL,
            user_id TEXT, owner_name TEXT, contact_info TEXT, status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS idx_found_category_zone ON found_items(category, zone);
        CREATE INDEX IF NOT EXISTS idx_found_datetime ON found_items(event_datetime);
        CREATE INDEX IF NOT EXISTS idx_lost_created ON lost_items(created_at);
        CREATE INDEX IF NOT EXISTS idx_lost_event ON lost_items(event_datetime);
        """)
        # Migrate older databases created before owner/contact/profile support.
        for table in ("lost_items", "found_items"):
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, ddl in (
                ("user_id", "TEXT"),
                ("owner_name", "TEXT"),
                ("contact_info", "TEXT"),
                ("status", "TEXT NOT NULL DEFAULT 'pending'"),
            ):
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.execute("UPDATE lost_items SET status='pending' WHERE status IS NULL OR status='' ")
        conn.execute("UPDATE found_items SET status='pending' WHERE status IS NULL OR status='' ")


def clean_text(value: Any, field: str, max_len: int = 1500) -> str:
    if value is None:
        raise ValueError(f"{field} is required.")
    value = str(value).strip()
    if not value:
        raise ValueError(f"{field} is required.")
    if len(value) > max_len:
        raise ValueError(f"{field} must be {max_len} characters or fewer.")
    return value


def parse_datetime(value: Any, field: str = "Date and time") -> str:
    raw = clean_text(value, field, 40).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"{field} must be a valid date and time.")


def normalize_category(data: dict, found: bool = False) -> str:
    if found:
        return canonical_category(clean_text(data.get("category"), "Item category", 80))
    if data.get("category_other"):
        return canonical_category(clean_text(data["category_other"], "Other item category", 80))
    key = str(data.get("category_id") or "").strip()
    if key in CATEGORY_IDS:
        return CATEGORY_IDS[key]
    # Support frontends that submit the category label instead of its option ID.
    if data.get("category"):
        return canonical_category(clean_text(data["category"], "Item category", 80))
    raise ValueError("Item category is required. Select a category or enter Others.")


def normalize_zone(data: dict, found: bool = False) -> str:
    if found:
        return canonical_location(clean_text(data.get("zone"), "Location", 120))
    if data.get("zone_other"):
        return canonical_location(clean_text(data["zone_other"], "Other location", 120))
    key = str(data.get("zone_id") or "").strip()
    if key in ZONE_IDS:
        return ZONE_IDS[key]
    if data.get("zone"):
        return canonical_location(clean_text(data["zone"], "Location", 120))
    raise ValueError("Location is required. Select a location or enter Others.")


def normalize_color(data: dict, found: bool = False) -> str:
    if found:
        return canonical_color(clean_text(data.get("color"), "Color", 60))
    if data.get("color_other"):
        return canonical_color(clean_text(data["color_other"], "Other color", 60))
    key = str(data.get("color_id") or "").strip()
    if key in COLOR_IDS:
        return COLOR_IDS[key]
    if data.get("color"):
        return canonical_color(clean_text(data["color"], "Color", 60))
    raise ValueError("Color is required. Select a color or enter Others.")


def canonical_category(value: str) -> str:
    key = normalize_phrase(value)
    return CATEGORY_ALIASES.get(key, key.title())


def canonical_color(value: str) -> str:
    key = normalize_phrase(value)
    return COLOR_ALIASES.get(key, key).title()


def canonical_brand(value: str) -> str:
    key = normalize_phrase(value)
    return BRAND_ALIASES.get(key, key).upper() if key in {"hp", "jbl", "lg", "acer", "asus", "ibm"} else BRAND_ALIASES.get(key, key).title()


def canonical_location(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip())
    aliases = {"cafeteria": "Canteen", "food court": "Canteen", "front gate": "Main Gate", "entrance": "Main Gate", "computer lab": "Computer Laboratory"}
    return aliases.get(value.casefold(), value.title())


def normalize_phrase(value: str) -> str:
    value = value.casefold().strip()
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    return re.sub(r"\s+", " ", value)


def validate_photo(data: dict) -> tuple[str | None, str | None]:
    photo_data, photo_name = data.get("photo_data"), data.get("photo_name")
    unavailable = bool(data.get("photo_not_available"))
    if photo_data and unavailable:
        raise ValueError("Upload a photo or mark Photo not available, not both.")
    if not photo_data and not unavailable:
        raise ValueError("Upload an item photo or select Photo not available.")
    if photo_data:
        if not isinstance(photo_data, str) or not re.match(r"^data:image/(png|jpeg|webp);base64,", photo_data, re.I):
            raise ValueError("Only JPG, PNG, and WEBP images are accepted.")
        if len(photo_data) > 6_500_000:
            raise ValueError("The uploaded photo is too large. Maximum size is 5 MB.")
        try:
            base64.b64decode(photo_data.split(",", 1)[1], validate=True)
        except Exception as exc:
            raise ValueError("The uploaded photo data is invalid.") from exc
    return photo_data, str(photo_name)[:255] if photo_name else None


def tokenize(text: str) -> list[str]:
    text = normalize_phrase(text)
    # Expand multiword and single-token aliases before tokenization.
    for old in sorted(SYNONYMS, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(old)}\b", SYNONYMS[old], text)
    tokens = re.findall(r"[a-z0-9]+", text)
    return [token for token in tokens if token not in STOP_WORDS and len(token) > 1]


def tfidf_cosine(a: str, b: str, corpus: list[str]) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    docs = [tokenize(doc) for doc in corpus] + [ta, tb]
    n_docs = max(1, len(docs))
    df = Counter(token for doc in docs for token in set(doc))

    def vector(tokens: list[str]) -> dict[str, float]:
        counts = Counter(tokens)
        return {term: (1 + math.log(count)) * (math.log((1 + n_docs) / (1 + df[term])) + 1)
                for term, count in counts.items()}

    va, vb = vector(ta), vector(tb)
    dot = sum(value * vb.get(term, 0.0) for term, value in va.items())
    norm_a = math.sqrt(sum(value * value for value in va.values()))
    norm_b = math.sqrt(sum(value * value for value in vb.values()))
    return max(0.0, min(1.0, dot / (norm_a * norm_b))) if norm_a and norm_b else 0.0


def category_score(a: str, b: str) -> float:
    aa, bb = normalize_phrase(a), normalize_phrase(b)
    if aa == bb:
        return 1.0
    aa = CATEGORY_ALIASES.get(aa, aa)
    bb = CATEGORY_ALIASES.get(bb, bb)
    if aa == bb:
        return 1.0
    pa, pb = CATEGORY_PARENT.get(aa), CATEGORY_PARENT.get(bb)
    if pa and pa == pb:
        return 0.55
    return SequenceMatcher(None, aa, bb).ratio() * 0.35


def color_score(a: str, b: str) -> float:
    aa = COLOR_ALIASES.get(normalize_phrase(a), normalize_phrase(a))
    bb = COLOR_ALIASES.get(normalize_phrase(b), normalize_phrase(b))
    if aa == bb:
        return 1.0
    if COLOR_FAMILY.get(aa) and COLOR_FAMILY.get(aa) == COLOR_FAMILY.get(bb):
        return 0.55
    return 0.0


def brand_score(a: str, b: str) -> float:
    aa, bb = canonical_brand(a).casefold(), canonical_brand(b).casefold()
    if aa == bb:
        return 1.0
    if "unknown" in (aa, bb):
        return 0.35
    ratio = SequenceMatcher(None, aa, bb).ratio()
    return 0.65 if ratio >= 0.88 else 0.0


def location_score(a: str, b: str) -> float:
    aa, bb = normalize_phrase(canonical_location(a)), normalize_phrase(canonical_location(b))
    if aa == bb:
        return 1.0
    for group in LOCATION_GROUPS.values():
        normalized = {normalize_phrase(x) for x in group}
        if aa in normalized and bb in normalized:
            return 0.65
    # Distinct named zones receive a low but non-zero score because an item can be moved.
    return 0.20


def date_score(lost_dt: str, found_dt: str) -> float:
    lost = datetime.strptime(lost_dt, "%Y-%m-%d %H:%M:%S")
    found = datetime.strptime(found_dt, "%Y-%m-%d %H:%M:%S")
    delta_hours = (found - lost).total_seconds() / 3600.0
    if delta_hours < 0:
        return 0.0
    # Exponential decay: recent discoveries receive higher temporal similarity.
    return math.exp(-delta_hours / (24.0 * 7.0))


def score_pair(lost: dict, found: dict, corpus: list[str]) -> tuple[float, dict[str, float]]:
    parts = {
        "category": category_score(lost["category"], found["category"]),
        "description": tfidf_cosine(lost["description_raw"], found["description_raw"], corpus),
        "color": color_score(lost["color"], found["color"]),
        "brand": brand_score(lost["brand"], found["brand"]),
        "location": location_score(lost["zone"], found["zone"]),
        "date_time": date_score(lost["event_datetime"], found["event_datetime"]),
    }
    return sum(WEIGHTS[key] * parts[key] for key in WEIGHTS), parts


def upper_bound(lost: dict, category: str, zone: str | None = None) -> float:
    """Safe upper bound: unresolved attribute scores are optimistically set to 1.0."""
    known = {"category": category_score(lost["category"], category)}
    if zone is not None:
        known["location"] = location_score(lost["zone"], zone)
    return sum(WEIGHTS[key] * known.get(key, 1.0) for key in WEIGHTS)


def normalize_lost_payload(payload: dict) -> dict:
    return {
        "category": normalize_category(payload),
        "zone": normalize_zone(payload),
        "color": normalize_color(payload),
        "brand": canonical_brand(clean_text(payload.get("brand_raw", payload.get("brand")), "Brand", 100)),
        "event_datetime": parse_datetime(payload.get("event_datetime"), "Date and time lost"),
        "description_raw": clean_text(payload.get("description_raw"), "Item description", 1500),
    }


def run_search(payload: dict) -> dict:
    lost = normalize_lost_payload(payload)
    tau = float(payload.get("tau", DEFAULT_TAU))
    k = int(payload.get("k", DEFAULT_K))
    if not 0 <= tau <= 1:
        raise ValueError("Similarity threshold must be between 0 and 1.")
    if not 1 <= k <= 50:
        raise ValueError("Maximum results must be between 1 and 50.")

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM found_items ORDER BY id ASC").fetchall()
    all_found = [dict(row) for row in rows]
    corpus = [item["description_raw"] for item in all_found]
    eligible = [item for item in all_found if item["event_datetime"] >= lost["event_datetime"]]
    category_nodes: dict[str, list[dict]] = defaultdict(list)
    for item in eligible:
        category_nodes[item["category"]].append(item)

    # Hierarchical Greedy Best-First Search. Priority is h(n)=1-S_hat(n).
    frontier: list[tuple[float, int, int, str, str | None, str | None, dict | None, float]] = []
    sequence = 0
    heapq.heappush(frontier, (0.0, 0, sequence, "root", None, None, None, 1.0))
    trace: list[dict] = []
    results: list[dict] = []
    explored = 0
    pruned = 0

    while frontier and len(results) < k:
        priority, _depth_order, _seq, node_type, category, zone, item, estimate = heapq.heappop(frontier)
        explored += 1
        node_id = str(item["id"]) if item else (f"{category}/{zone}" if zone else category or "root")
        trace.append({"action": "expand", "node_id": node_id, "type": node_type,
                      "s_hat": round(estimate, 4), "h": round(1 - estimate, 4)})

        if node_type == "root":
            for cat in sorted(category_nodes):
                bound = upper_bound(lost, cat)
                if bound + 1e-12 < tau:
                    pruned += 1
                    trace.append({"action": "bound_prune", "node_id": cat, "type": "category",
                                  "s_hat": round(bound, 4), "h": round(1 - bound, 4)})
                    continue
                sequence += 1
                heapq.heappush(frontier, (1 - bound, -1, sequence, "category", cat, None, None, bound))
        elif node_type == "category":
            zones: dict[str, list[dict]] = defaultdict(list)
            for candidate in category_nodes.get(category, []):
                zones[candidate["zone"]].append(candidate)
            for zone_name in sorted(zones):
                bound = upper_bound(lost, category, zone_name)
                if bound + 1e-12 < tau:
                    pruned += 1
                    trace.append({"action": "bound_prune", "node_id": f"{category}/{zone_name}",
                                  "type": "zone", "s_hat": round(bound, 4), "h": round(1 - bound, 4)})
                    continue
                sequence += 1
                heapq.heappush(frontier, (1 - bound, -2, sequence, "zone", category, zone_name, None, bound))
        elif node_type == "zone":
            for candidate in category_nodes.get(category, []):
                if candidate["zone"] != zone:
                    continue
                actual, breakdown = score_pair(lost, candidate, corpus)
                if actual + 1e-12 < tau:
                    pruned += 1
                    trace.append({"action": "threshold_prune", "node_id": str(candidate["id"]), "type": "item",
                                  "s_hat": round(actual, 4), "h": round(1 - actual, 4),
                                  "breakdown": {key: round(value, 4) for key, value in breakdown.items()}})
                    continue
                sequence += 1
                heapq.heappush(frontier, (1 - actual, -3, sequence, "item", category, zone,
                                          {**candidate, "_score": actual, "_breakdown": breakdown}, actual))
        elif node_type == "item":
            breakdown = item["_breakdown"]
            results.append({
                "found_id": item["id"], "similarity": round(item["_score"], 6),
                "path_cost": round(1 - item["_score"], 6),
                "breakdown": {key: round(value, 4) for key, value in breakdown.items()},
                "weighted_contribution": {key: round(WEIGHTS[key] * breakdown[key], 4) for key in WEIGHTS},
                "category": item["category"], "zone": item["zone"], "color": item["color"],
                "brand": item["brand"], "description_raw": item["description_raw"],
                "photo_data": item["photo_data"], "event_datetime": item["event_datetime"],
                "owner_name": item.get("owner_name") or "Community reporter",
                "contact_info": item.get("contact_info") or "Contact through the platform",
                "status": item.get("status") or "pending",
                "is_sample": bool(item.get("photo_name") and str(item["photo_name"]).startswith("sample_")),
            })
            trace.append({"action": "goal", "node_id": str(item["id"]), "type": "item",
                          "s_hat": round(item["_score"], 4), "h": round(1 - item["_score"], 4)})

    # Keep the normal Best-First result set, but if nothing reached tau, return
    # the strongest weak candidates so the user can still see a possible match.
    if not results:
        weak = []
        for candidate in eligible:
            actual, breakdown = score_pair(lost, candidate, corpus)
            if actual <= 0:
                continue
            weak.append((actual, candidate, breakdown))
        weak.sort(key=lambda x: (-x[0], x[1]["id"]))
        for actual, candidate, breakdown in weak[:k]:
            results.append({
                "found_id": candidate["id"], "similarity": round(actual, 6),
                "path_cost": round(1 - actual, 6),
                "breakdown": {key: round(value, 4) for key, value in breakdown.items()},
                "weighted_contribution": {key: round(WEIGHTS[key] * breakdown[key], 4) for key in WEIGHTS},
                "category": candidate["category"], "zone": candidate["zone"], "color": candidate["color"],
                "brand": candidate["brand"], "description_raw": candidate["description_raw"],
                "photo_data": candidate["photo_data"], "event_datetime": candidate["event_datetime"],
                "owner_name": candidate.get("owner_name") or "Community reporter",
                "contact_info": candidate.get("contact_info") or "Contact through the platform",
                "status": candidate.get("status") or "pending",
                "is_sample": bool(candidate.get("photo_name") and str(candidate["photo_name"]).startswith("sample_")),
                "weak_match": True,
            })
    results.sort(key=lambda row: (-row["similarity"], row["found_id"]))
    return {"results": results[:k], "trace": trace, "nodes_explored": explored,
            "nodes_pruned": pruned, "threshold": tau, "weights": WEIGHTS,
            "eligible_found_records": len(eligible)}



def run_reverse_search(payload: dict) -> dict:
    """Match a newly reported found item against previously submitted lost items."""
    found = {
        "category": normalize_category(payload, found=True),
        "zone": normalize_zone(payload, found=True),
        "color": normalize_color(payload, found=True),
        "brand": canonical_brand(clean_text(payload.get("brand"), "Brand", 100)),
        "event_datetime": parse_datetime(payload.get("event_datetime"), "Date and time found"),
        "description_raw": clean_text(payload.get("description_raw"), "Item description", 1500),
    }
    tau = float(payload.get("tau", DEFAULT_TAU))
    k = int(payload.get("k", DEFAULT_K))
    if not 0 <= tau <= 1:
        raise ValueError("Similarity threshold must be between 0 and 1.")
    if not 1 <= k <= 50:
        raise ValueError("Maximum results must be between 1 and 50.")

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM lost_items ORDER BY id ASC").fetchall()
    all_lost = [dict(row) for row in rows]
    corpus = [item["description_raw"] for item in all_lost]

    results = []
    for lost in all_lost:
        # A found report should not match a lost report that happened after it.
        if lost["event_datetime"] > found["event_datetime"]:
            continue
        actual, breakdown = score_pair(lost, found, corpus)
        if actual + 1e-12 < tau:
            continue
        results.append({
            "found_id": lost["id"],
            "similarity": round(actual, 6),
            "path_cost": round(1 - actual, 6),
            "breakdown": {key: round(value, 4) for key, value in breakdown.items()},
            "weighted_contribution": {key: round(WEIGHTS[key] * breakdown[key], 4) for key in WEIGHTS},
            "category": lost["category"], "zone": lost["zone"], "color": lost["color"],
            "brand": lost["brand"], "description_raw": lost["description_raw"],
            "photo_data": lost["photo_data"], "event_datetime": lost["event_datetime"],
            "owner_name": lost.get("owner_name") or "Community reporter",
            "contact_info": lost.get("contact_info") or "Contact through the platform",
            "status": lost.get("status") or "pending",
            "is_sample": bool(lost.get("photo_name") and str(lost["photo_name"]).startswith("sample_")),
        })
    if not results:
        weak = []
        for lost in all_lost:
            if lost["event_datetime"] > found["event_datetime"]:
                continue
            actual, breakdown = score_pair(lost, found, corpus)
            if actual <= 0:
                continue
            weak.append((actual, lost, breakdown))
        weak.sort(key=lambda x: (-x[0], x[1]["id"]))
        for actual, lost, breakdown in weak[:k]:
            results.append({
                "found_id": lost["id"], "similarity": round(actual, 6),
                "path_cost": round(1 - actual, 6),
                "breakdown": {key: round(value, 4) for key, value in breakdown.items()},
                "weighted_contribution": {key: round(WEIGHTS[key] * breakdown[key], 4) for key in WEIGHTS},
                "category": lost["category"], "zone": lost["zone"], "color": lost["color"],
                "brand": lost["brand"], "description_raw": lost["description_raw"],
                "photo_data": lost["photo_data"], "event_datetime": lost["event_datetime"],
                "owner_name": lost.get("owner_name") or "Community reporter",
                "contact_info": lost.get("contact_info") or "Contact through the platform",
                "status": lost.get("status") or "pending",
                "is_sample": bool(lost.get("photo_name") and str(lost["photo_name"]).startswith("sample_")),
                "weak_match": True,
            })
    results.sort(key=lambda row: (-row["similarity"], row["found_id"]))
    return {
        "results": results[:k],
        "trace": [],
        "nodes_explored": len(all_lost),
        "nodes_pruned": max(0, len(all_lost) - len(results)),
        "threshold": tau,
        "weights": WEIGHTS,
        "eligible_lost_records": sum(1 for item in all_lost if item["event_datetime"] <= found["event_datetime"]),
    }


def potential_duplicate(data: dict, table: str) -> dict | None:
    """Find likely duplicate reports; user can explicitly confirm a separate report."""
    with get_db() as conn:
        rows = conn.execute(f"SELECT id,category,zone,color,brand,event_datetime,description_raw FROM {table} ORDER BY id DESC LIMIT 250").fetchall()
    incoming_dt = datetime.strptime(data["event_datetime"], "%Y-%m-%d %H:%M:%S")
    for row in rows:
        existing = dict(row)
        existing_dt = datetime.strptime(existing["event_datetime"], "%Y-%m-%d %H:%M:%S")
        if abs((incoming_dt - existing_dt).total_seconds()) > 24 * 3600:
            continue
        if category_score(data["category"], existing["category"]) < 0.8:
            continue
        if normalize_phrase(data["zone"]) != normalize_phrase(existing["zone"]):
            continue
        if color_score(data["color"], existing["color"]) < 0.5:
            continue
        text_similarity = tfidf_cosine(data["description_raw"], existing["description_raw"], [data["description_raw"], existing["description_raw"]])
        brand_similarity = brand_score(data["brand"], existing["brand"])
        if text_similarity >= 0.72 and brand_similarity >= 0.65:
            return {"id": existing["id"], "description": existing["description_raw"], "similarity": round(text_similarity, 3)}
    return None


def validate_reporter(data: dict) -> tuple[str, str, str]:
    user_id = clean_text(data.get("user_id"), "User ID", 120)
    owner_name = clean_text(data.get("owner_name"), "Name", 120)
    contact_info = clean_text(data.get("contact_info"), "Contact information", 180)
    return user_id, owner_name, contact_info


def create_report(data: dict, found: bool = False) -> int:
    category = normalize_category(data, found)
    zone = normalize_zone(data, found)
    color = normalize_color(data, found)
    brand = canonical_brand(clean_text(data.get("brand" if found else "brand_raw", data.get("brand")), "Brand", 100))
    event_dt = parse_datetime(data.get("event_datetime"), "Date and time found" if found else "Date and time lost")
    description = clean_text(data.get("description_raw"), "Item description", 1500)
    if len(description) < 5:
        raise ValueError("Item description must contain at least 5 characters.")
    photo_data, photo_name = validate_photo(data)
    user_id, owner_name, contact_info = validate_reporter(data)
    record = {"category": category, "zone": zone, "color": color, "brand": brand,
              "event_datetime": event_dt, "description_raw": description}
    table = "found_items" if found else "lost_items"
    if not data.get("allow_duplicate"):
        duplicate = potential_duplicate(record, table)
        if duplicate:
            raise DuplicateReport(duplicate)
    created = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cur = conn.execute(
            f"INSERT INTO {table} (category,zone,color,brand,event_datetime,description_raw,photo_data,photo_name,created_at,user_id,owner_name,contact_info,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (category, zone, color, brand, event_dt, description, photo_data, photo_name, created, user_id, owner_name, contact_info, "pending"),
        )
        return int(cur.lastrowid)


class DuplicateReport(Exception):
    def __init__(self, duplicate: dict):
        self.duplicate = duplicate
        super().__init__("A similar report already exists.")


def seed_demo_data() -> None:
    # LOST and FOUND feeds intentionally use different demo posts.
    # Some are related enough to demonstrate possible matching, but they are
    # separate reports and must never appear as the same post in both feeds.
    lost_samples = [
        {"file": "black_headphones.jpg", "category": "Electronics", "zone": "Library", "color": "Black", "brand": "JBL", "description": "Black JBL wireless headphones with a small silver logo. Last seen on a study table."},
        {"file": "brown_wallet.jpg", "category": "Wallet", "zone": "Canteen", "color": "Brown", "brand": "Leather", "description": "Small brown leather bifold wallet with card slots. Last seen near the dining tables."},
        {"file": "navy_backpack.jpg", "category": "Bags", "zone": "Main Gate", "color": "Navy", "brand": "Nike", "description": "Navy blue backpack with two front pockets and a small white logo."},
        {"file": "silver_phone.jpg", "category": "Phone", "zone": "Library", "color": "Silver", "brand": "Samsung", "description": "Silver Samsung smartphone with a clear case and a small scratch near one corner."},
                {"file": "black_watch.jpg", "category": "Watch", "zone": "Main Gate", "color": "Black", "brand": "Casio", "description": "Black Casio digital wristwatch with a rectangular face and black resin strap."},
    ]
    found_samples = [
        {"file": "found_blue_book.jpg", "category": "Book", "zone": "Gymnasium", "color": "Blue", "brand": "Oxford", "description": "Blue Oxford notebook with lined pages and a plain fabric cover, found on a gymnasium bench."},
        {"file": "found_green_bottle.jpg", "category": "Bottle", "zone": "Engineering Building", "color": "Green", "brand": "Hydro Flask", "description": "Green insulated water bottle with a silver cap, found beside a hallway bench in the engineering building."},
        {"file": "found_gray_jacket.jpg", "category": "Clothing", "zone": "Auditorium", "color": "Gray", "brand": "Uniqlo", "description": "Gray lightweight jacket with front pockets, found on a seat inside the auditorium."},
        {"file": "found_yellow_pouch.jpg", "category": "Stationery", "zone": "Computer Laboratory", "color": "Yellow", "brand": "Kokuyo", "description": "Small yellow stationery pouch containing pens and a highlighter, found beside a computer workstation."},
        {"file": "found_white_shoes.jpg", "category": "Shoes", "zone": "Covered Court", "color": "White", "brand": "Puma", "description": "Pair of white athletic shoes with a dark sole, found near the covered court bleachers."},
        {"file": "found_purple_tote.jpg", "category": "Bag", "zone": "Student Lounge", "color": "Purple", "brand": "Canvas", "description": "Purple canvas tote bag with a simple printed design, found on a lounge chair."},
{"file": "found_red_umbrella.jpg", "category": "Umbrella", "zone": "Canteen", "color": "Red", "brand": "Unknown", "description": "Folded red umbrella with a black curved handle, found beside the canteen entrance."},
    ]
    sample_names = ["Mika Santos", "Daniel Cruz", "Alyssa Reyes", "Noah Garcia", "Trisha Lopez", "Marco Dela Cruz"]
    sample_contacts = ["0987654321", "09171234567", "09221234567", "09351234567", "09481234567", "09561234567"]
    now = datetime.utcnow()
    with get_db() as conn:
        # Remove only old demo records. User-submitted reports are untouched.
        conn.execute("DELETE FROM lost_items WHERE photo_name LIKE 'sample_%'")
        conn.execute("DELETE FROM found_items WHERE photo_name LIKE 'sample_%'")

        for table, samples, direction in (
            ("lost_items", lost_samples, "lost"),
            ("found_items", found_samples, "found"),
        ):
            for index, sample in enumerate(samples):
                photo_name = "sample_" + direction + "_" + sample["file"]
                path = BASE_DIR / "sample_images" / sample["file"]
                image_data = "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii") if path.exists() else None
                dt = now - timedelta(days=6-index, hours=2) if direction == "lost" else now - timedelta(days=5-index, hours=1)
                conn.execute(
                    f"INSERT INTO {table} (category,zone,color,brand,event_datetime,description_raw,photo_data,photo_name,created_at,user_id,owner_name,contact_info,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sample["category"], sample["zone"], sample["color"], sample["brand"], dt.strftime("%Y-%m-%d %H:%M:%S"),
                     sample["description"], image_data, photo_name, now.strftime("%Y-%m-%d %H:%M:%S"),
                     "sample-community", sample_names[index], sample_contacts[index], "pending")
                )


def metric_summary(expected: list[str | None], predicted: list[str | None]) -> dict:
    tp = fp = fn = 0
    for exp, pred in zip(expected, predicted):
        if exp is not None and pred == exp:
            tp += 1
        elif exp is None and pred is not None:
            fp += 1
        elif exp is not None and pred != exp:
            fn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = sum(1 for exp, pred in zip(expected, predicted) if exp == pred) / max(1, len(expected))
    return {"true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1_score": round(f1, 4), "accuracy": round(accuracy, 4), "cases": len(expected)}


def benchmark_cases() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM found_items WHERE photo_name LIKE 'sample_%' ORDER BY id ASC").fetchall()
    samples = [dict(row) for row in rows]
    if len(samples) < 4:
        raise ValueError("The benchmark requires the six seeded sample found-item records. Keep the sample data or restore it before evaluating.")
    base = samples[0]
    partial = samples[3] if len(samples) > 3 else samples[0]
    future = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    return [
        {"case_id": "exact", "scenario": "Exact match", "expected_id": str(base["id"]),
         "query": {"category": base["category"], "zone": base["zone"], "color": base["color"], "brand": base["brand"], "event_datetime": (datetime.strptime(base["event_datetime"], "%Y-%m-%d %H:%M:%S") - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), "description_raw": base["description_raw"]}},
        {"case_id": "partial", "scenario": "Partial match", "expected_id": str(partial["id"]),
         "query": {"category": partial["category"], "zone": "Library", "color": partial["color"], "brand": partial["brand"], "event_datetime": (datetime.strptime(partial["event_datetime"], "%Y-%m-%d %H:%M:%S") - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), "description_raw": partial["description_raw"]}},
        {"case_id": "conflict", "scenario": "Conflicting description", "expected_id": None,
         "query": {"category": base["category"], "zone": "Canteen", "color": "bright yellow", "brand": "DifferentBrand", "event_datetime": future, "description_raw": base["description_raw"]}},
        {"case_id": "no_match", "scenario": "No match", "expected_id": None,
         "query": {"category": "Musical Instrument", "zone": "Gymnasium", "color": "Violet", "brand": "NoBrand", "event_datetime": future, "description_raw": "purple violin case with music sheets and a shoulder strap"}},
    ]


def score_all_candidates(query: dict, tau: float) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM found_items ORDER BY id ASC").fetchall()
    found = [dict(row) for row in rows]
    corpus = [row["description_raw"] for row in found]
    scored = []
    for row in found:
        if row["event_datetime"] < query["event_datetime"]:
            continue
        score, breakdown = score_pair(query, row, corpus)
        if score >= tau:
            scored.append({"id": str(row["id"]), "score": score, "breakdown": breakdown})
    return sorted(scored, key=lambda x: (-x["score"], int(x["id"])))


def keyword_prediction(query: dict) -> str | None:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM found_items ORDER BY id ASC").fetchall()
    q_tokens = set(tokenize(" ".join(str(query.get(k, "")) for k in ("category", "color", "brand", "description_raw"))))
    best_id, best_score = None, 0.0
    for row in rows:
        if row["event_datetime"] < query["event_datetime"]:
            continue
        candidate_tokens = set(tokenize(" ".join(str(row[k]) for k in ("category", "color", "brand", "description_raw"))))
        score = len(q_tokens & candidate_tokens) / len(q_tokens | candidate_tokens) if q_tokens | candidate_tokens else 0.0
        if score > best_score:
            best_id, best_score = str(row["id"]), score
    return best_id if best_score >= 0.10 else None


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "lost_and_found_ai_frontend.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "Lost & Found AI API", "database": str(DB_PATH.name)})


@app.post("/api/lost-items")
def submit_lost_item():
    data = request.get_json(silent=True) or {}
    try:
        item_id = create_report(data, found=False)
        return jsonify({"ok": True, "message": "Lost item report submitted and saved.", "id": item_id}), 201
    except DuplicateReport as exc:
        return jsonify({"error": "A similar lost-item report already exists. Submit again only if this is a separate report.", "duplicate": exc.duplicate}), 409
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/found-items")
def submit_found_item():
    data = request.get_json(silent=True) or {}
    try:
        item_id = create_report(data, found=True)
        return jsonify({"ok": True, "message": "Found item report submitted and saved.", "id": item_id}), 201
    except DuplicateReport as exc:
        return jsonify({"error": "A similar found-item report already exists. Submit again only if this is a separate report.", "duplicate": exc.duplicate}), 409
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/search-found")
def search_found():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(run_reverse_search(data))
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({"error": str(exc), "results": [], "trace": []}), 400
    except Exception:
        app.logger.exception("Unexpected reverse search failure")
        return jsonify({"error": "The reverse matching service encountered an unexpected error. Check the backend terminal for details.", "results": [], "trace": []}), 500


@app.get("/api/feed")
def feed():
    kind = request.args.get("type", "all").strip().lower()
    try:
        limit = max(1, min(int(request.args.get("limit", 80)), 100))
    except ValueError:
        limit = 80

    tables = [("lost_items", "Lost")] if kind == "lost" else [("found_items", "Found")] if kind == "found" else [("lost_items", "Lost"), ("found_items", "Found")]
    items = []
    with get_db() as conn:
        for table, verb in tables:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            for row in rows:
                item = dict(row)
                item["report_type"] = verb.lower()
                parts = [item.get("color"), item.get("brand"), item.get("category")]
                item["caption"] = verb + " " + " ".join(x for x in parts if x) + (f" near {item['zone']}" if item.get("zone") else "")
                item["is_sample"] = bool(item.get("photo_name") and str(item["photo_name"]).startswith("sample_"))
                item["owner_name"] = item.get("owner_name") or "Community Reporter"
                # Status and contact are private profile fields and must never be exposed in public feeds.
                item.pop("contact_info", None)
                item.pop("status", None)
                items.append(item)
    items.sort(key=lambda x: x.get("id", 0), reverse=True)
    return jsonify({"items": items[:limit], "type": kind})


@app.get("/api/profile")
@app.get("/api/profile/<user_id>")
def profile(user_id=None):
    user_id = user_id or request.args.get("user_id") or request.args.get("id")
    user_id = clean_text(user_id, "User ID", 120)
    with get_db() as conn:
        lost = [dict(r) for r in conn.execute("SELECT * FROM lost_items WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()]
        found = [dict(r) for r in conn.execute("SELECT * FROM found_items WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()]
    for item in lost + found:
        item["contact_info"] = item.get("contact_info") or ""
        item["status"] = item.get("status") or "pending"
    name = (lost + found)[0].get("owner_name") if (lost + found) else ""
    contact = (lost + found)[0].get("contact_info") if (lost + found) else ""
    return jsonify({"owner_name": name or "", "contact_info": contact or "", "lost_items": lost, "found_items": found})


@app.delete("/api/items/<kind>/<int:item_id>")
def delete_item(kind, item_id):
    table = "found_items" if kind.lower() == "found" else "lost_items" if kind.lower() == "lost" else None
    if not table:
        return jsonify({"error": "Invalid item type."}), 400
    user_id = clean_text(request.args.get("user_id"), "User ID", 120)
    with get_db() as conn:
        row = conn.execute(f"SELECT id FROM {table} WHERE id=? AND user_id=?", (item_id, user_id)).fetchone()
        if not row:
            return jsonify({"error": "Report not found or you do not have permission to delete it."}), 404
        conn.execute(f"DELETE FROM {table} WHERE id=? AND user_id=?", (item_id, user_id))
    return jsonify({"ok": True, "message": "Report deleted."})


@app.patch("/api/items/<kind>/<int:item_id>/status")
def update_status(kind, item_id):
    table = "found_items" if kind.lower() == "found" else "lost_items" if kind.lower() == "lost" else None
    if not table:
        return jsonify({"error": "Invalid item type."}), 400
    user_id = clean_text(request.args.get("user_id"), "User ID", 120)
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip().lower()
    if status not in {"pending", "found"}:
        return jsonify({"error": "Status must be pending or found."}), 400
    with get_db() as conn:
        cur = conn.execute(f"UPDATE {table} SET status=? WHERE id=? AND user_id=?", (status, item_id, user_id))
        if cur.rowcount == 0:
            return jsonify({"error": "Report not found or you do not have permission to update it."}), 404
    return jsonify({"ok": True, "status": status})


@app.post("/api/search")
def search():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(run_search(data))
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({"error": str(exc), "results": [], "trace": []}), 400
    except Exception:
        app.logger.exception("Unexpected search failure")
        return jsonify({"error": "The search service encountered an unexpected error. Check the backend terminal for details.", "results": [], "trace": []}), 500


@app.get("/api/evaluation/benchmark")
def evaluation_benchmark():
    """Runs a repeatable demo benchmark; manual predictions are optional human-entered observations."""
    try:
        tau = float(request.args.get("tau", DEFAULT_TAU))
        if not 0 <= tau <= 1:
            raise ValueError("Threshold must be between 0 and 1.")
        cases = benchmark_cases()
        expected, semantic_predictions, keyword_predictions = [], [], []
        detail_rows = []
        for case in cases:
            query = case["query"]
            scored = score_all_candidates(query, tau)
            semantic_id = scored[0]["id"] if scored else None
            keyword_id = keyword_prediction(query)
            expected.append(case["expected_id"])
            semantic_predictions.append(semantic_id)
            keyword_predictions.append(keyword_id)
            detail_rows.append({"case_id": case["case_id"], "scenario": case["scenario"],
                                "expected_id": case["expected_id"], "semantic_prediction": semantic_id,
                                "keyword_prediction": keyword_id,
                                "semantic_score": round(scored[0]["score"], 4) if scored else None,
                                "semantic_correct": semantic_id == case["expected_id"],
                                "keyword_correct": keyword_id == case["expected_id"]})
        report = {"threshold": tau, "dataset_note": "Four controlled scenarios generated from the seeded demonstration records. These are functional test cases, not a representative campus-wide accuracy study.",
                  "semantic_search": metric_summary(expected, semantic_predictions),
                  "keyword_baseline": metric_summary(expected, keyword_predictions),
                  "cases": detail_rows,
                  "manual_note": "Manual matching is not fabricated or simulated. Enter human-assessed predictions in manual_predictions to calculate a manual baseline."}
        manual_raw = request.args.get("manual_predictions")
        if manual_raw:
            manual = json.loads(manual_raw)
            manual_predictions = [str(manual.get(case["case_id"])) if manual.get(case["case_id"]) not in (None, "", "none", "null") else None for case in cases]
            report["manual_matching"] = metric_summary(expected, manual_predictions)
            for row, prediction in zip(detail_rows, manual_predictions):
                row["manual_prediction"] = prediction
                row["manual_correct"] = prediction == row["expected_id"]
        return jsonify(report)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        app.logger.exception("Evaluation benchmark failed")
        return jsonify({"error": "Unable to run evaluation benchmark."}), 500


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "The uploaded image is too large. Maximum request size is 7 MB."}), 413


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "API route not found."}), 404
    return "Page not found", 404


init_db()
seed_demo_data()

if __name__ == "__main__":
    print("Lost & Found AI running at http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
