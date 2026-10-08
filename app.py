import streamlit as st
import sqlite3
import requests
import pandas as pd
import re
import time
import json
import math
import altair as alt

DB_NAME = "mtg_inventory.db"
SCRYFALL_HEADERS = {
    "User-Agent": "MTGInventoryManager/1.0 (Commander Collection Tracker; local app)",
    "Accept": "application/json"
}


# ==========================================
# 1. DATABASE SETUP
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Local Scryfall Card Cache to avoid duplicate API calls
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS card_cache (
        card_name TEXT PRIMARY KEY,
        cmc REAL,
        type_line TEXT,
        color_identity TEXT,
        image_url TEXT,
        mana_cost TEXT,
        oracle_text TEXT,
        keywords TEXT,
        faces_json TEXT
    )""")

    # Check for missing columns in card_cache
    cursor.execute("PRAGMA table_info(card_cache)")
    cache_cols = [col[1] for col in cursor.fetchall()]
    for col in ["image_url", "mana_cost", "oracle_text", "keywords", "faces_json", "produced_mana"]:
        if col not in cache_cols:
            cursor.execute(f"ALTER TABLE card_cache ADD COLUMN {col} TEXT")

    # Physical Collection
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS collection (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_name TEXT UNIQUE NOT NULL,
        total_owned INTEGER NOT NULL DEFAULT 1,
        cmc REAL,
        type_line TEXT,
        color_identity TEXT,
        image_url TEXT,
        mana_cost TEXT,
        oracle_text TEXT,
        keywords TEXT,
        faces_json TEXT,
        produced_mana TEXT
    )""")

    # Check for missing columns in collection
    cursor.execute("PRAGMA table_info(collection)")
    cols = [col[1] for col in cursor.fetchall()]
    for col in ["image_url", "mana_cost", "oracle_text", "keywords", "faces_json", "produced_mana"]:
        if col not in cols:
            cursor.execute(f"ALTER TABLE collection ADD COLUMN {col} TEXT")

    # Decks (stores primary commander and partner/background commander)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS decks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        commander TEXT,
        partner_commander TEXT
    )""")

    cursor.execute("PRAGMA table_info(decks)")
    deck_cols = [col[1] for col in cursor.fetchall()]
    if "commander" not in deck_cols:
        cursor.execute("ALTER TABLE decks ADD COLUMN commander TEXT")
    if "partner_commander" not in deck_cols:
        cursor.execute("ALTER TABLE decks ADD COLUMN partner_commander TEXT")

    # Deck Allocations
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS deck_cards (
        deck_id INTEGER,
        card_name TEXT,
        quantity INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (deck_id, card_name),
        FOREIGN KEY (deck_id) REFERENCES decks(id) ON DELETE CASCADE,
        FOREIGN KEY (card_name) REFERENCES collection(card_name)
    )""")

    # View for Available Cards (includes faces_json and produced_mana)
    cursor.execute("DROP VIEW IF EXISTS v_available_cards")
    cursor.execute("""
    CREATE VIEW v_available_cards AS
    SELECT 
        c.card_name,
        c.cmc,
        c.type_line,
        c.color_identity,
        c.image_url,
        c.mana_cost,
        c.oracle_text,
        c.keywords,
        c.faces_json,
        c.produced_mana,
        c.total_owned,
        COALESCE(SUM(dc.quantity), 0) AS total_assigned,
        (c.total_owned - COALESCE(SUM(dc.quantity), 0)) AS quantity_available
    FROM collection c
    LEFT JOIN deck_cards dc ON c.card_name = dc.card_name
    GROUP BY c.card_name
    """)
    conn.commit()
    conn.close()


# ==========================================
# 2. HELPER FUNCTIONS & SCRYFALL CACHING
# ==========================================
def get_cached_card(card_name):
    """
    Checks local SQLite DB first (both collection and card_cache tables)
    to avoid redundant Scryfall API calls.
    """
    if not card_name or not str(card_name).strip():
        return None
    cleaned = str(card_name).strip()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. Check collection table first
    cursor.execute("""
        SELECT card_name, cmc, type_line, color_identity, image_url, mana_cost, oracle_text, keywords, faces_json, produced_mana 
        FROM collection 
        WHERE card_name = ? OR card_name LIKE ?
    """, (cleaned, cleaned))
    row = cursor.fetchone()
    if row and row[1] is not None and row[2] is not None:
        conn.close()
        faces_list = []
        if len(row) > 8 and row[8]:
            try:
                faces_list = json.loads(row[8])
            except Exception:
                pass
        return {
            "name": row[0],
            "cmc": row[1] or 0.0,
            "type_line": row[2] or "",
            "color_identity": row[3] or "",
            "image_url": row[4] or "",
            "mana_cost": row[5] or "",
            "oracle_text": row[6] or "",
            "keywords": row[7] or "",
            "faces_json": row[8] or "",
            "faces": faces_list,
            "produced_mana": (row[9] if len(row) > 9 and row[9] else "") or ""
        }

    # 2. Check local card_cache table
    cursor.execute("""
        SELECT card_name, cmc, type_line, color_identity, image_url, mana_cost, oracle_text, keywords, faces_json, produced_mana 
        FROM card_cache 
        WHERE card_name = ? OR card_name LIKE ?
    """, (cleaned, cleaned))
    row_c = cursor.fetchone()
    conn.close()
    if row_c and row_c[1] is not None and row_c[2] is not None:
        faces_list = []
        if len(row_c) > 8 and row_c[8]:
            try:
                faces_list = json.loads(row_c[8])
            except Exception:
                pass
        return {
            "name": row_c[0],
            "cmc": row_c[1] or 0.0,
            "type_line": row_c[2] or "",
            "color_identity": row_c[3] or "",
            "image_url": row_c[4] or "",
            "mana_cost": row_c[5] or "",
            "oracle_text": row_c[6] or "",
            "keywords": row_c[7] or "",
            "faces_json": row_c[8] or "",
            "faces": faces_list,
            "produced_mana": (row_c[9] if len(row_c) > 9 and row_c[9] else "") or ""
        }
    return None


def save_to_card_cache(card_data):
    """Caches card data in card_cache table so Scryfall is never queried again for this card."""
    if not card_data or not card_data.get("name"):
        return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO card_cache (card_name, cmc, type_line, color_identity, image_url, mana_cost, oracle_text, keywords, faces_json, produced_mana)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(card_name) DO UPDATE SET
            cmc = excluded.cmc,
            type_line = excluded.type_line,
            color_identity = excluded.color_identity,
            image_url = excluded.image_url,
            mana_cost = excluded.mana_cost,
            oracle_text = excluded.oracle_text,
            keywords = excluded.keywords,
            faces_json = excluded.faces_json,
            produced_mana = excluded.produced_mana
    """, (card_data["name"], card_data.get("cmc", 0.0), card_data.get("type_line", ""),
          card_data.get("color_identity", ""), card_data.get("image_url", ""),
          card_data.get("mana_cost", ""), card_data.get("oracle_text", ""), card_data.get("keywords", ""),
          card_data.get("faces_json"), card_data.get("produced_mana", "")))
    conn.commit()
    conn.close()


def fetch_scryfall_data(card_name):
    """
    Fetch card metadata from local cache first.
    Queries Scryfall API ONLY initially if the card is not already cached.
    """
    cached = get_cached_card(card_name)
    if cached:
        return cached

    # Query Scryfall API with custom headers and timeout
    try:
        url = f"https://api.scryfall.com/cards/named?exact={requests.utils.quote(str(card_name).strip())}"
        resp = requests.get(url, headers=SCRYFALL_HEADERS, timeout=5)
        if resp.status_code == 200:
            data = resp.json()

            # Handle double-faced cards (Transform, MDFC, Flip, Battles)
            faces_list = []
            if "card_faces" in data and len(data["card_faces"]) >= 2:
                for f in data["card_faces"]:
                    f_img = f.get("image_uris", {}).get("normal")
                    if not f_img:
                        f_img = f"https://api.scryfall.com/cards/named?exact={requests.utils.quote(f.get('name', ''))}&format=image"
                    f_kw = f.get("keywords", [])
                    f_kw_str = ", ".join(f_kw) if isinstance(f_kw, list) else str(f_kw or "")
                    faces_list.append({
                        "name": f.get("name", ""),
                        "mana_cost": f.get("mana_cost", ""),
                        "type_line": f.get("type_line", ""),
                        "oracle_text": f.get("oracle_text", ""),
                        "image_url": f_img,
                        "power": f.get("power", ""),
                        "toughness": f.get("toughness", ""),
                        "loyalty": f.get("loyalty", ""),
                        "defense": f.get("defense", ""),
                        "cmc": f.get("cmc", 0.0),
                        "keywords": f_kw_str
                    })
            faces_json_str = json.dumps(faces_list) if faces_list else None

            img = data.get("image_uris", {}).get("normal")
            if not img and faces_list:
                img = faces_list[0]["image_url"]
            if not img:
                img = f"https://api.scryfall.com/cards/named?exact={requests.utils.quote(data.get('name', card_name))}&format=image"

            mana_cost = data.get("mana_cost", "")
            if not mana_cost and faces_list:
                mana_cost = faces_list[0].get("mana_cost", "")

            oracle_text = data.get("oracle_text", "")
            if not oracle_text and faces_list:
                oracle_text = " // \n".join([f.get("oracle_text", "") for f in faces_list if f.get("oracle_text")])

            keywords_val = data.get("keywords", [])
            if not keywords_val and "card_faces" in data:
                face_kws = []
                for f in data["card_faces"]:
                    for kw in f.get("keywords", []):
                        if kw not in face_kws:
                            face_kws.append(kw)
                keywords_val = face_kws
            keywords_str = ", ".join(keywords_val) if isinstance(keywords_val, list) else str(keywords_val or "")

            produced_mana_val = data.get("produced_mana", [])
            if not produced_mana_val and "card_faces" in data:
                f_prod = []
                for f in data["card_faces"]:
                    for pm in f.get("produced_mana", []):
                        if pm not in f_prod:
                            f_prod.append(pm)
                produced_mana_val = f_prod
            produced_mana_str = ",".join(produced_mana_val) if isinstance(produced_mana_val, list) else str(produced_mana_val or "")

            ret_data = {
                "name": data.get("name"),
                "cmc": data.get("cmc", 0.0),
                "type_line": data.get("type_line", ""),
                "color_identity": ",".join(data.get("color_identity", [])),
                "image_url": img,
                "mana_cost": mana_cost,
                "oracle_text": oracle_text,
                "keywords": keywords_str,
                "faces_json": faces_json_str,
                "faces": faces_list,
                "produced_mana": produced_mana_str
            }

            # Cache locally to prevent future API calls
            save_to_card_cache(ret_data)

            # If this card exists in the collection table, update cached fields
            try:
                conn_up = sqlite3.connect(DB_NAME)
                cur_up = conn_up.cursor()
                cur_up.execute("""
                    UPDATE collection 
                    SET cmc = COALESCE(cmc, ?),
                        type_line = CASE WHEN type_line IS NULL OR type_line = '' THEN ? ELSE type_line END,
                        color_identity = CASE WHEN color_identity IS NULL OR color_identity = '' THEN ? ELSE color_identity END,
                        image_url = CASE WHEN image_url IS NULL OR image_url = '' THEN ? ELSE image_url END,
                        mana_cost = CASE WHEN mana_cost IS NULL OR mana_cost = '' THEN ? ELSE mana_cost END,
                        oracle_text = CASE WHEN ? != '' THEN ? ELSE oracle_text END,
                        keywords = CASE WHEN ? != '' THEN ? ELSE keywords END,
                        faces_json = CASE WHEN ? IS NOT NULL AND ? != '' THEN ? ELSE faces_json END,
                        produced_mana = CASE WHEN ? != '' THEN ? ELSE produced_mana END
                    WHERE card_name = ? OR card_name LIKE ?
                """, (ret_data["cmc"], ret_data["type_line"], ret_data["color_identity"],
                      ret_data["image_url"], ret_data["mana_cost"],
                      oracle_text, oracle_text,
                      keywords_str, keywords_str,
                      faces_json_str, faces_json_str,
                      produced_mana_str, produced_mana_str,
                      ret_data["name"], card_name))
                conn_up.commit()
                conn_up.close()
            except Exception:
                pass

            return ret_data
    except Exception:
        pass

    return None


def get_card_display_details(card_name, card_info, scope="default"):
    """
    Handles single-faced and double-faced MTG cards.
    Returns: (active_face_idx, info_dict, has_faces, faces_list)
    where info_dict contains ONLY the fields belonging to the currently flipped face.
    """
    faces = []
    if isinstance(card_info, dict):
        faces = card_info.get("faces", [])
        if not faces and card_info.get("faces_json"):
            try:
                faces = json.loads(card_info["faces_json"])
            except Exception:
                faces = []
    elif hasattr(card_info, "get"):
        fj = card_info.get("faces_json")
        if fj and pd.notna(fj) and str(fj).strip():
            try:
                faces = json.loads(str(fj))
            except Exception:
                faces = []

    has_faces = bool(faces and len(faces) >= 2)
    state_key = f"face_idx_{scope}_{card_name}"
    active_idx = st.session_state.get(state_key, 0)
    if active_idx >= len(faces):
        active_idx = 0

    if has_faces:
        face = faces[active_idx]
        face_img = face.get("image_url") or get_card_image_url(face.get("name", card_name))
        info = {
            "name": face.get("name", card_name),
            "image_url": face_img,
            "mana_cost": face.get("mana_cost", ""),
            "type_line": face.get("type_line", ""),
            "oracle_text": face.get("oracle_text", ""),
            "cmc": face.get("cmc", 0.0),
            "power": face.get("power", ""),
            "toughness": face.get("toughness", ""),
            "loyalty": face.get("loyalty", ""),
            "defense": face.get("defense", ""),
            "keywords": face.get("keywords", "")
        }
    else:
        if isinstance(card_info, dict):
            info = {
                "name": card_info.get("name", card_name),
                "image_url": get_card_image_url(card_name, card_info.get("image_url")),
                "mana_cost": card_info.get("mana_cost", ""),
                "type_line": card_info.get("type_line", ""),
                "oracle_text": card_info.get("oracle_text", ""),
                "cmc": card_info.get("cmc", 0.0),
                "power": "", "toughness": "", "loyalty": "", "defense": "",
                "keywords": card_info.get("keywords", "")
            }
        else:
            info = {
                "name": card_name,
                "image_url": get_card_image_url(card_name, card_info.get('image_url') if hasattr(card_info, 'get') else None),
                "mana_cost": card_info.get('mana_cost', '') if hasattr(card_info, 'get') else "",
                "type_line": card_info.get('type_line', '') if hasattr(card_info, 'get') else "",
                "oracle_text": card_info.get('oracle_text', '') if hasattr(card_info, 'get') else "",
                "cmc": card_info.get('cmc', 0.0) if hasattr(card_info, 'get') else 0.0,
                "power": "", "toughness": "", "loyalty": "", "defense": "",
                "keywords": card_info.get('keywords', '') if hasattr(card_info, 'get') else ""
            }

    return active_idx, info, has_faces, faces


def toggle_card_face(card_name, scope="default"):
    """Toggles active face for double-faced cards."""
    key = f"face_idx_{scope}_{card_name}"
    st.session_state[key] = 1 if st.session_state.get(key, 0) == 0 else 0


def get_commander_partner_type(cmdr_data):
    """
    Checks whether a commander card supports a secondary commander (Partner or Background).
    Returns:
      'background' - if the commander has 'Choose a Background'
      'partner'    - if the commander has 'Partner', 'Partner with', 'Friends forever', or "Doctor's companion"
      None         - if neither (standard single commander)
    """
    if not cmdr_data:
        return None

    oracle = str(cmdr_data.get('oracle_text', '')).lower()
    keywords_raw = cmdr_data.get('keywords', '')
    if isinstance(keywords_raw, list):
        keywords = [k.strip().lower() for k in keywords_raw]
    else:
        keywords = [k.strip().lower() for k in str(keywords_raw).split(',') if k.strip()]

    # 1. Background check
    if 'choose a background' in oracle or any('choose a background' in k for k in keywords):
        return 'background'

    # 2. Partner variants (standard Partner, Friends forever, Doctor's companion, Partner with)
    if any('partner' in k for k in keywords) or 'partner' in oracle:
        return 'partner'
    if any('friends forever' in k for k in keywords) or 'friends forever' in oracle:
        return 'partner'
    if any("doctor's companion" in k for k in keywords) or "doctor's companion" in oracle:
        return 'partner'

    return None


def get_card_image_url(card_name, stored_url=None):
    """Returns valid image URL, falling back to Scryfall direct named image redirect."""
    if stored_url and isinstance(stored_url, str) and stored_url.strip():
        return stored_url
    return f"https://api.scryfall.com/cards/named?exact={requests.utils.quote(str(card_name))}&format=image"


CATEGORY_ORDER = [
    ("Creatures", "⚔️"),
    ("Planeswalkers", "🧙"),
    ("Instants", "⚡"),
    ("Sorceries", "📜"),
    ("Artifacts", "🛡️"),
    ("Enchantments", "🔮"),
    ("Battles", "💥"),
    ("Lands", "🏔️"),
    ("Other", "📦"),
]


def get_card_category(type_line):
    """Categorizes card according to standard MTG deck conventions."""
    if not type_line or pd.isna(type_line):
        return "Other"
    tl = str(type_line).lower()
    if "creature" in tl:
        return "Creatures"
    if "planeswalker" in tl:
        return "Planeswalkers"
    if "instant" in tl:
        return "Instants"
    if "sorcery" in tl:
        return "Sorceries"
    if "land" in tl:
        return "Lands"
    if "artifact" in tl:
        return "Artifacts"
    if "enchantment" in tl:
        return "Enchantments"
    if "battle" in tl:
        return "Battles"
    return "Other"


def parse_card_line(line):
    """Parses lines like '4 Sol Ring', '1x Counterspell', or 'Brainstorm'."""
    line = line.strip()
    if not line:
        return None
    match = re.match(r"^(\d+)\s*x?\s+(.+)$", line, re.IGNORECASE)
    if match:
        return int(match.group(1)), match.group(2).strip()
    return 1, line  # Default to 1 copy if no quantity is specified


def run_query(query, params=()):
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


def execute_db(query, params=()):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        conn.commit()
        return True
    except Exception as e:
        st.error(f"Database error: {e}")
        return False
    finally:
        conn.close()


def add_card_to_collection(card_name, qty):
    """Adds or increases card count in the collection table."""
    card_data = fetch_scryfall_data(card_name)
    if card_data:
        name = card_data["name"]
        execute_db("""
            INSERT INTO collection (card_name, total_owned, cmc, type_line, color_identity, image_url, mana_cost, oracle_text, keywords, faces_json, produced_mana) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(card_name) DO UPDATE SET 
                total_owned = total_owned + ?,
                cmc = excluded.cmc,
                type_line = excluded.type_line,
                color_identity = excluded.color_identity,
                image_url = CASE WHEN excluded.image_url IS NOT NULL AND excluded.image_url != '' THEN excluded.image_url ELSE collection.image_url END,
                mana_cost = CASE WHEN excluded.mana_cost IS NOT NULL AND excluded.mana_cost != '' THEN excluded.mana_cost ELSE collection.mana_cost END,
                oracle_text = CASE WHEN excluded.oracle_text IS NOT NULL AND excluded.oracle_text != '' THEN excluded.oracle_text ELSE collection.oracle_text END,
                keywords = CASE WHEN excluded.keywords IS NOT NULL AND excluded.keywords != '' THEN excluded.keywords ELSE collection.keywords END,
                faces_json = CASE WHEN excluded.faces_json IS NOT NULL AND excluded.faces_json != '' THEN excluded.faces_json ELSE collection.faces_json END,
                produced_mana = CASE WHEN excluded.produced_mana IS NOT NULL AND excluded.produced_mana != '' THEN excluded.produced_mana ELSE collection.produced_mana END
        """, (name, qty, card_data["cmc"], card_data["type_line"], card_data["color_identity"],
              card_data.get("image_url"), card_data.get("mana_cost"), card_data.get("oracle_text", ""), card_data.get("keywords", ""),
              card_data.get("faces_json"), card_data.get("produced_mana", ""), qty))
        return name
    return None


BASIC_LAND_COLORS = {
    "plains": {"W"},
    "snow-covered plains": {"W"},
    "island": {"U"},
    "snow-covered island": {"U"},
    "swamp": {"B"},
    "snow-covered swamp": {"B"},
    "mountain": {"R"},
    "snow-covered mountain": {"R"},
    "forest": {"G"},
    "snow-covered forest": {"G"},
    "wastes": set(),
}

BASIC_LANDS = set(BASIC_LAND_COLORS.keys())

COLOR_NAME_MAP = {
    "W": "☀️ White",
    "U": "🔵 Blue",
    "B": "⚫ Black",
    "R": "🔴 Red",
    "G": "🟢 Green",
    "C": "🔘 Colorless",
}

COLOR_EMOJI_MAP = {
    "W": "☀️",
    "U": "🔵",
    "B": "⚫",
    "R": "🔴",
    "G": "🟢",
    "C": "🔘",
}


def parse_color_identity(ci_str):
    if not ci_str or pd.isna(ci_str):
        return set()
    if isinstance(ci_str, set):
        return ci_str
    return set(c.strip().upper() for c in str(ci_str).split(",") if c.strip())


def is_card_legal_for_commander(card_name, card_ci_str, commander_ci):
    """
    Checks if a card is legal under the Commander's (or combined partners') color identity.
    If commander_ci is None, any card is permitted.
    """
    if commander_ci is None:
        return True

    c_lower = card_name.lower().strip()
    if c_lower in BASIC_LAND_COLORS:
        land_colors = BASIC_LAND_COLORS[c_lower]
        return land_colors.issubset(commander_ci)

    card_ci = parse_color_identity(card_ci_str)
    return card_ci.issubset(commander_ci)


def remove_card_from_deck(deck_id, card_name, remove_all=False):
    """Removes card copies from deck_cards, instantly returning them to binder."""
    if remove_all:
        execute_db("DELETE FROM deck_cards WHERE deck_id = ? AND card_name = ?", (deck_id, card_name))
    else:
        res = run_query("SELECT quantity FROM deck_cards WHERE deck_id = ? AND card_name = ?", (deck_id, card_name))
        if not res.empty:
            current_qty = int(res.iloc[0]['quantity'])
            if current_qty > 1:
                execute_db("UPDATE deck_cards SET quantity = quantity - 1 WHERE deck_id = ? AND card_name = ?", (deck_id, card_name))
            else:
                execute_db("DELETE FROM deck_cards WHERE deck_id = ? AND card_name = ?", (deck_id, card_name))
    return True


def add_card_to_deck(deck_id, card_name, qty=1):
    """Adds a card copy to deck."""
    execute_db("""
        INSERT INTO deck_cards (deck_id, card_name, quantity) 
        VALUES (?, ?, ?)
        ON CONFLICT(deck_id, card_name) DO UPDATE SET quantity = quantity + ?
    """, (deck_id, card_name, qty, qty))
    return True


# ==========================================
# 3. OLLAMA LOCAL LLM INTEGRATION
# ==========================================
def get_ollama_status_and_models(base_url="http://localhost:11434"):
    """Checks if local Ollama server is running and returns list of installed models."""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=2)
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("name") for m in data.get("models", []) if m.get("name")]
            return True, models
    except Exception:
        pass
    return False, []


def call_ollama(prompt, model="llama3", base_url="http://localhost:11434"):
    """Calls local Ollama API strictly without making any external network requests."""
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False
    }
    try:
        resp = requests.post(url, json=payload, timeout=180)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("message", {}).get("content", "")
    except Exception:
        pass

    # Fallback to /api/generate
    gen_url = f"{base_url.rstrip('/')}/api/generate"
    gen_payload = {
        "model": model,
        "prompt": prompt,
        "stream": False
    }
    gen_resp = requests.post(gen_url, json=gen_payload, timeout=180)
    if gen_resp.status_code == 200:
        return gen_resp.json().get("response", "")
    else:
        raise Exception(f"Ollama returned HTTP {gen_resp.status_code}: {gen_resp.text}")


def format_deck_concise_for_llm(df_deck):
    """Formats the current deck compactly by category to minimize token consumption for local LLMs."""
    lines = []
    for cat_name, _ in CATEGORY_ORDER:
        cat_group = df_deck[df_deck['category'] == cat_name]
        if not cat_group.empty:
            cards_summary = []
            for _, r in cat_group.iterrows():
                cost_str = r['mana_cost'] if r['mana_cost'] else (f"CMC {r['cmc']:g}" if r['cmc'] else "0")
                o_txt = str(r.get('oracle_text', '')).replace('\n', ' ').strip()
                if len(o_txt) > 90:
                    o_txt = o_txt[:90] + "..."
                kw_str = f" [{r['keywords']}]" if r.get('keywords') and str(r['keywords']).strip() and str(r['keywords']).strip() != 'nan' else ""
                cards_summary.append(f"  • {r['quantity']}x {r['card_name']} ({cost_str}|{r['type_line']}){kw_str}: {o_txt}")
            lines.append(f"[{cat_name.upper()} ({int(cat_group['quantity'].sum())})]\n" + "\n".join(cards_summary))
    return "\n\n".join(lines)


def format_candidate_card_concise(r):
    """Formats a candidate binder card compactly to preserve local LLM context window."""
    cost_str = r['mana_cost'] if r['mana_cost'] else (f"CMC {r['cmc']:g}" if r['cmc'] else "0")
    o_txt = str(r.get('oracle_text', '')).replace('\n', ' ').strip()
    if len(o_txt) > 100:
        o_txt = o_txt[:100] + "..."
    kw_str = f" [{r['keywords']}]" if r.get('keywords') and str(r['keywords']).strip() and str(r['keywords']).strip() != 'nan' else ""
    return f"• {r['card_name']} ({cost_str}|{r['type_line']}){kw_str}: {o_txt}"


def extract_swaps_from_text(text, deck_cards_set, binder_cards_set):
    """
    Extracts valid (cut_card, add_card) pairs from LLM text that match actual
    cards in the deck and available in the binder. Supports both single-line
    and multi-line (bulleted) response formats.
    """
    found_swaps = []
    if not text:
        return found_swaps

    def match_card(cand_name, card_set):
        clean_cand = cand_name.strip().strip("*[]\"'").lower()
        if not clean_cand:
            return None
        # Exact match first
        for c in card_set:
            if c.lower() == clean_cand:
                return c
        # Substring / partial match
        for c in card_set:
            if c.lower() in clean_cand or clean_cand in c.lower():
                return c
        return None

    # 1. Multi-line pattern (e.g. "- Cut: Card A\n- Add: Card B")
    multi_pattern = re.compile(
        r"(?:Cut|Remove):\s*([^\n\r\(\)—–|]+)[\s\S]*?(?:Add|Include):\s*([^\n\r\(\)—–|]+)",
        re.IGNORECASE
    )
    for m in multi_pattern.finditer(text):
        c_raw = m.group(1).split("\n")[0].strip()
        a_raw = m.group(2).split("\n")[0].strip()
        c_card = match_card(c_raw, deck_cards_set)
        a_card = match_card(a_raw, binder_cards_set)
        if c_card and a_card and (c_card, a_card) not in found_swaps:
            found_swaps.append((c_card, a_card))

    # 2. Single-line pattern (e.g. "Cut: Card A -> Add: Card B" or "Swap Card A for Card B")
    for line in text.split("\n"):
        cleaned_line = line.replace("*", "").replace("[", "").replace("]", "")
        m_single = re.search(
            r"(?:Cut|Remove):?\s*([^\-–—\|,\(\)]+).*?(?:Add|Add from Binder|with|for):?\s*([^\-–—\|,\(\)]+)",
            cleaned_line,
            re.IGNORECASE
        )
        if m_single:
            c_raw = m_single.group(1).strip()
            a_raw = m_single.group(2).strip()
            c_card = match_card(c_raw, deck_cards_set)
            a_card = match_card(a_raw, binder_cards_set)
            if c_card and a_card and (c_card, a_card) not in found_swaps:
                found_swaps.append((c_card, a_card))

    return found_swaps


# ==========================================
# 3.5. LAND BASE BALANCER & MANA MATH
# ==========================================
def count_mana_pips(df_non_land):
    """
    Parses mana_cost from non-land cards to count colored mana symbols (pips).
    Returns a dict: {'W': count, 'U': count, 'B': count, 'R': count, 'G': count, 'C': count}
    """
    pips = {"W": 0.0, "U": 0.0, "B": 0.0, "R": 0.0, "G": 0.0, "C": 0.0}
    if df_non_land.empty:
        return pips

    for _, row in df_non_land.iterrows():
        m_cost = str(row.get('mana_cost', '')).strip()
        qty = int(row.get('quantity', 1))
        if not m_cost or m_cost == 'nan':
            continue
        symbols = re.findall(r"\{([^}]+)\}", m_cost)
        for sym in symbols:
            sym_clean = sym.strip().upper()
            if sym_clean in ["W", "U", "B", "R", "G", "C"]:
                pips[sym_clean] += 1.0 * qty
            elif "/" in sym_clean:
                parts = sym_clean.split("/")
                colored_parts = [p for p in parts if p in ["W", "U", "B", "R", "G"]]
                if colored_parts:
                    frac = 1.0 / len(colored_parts)
                    for cp in colored_parts:
                        pips[cp] += frac * qty
                elif "C" in parts:
                    pips["C"] += 0.5 * qty
    return pips


def get_card_produced_mana(card_name, oracle_text="", type_line="", produced_mana="", color_identity="", commander_ci=None):
    """
    Determines which mana colors a card can produce completely offline without
    relying on any hardcoded card dictionaries.
    Uses stored Scryfall produced_mana if available, and falls back to
    robust offline parsing of oracle_text, type_line, and color_identity.
    """
    if produced_mana and str(produced_mana).strip():
        pm_set = {c.strip().upper() for c in str(produced_mana).split(',') if c.strip()}
        if {"W", "U", "B", "R", "G"}.issubset(pm_set):
            if commander_ci:
                return set(commander_ci)
            return pm_set
        if pm_set:
            return pm_set

    txt = str(oracle_text or "").upper()
    t_line = str(type_line or "").lower()
    colors_found = set()

    # Rainbow / Any color phrases
    rainbow_phrases = [
        "MANA OF ANY COLOR", "ANY COLOR OF MANA", "ONE MANA OF ANY COLOR",
        "MANA OF ANY TYPE THAT A LAND YOU CONTROL COULD PRODUCE",
        "ANY COLOR AMONG PERMANENTS YOU CONTROL",
        "ANY COLOR IN YOUR COMMANDER'S COLOR IDENTITY",
        "CHOOSE A COLOR"
    ]
    for rp in rainbow_phrases:
        if rp in txt:
            if commander_ci:
                return set(commander_ci)
            return {"W", "U", "B", "R", "G"}

    # Basic land types in type line
    if "land" in t_line:
        if "plains" in t_line: colors_found.add("W")
        if "island" in t_line: colors_found.add("U")
        if "swamp" in t_line: colors_found.add("B")
        if "mountain" in t_line: colors_found.add("R")
        if "forest" in t_line: colors_found.add("G")
        if "wastes" in t_line: colors_found.add("C")

    # Land ramp spells
    if ("search your library" in txt.lower() or "search their library" in txt.lower()) and ("basic land" in txt.lower() or "land card" in txt.lower() or "forest" in txt.lower()):
        if commander_ci:
            return set(commander_ci)
        return {"W", "U", "B", "R", "G"}

    # Explicit colorless mana production patterns ({C}, {1}, {2}, "colorless mana", etc.)
    colorless_patterns = [
        r"ADD\s+(?:(?:ONE|TWO|THREE)\s+)?(?:\{C\})+",
        r"ADD\s+(?:(?:ONE|TWO|THREE)\s+)?(?:\{1\}|\{2\}|\{3\})",
        r"ADD\s+(?:(?:ONE|TWO|THREE)\s+)?COLORLESS\s+MANA",
        r"ADD\s+(?:(?:ONE|TWO|THREE)\s+)?UNCOLORED\s+MANA",
    ]
    for cp in colorless_patterns:
        if re.search(cp, txt):
            colors_found.add("C")

    # Explicit colored mana symbols produced in "ADD ..." clauses
    # We restrict search specifically to the clause following "ADD " up to sentence boundary
    # to avoid falsely matching activation costs like "{W}, {T}: ..." on colorless-producing cards
    add_clauses = re.findall(r"ADD\s+([^.\n;:]+)", txt)
    for clause in add_clauses:
        syms_in_clause = re.findall(r"\{([WUBRGC])\}", clause)
        for sym in syms_in_clause:
            colors_found.add(sym)

    # Fallback to color identity for mana-producing permanents
    if not colors_found and "ADD " in txt and color_identity:
        ci_parts = {c.strip().upper() for c in color_identity.split(',') if c.strip()}
        colors_found = colors_found.union(ci_parts)

    return colors_found


def analyze_deck_mana_sources(df_deck, commander_ci=None):
    """
    Dynamically analyzes all mana sources across the deck offline:
    - Non-basic lands
    - Mana rocks (artifacts)
    - Mana dorks (creatures)
    - Ramp spells (sorceries, instants, enchantments)
    Returns per-color sources and ramp package counts.
    """
    nonbasic_sources = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
    rock_dork_sources = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}

    mana_rocks_list = []
    mana_dorks_list = []
    ramp_spells_list = []
    cheap_ramp_count = 0  # CMC <= 2

    if df_deck.empty:
        return {
            "nonbasic_sources": nonbasic_sources,
            "rock_dork_sources": rock_dork_sources,
            "mana_rocks": [],
            "mana_dorks": [],
            "ramp_spells": [],
            "cheap_ramp_count": 0,
            "total_ramp_count": 0
        }

    for _, row in df_deck.iterrows():
        c_name = str(row.get('card_name', '')).strip()
        t_line = str(row.get('type_line', '')).lower()
        cmc = float(row.get('cmc', 0.0) or 0.0)
        q = int(row.get('quantity', 1))
        oracle = str(row.get('oracle_text', ''))
        prod = str(row.get('produced_mana', ''))
        ci = str(row.get('color_identity', ''))

        is_land = "land" in t_line
        is_basic = c_name.lower() in BASIC_LANDS

        produced_colors = get_card_produced_mana(
            card_name=c_name,
            oracle_text=oracle,
            type_line=t_line,
            produced_mana=prod,
            color_identity=ci,
            commander_ci=commander_ci
        )

        if is_land:
            if not is_basic:
                for col in produced_colors:
                    if col in nonbasic_sources:
                        nonbasic_sources[col] += q
        else:
            # Non-land card: check if it's a mana rock, mana dork, or ramp spell
            is_ramp = False
            if produced_colors:
                is_ramp = True
                for col in produced_colors:
                    if col in rock_dork_sources:
                        rock_dork_sources[col] += q
            elif ("search your library" in oracle.lower() and "land" in oracle.lower()) or "add mana" in oracle.lower():
                is_ramp = True
                target_cols = list(commander_ci) if commander_ci else ["W", "U", "B", "R", "G"]
                for col in target_cols:
                    if col in rock_dork_sources:
                        rock_dork_sources[col] += q

            if is_ramp:
                if "artifact" in t_line:
                    mana_rocks_list.append((c_name, cmc, q))
                    if cmc <= 2:
                        cheap_ramp_count += q
                elif "creature" in t_line:
                    mana_dorks_list.append((c_name, cmc, q))
                    if cmc <= 2:
                        cheap_ramp_count += q
                else:
                    ramp_spells_list.append((c_name, cmc, q))
                    if cmc <= 3:
                        cheap_ramp_count += q

    return {
        "nonbasic_sources": nonbasic_sources,
        "rock_dork_sources": rock_dork_sources,
        "mana_rocks": mana_rocks_list,
        "mana_dorks": mana_dorks_list,
        "ramp_spells": ramp_spells_list,
        "cheap_ramp_count": cheap_ramp_count,
        "total_ramp_count": len(mana_rocks_list) + len(mana_dorks_list) + len(ramp_spells_list)
    }


def allocate_integers(weights, total_slots, min_each=1):
    """
    Hamilton-Hare largest remainder quota method to distribute total_slots
    cleanly into exact integers proportional to weights.
    """
    keys = list(weights.keys())
    if not keys or total_slots <= 0:
        return {k: 0 for k in keys}
    if total_slots < len(keys) * min_each:
        sorted_keys = sorted(keys, key=lambda k: weights[k], reverse=True)
        res = {k: 0 for k in keys}
        for k in sorted_keys[:total_slots]:
            res[k] = 1
        return res

    res = {k: min_each for k in keys}
    remaining_slots = total_slots - (len(keys) * min_each)
    total_w = sum(weights.values())
    if total_w <= 0:
        for i in range(remaining_slots):
            res[keys[i % len(keys)]] += 1
        return res

    quotas = {k: (weights[k] / total_w) * remaining_slots for k in keys}
    ints = {k: int(quotas[k]) for k in keys}
    for k in keys:
        res[k] += ints[k]
    leftover = remaining_slots - sum(ints.values())
    remainders = sorted(keys, key=lambda k: quotas[k] - ints[k], reverse=True)
    for i in range(leftover):
        res[remainders[i]] += 1
    return res


def calculate_balanced_land_base(target_total_lands, df_deck, commander_ci=None, mode="deficit"):
    """
    Calculates the recommended basic land distribution taking into account:
    - target_total_lands (e.g. 30 to 42)
    - existing non-basic lands (slots & colored fixing provided)
    - non-land spell mana pips
    - mana rocks, mana dorks, and ramp spells in the deck
    """
    COLOR_TO_BASIC = {
        "W": "Plains",
        "U": "Island",
        "B": "Swamp",
        "R": "Mountain",
        "G": "Forest",
        "C": "Wastes"
    }

    df_lands = df_deck[df_deck['type_line'].str.lower().str.contains('land')].copy() if not df_deck.empty else pd.DataFrame()
    df_non_land = df_deck[~df_deck['type_line'].str.lower().str.contains('land')].copy() if not df_deck.empty else pd.DataFrame()
    df_basics = df_lands[df_lands['card_name'].str.lower().isin(BASIC_LANDS)].copy() if not df_lands.empty else pd.DataFrame()
    df_nonbasics = df_lands[~df_lands['card_name'].str.lower().isin(BASIC_LANDS)].copy() if not df_lands.empty else pd.DataFrame()

    current_nonbasics_count = int(df_nonbasics['quantity'].sum()) if not df_nonbasics.empty else 0
    current_basics_count = int(df_basics['quantity'].sum()) if not df_basics.empty else 0
    needed_basics = max(0, target_total_lands - current_nonbasics_count)

    pip_counts = count_mana_pips(df_non_land)
    mana_analysis = analyze_deck_mana_sources(df_deck, commander_ci=commander_ci)
    nonbasic_sources = mana_analysis["nonbasic_sources"]
    rock_dork_sources = mana_analysis["rock_dork_sources"]

    if commander_ci is not None and len(commander_ci) > 0:
        active_colors = sorted(list(commander_ci))
    else:
        active_colors = [c for c in ["W", "U", "B", "R", "G"] if pip_counts.get(c, 0) > 0]
        if not active_colors:
            active_colors = ["C"]

    suggested_basics = {b: 0 for b in ["Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"]}

    if not active_colors or active_colors == ["C"]:
        suggested_basics["Wastes"] = needed_basics
        return {
            "target_total": target_total_lands,
            "nonbasic_count": current_nonbasics_count,
            "needed_basics": needed_basics,
            "pip_counts": pip_counts,
            "nonbasic_sources": nonbasic_sources,
            "rock_dork_sources": rock_dork_sources,
            "mana_analysis": mana_analysis,
            "suggested_basics": suggested_basics,
            "active_colors": active_colors
        }

    if len(active_colors) == 1:
        c = active_colors[0]
        b_name = COLOR_TO_BASIC.get(c, "Wastes")
        suggested_basics[b_name] = needed_basics
        return {
            "target_total": target_total_lands,
            "nonbasic_count": current_nonbasics_count,
            "needed_basics": needed_basics,
            "pip_counts": pip_counts,
            "nonbasic_sources": nonbasic_sources,
            "rock_dork_sources": rock_dork_sources,
            "mana_analysis": mana_analysis,
            "suggested_basics": suggested_basics,
            "active_colors": active_colors
        }

    total_active_pips = sum([pip_counts.get(c, 0.0) for c in active_colors])
    weights = {}

    for c in active_colors:
        f_pip = (pip_counts.get(c, 0.0) / total_active_pips) if total_active_pips > 0 else (1.0 / len(active_colors))
        if mode == "pip_only":
            weights[c] = max(0.1, f_pip)
        else:
            desired_sources = target_total_lands * f_pip
            existing_fixed = nonbasic_sources.get(c, 0) + rock_dork_sources.get(c, 0)
            deficit = max(0.5, desired_sources - existing_fixed)
            weights[c] = deficit

    min_guarantee = 1 if needed_basics >= len(active_colors) else 0
    allocated = allocate_integers(weights, needed_basics, min_each=min_guarantee)

    for c, count in allocated.items():
        b_name = COLOR_TO_BASIC.get(c, "Wastes")
        suggested_basics[b_name] = count

    return {
        "target_total": target_total_lands,
        "nonbasic_count": current_nonbasics_count,
        "needed_basics": needed_basics,
        "pip_counts": pip_counts,
        "nonbasic_sources": nonbasic_sources,
        "rock_dork_sources": rock_dork_sources,
        "mana_analysis": mana_analysis,
        "suggested_basics": suggested_basics,
        "active_colors": active_colors
    }


# ==========================================
# 4. COMPREHENSIVE DECK DISPLAY & BUILDER
# ==========================================
def render_deck_display(deck_id, deck_name):
    """Renders a visual, analytical MTG Commander deck display with Partner/Background and Ollama optimization."""
    # Fetch deck metadata (including partner_commander)
    deck_meta = run_query("SELECT id, name, commander, partner_commander FROM decks WHERE id = ?", (deck_id,))
    current_commander = None
    current_partner = None

    if not deck_meta.empty:
        if pd.notna(deck_meta['commander'].iloc[0]) and deck_meta['commander'].iloc[0]:
            current_commander = str(deck_meta['commander'].iloc[0]).strip()
        if 'partner_commander' in deck_meta.columns and pd.notna(deck_meta['partner_commander'].iloc[0]) and deck_meta['partner_commander'].iloc[0]:
            current_partner = str(deck_meta['partner_commander'].iloc[0]).strip()

    # Commander data and color identity
    cmdr_data = None
    cmdr_ci = set()
    if current_commander:
        cmdr_data = fetch_scryfall_data(current_commander)
        if cmdr_data:
            cmdr_ci = parse_color_identity(cmdr_data.get("color_identity", ""))

    # Detect partner or background keyword capability
    partner_type = get_commander_partner_type(cmdr_data) if cmdr_data else None

    # Partner/Background data and color identity
    partner_data = None
    partner_ci = set()
    if current_partner:
        partner_data = fetch_scryfall_data(current_partner)
        if partner_data:
            partner_ci = parse_color_identity(partner_data.get("color_identity", ""))

    # Combined Color Identity (Union of Commander and Partner)
    if current_commander:
        combined_ci = cmdr_ci.union(partner_ci)
    else:
        combined_ci = None

    # Fetch deck cards (including faces_json)
    df_deck = run_query("""
        SELECT 
            dc.card_name,
            dc.quantity,
            COALESCE(c.cmc, 0.0) AS cmc,
            COALESCE(c.type_line, 'Other') AS type_line,
            COALESCE(c.color_identity, '') AS color_identity,
            c.image_url,
            COALESCE(c.mana_cost, '') AS mana_cost,
            COALESCE(c.oracle_text, '') AS oracle_text,
            COALESCE(c.keywords, '') AS keywords,
            c.faces_json,
            COALESCE(c.produced_mana, '') AS produced_mana
        FROM deck_cards dc
        LEFT JOIN collection c ON dc.card_name = c.card_name
        WHERE dc.deck_id = ?
        ORDER BY dc.card_name ASC
    """, (deck_id,))

    if not df_deck.empty:
        df_deck['category'] = df_deck['type_line'].apply(get_card_category)
        total_cards = int(df_deck['quantity'].sum())
        unique_cards = len(df_deck)
        df_non_land = df_deck[~df_deck['type_line'].str.lower().str.contains('land')]
        total_non_land = int(df_non_land['quantity'].sum()) if not df_non_land.empty else 0
        total_lands = total_cards - total_non_land
        avg_cmc = (df_non_land['cmc'] * df_non_land['quantity']).sum() / total_non_land if total_non_land > 0 else 0.0
    else:
        df_deck['category'] = []
        total_cards = 0
        unique_cards = 0
        df_non_land = pd.DataFrame()
        total_non_land = 0
        total_lands = 0
        avg_cmc = 0.0

    # ---------------------------------------------------------
    # COMMAND ZONE (COMMANDER & OPTIONAL PARTNER / BACKGROUND)
    # ---------------------------------------------------------
    col_cz_title, col_cz_dup = st.columns([3, 1])
    with col_cz_title:
        st.markdown("### 👑 Command Zone")
    with col_cz_dup:
        with st.popover("📋 Duplicate (Test Branch)", use_container_width=True):
            st.markdown("#### 🧪 Clone Deck for Testing")
            st.caption("Create a testing branch to safely experiment with land balances, AI card swaps, or cuts without altering your primary deck.")
            quick_dup_val = st.text_input("Branch Name", value=f"{deck_name} (Test Branch)", key=f"top_dup_{deck_id}")
            if st.button("🚀 Create Branch & Switch", key=f"btn_top_dup_{deck_id}", type="primary", use_container_width=True):
                if quick_dup_val.strip():
                    new_b_name = quick_dup_val.strip()
                    execute_db("INSERT INTO decks (name, commander, partner_commander) VALUES (?, ?, ?)",
                               (new_b_name, current_commander, current_partner))
                    conn = sqlite3.connect(DB_NAME)
                    new_d_id = pd.read_sql_query("SELECT id FROM decks WHERE name = ?", conn, params=(new_b_name,)).iloc[0]['id']
                    conn.close()
                    for _, r in df_deck.iterrows():
                        execute_db("INSERT INTO deck_cards (deck_id, card_name, quantity) VALUES (?, ?, ?)",
                                   (int(new_d_id), r['card_name'], int(r['quantity'])))
                    st.session_state.selected_deck_display = new_b_name
                    st.success(f"Duplicated deck as '{new_b_name}'!")
                    st.rerun()

    if current_commander:
        # If Commander supports Partner or Background, display dual slots
        if partner_type is not None:
            c_col1, c_col2 = st.columns(2)

            # Slot 1: Primary Commander (with flip support)
            with c_col1:
                c_idx, c_disp, c_has_faces, c_faces = get_card_display_details(current_commander, cmdr_data, scope=f"cmdr_{deck_id}")
                sub1, sub2 = st.columns([1, 2])
                with sub1:
                    st.image(c_disp["image_url"], use_container_width=True)
                    if c_has_faces:
                        other_face_name = c_faces[1 - c_idx]["name"]
                        if st.button(f"🔄 Flip ({other_face_name})", key=f"btn_flip_cmdr_{deck_id}", use_container_width=True):
                            toggle_card_face(current_commander, scope=f"cmdr_{deck_id}")
                            st.rerun()
                with sub2:
                    st.markdown(f"#### 👑 **{c_disp['name']}**")
                    cost_str = c_disp['mana_cost']
                    type_str = c_disp['type_line']
                    cmc_num = c_disp.get('cmc', 0)
                    st.caption(f"{cost_str} • CMC {cmc_num:g} • {type_str}")
                    c_emojis = " ".join([COLOR_EMOJI_MAP.get(c, c) for c in sorted(cmdr_ci)]) if cmdr_ci else "🔘 Colorless"
                    st.caption(f"Colors: {c_emojis}")
                    cmdr_kw = c_disp.get('keywords', '')
                    if cmdr_kw and str(cmdr_kw).strip():
                        st.caption(f"🏷️ `{cmdr_kw}`")
                    if c_disp['oracle_text']:
                        with st.expander(f"📜 Text ({c_disp['name']})"):
                            st.markdown(f"> *{str(c_disp['oracle_text']).replace(chr(10), '<br>') }*", unsafe_allow_html=True)
                    with st.popover("⚙️ Modify Commander"):
                        st.markdown("**Choose New Commander**")
                        leg_df = run_query("SELECT card_name FROM collection WHERE (type_line LIKE '%Legendary%Creature%' OR type_line LIKE '%Planeswalker%') ORDER BY card_name ASC")
                        leg_opts = ["-- Select from Collection --"] + (leg_df['card_name'].tolist() if not leg_df.empty else [])
                        ch_leg = st.selectbox("From Collection", leg_opts, key=f"ch_leg_{deck_id}")
                        ch_custom = st.text_input("Or enter Card Name", placeholder="e.g. Atraxa", key=f"ch_custom_{deck_id}")
                        if st.button("Update Commander", key=f"btn_upd_cmdr_{deck_id}"):
                            picked = ch_custom.strip() if ch_custom.strip() else (ch_leg if ch_leg != "-- Select from Collection --" else "")
                            if picked:
                                scry = fetch_scryfall_data(picked)
                                if scry:
                                    execute_db("UPDATE decks SET commander = ? WHERE id = ?", (scry['name'], deck_id))
                                    add_card_to_deck(deck_id, scry['name'], 1)
                                    add_card_to_collection(scry['name'], 1)
                                    st.success(f"Commander updated to {scry['name']}!")
                                    st.rerun()
                                else:
                                    st.error("Card not found on Scryfall.")
                        if st.button("Remove Commander", key=f"btn_clear_cmdr_{deck_id}"):
                            execute_db("UPDATE decks SET commander = NULL WHERE id = ?", (deck_id,))
                            st.rerun()

            # Slot 2: Secondary Commander (Partner or Background with flip support)
            with c_col2:
                badge_title = "🏰 Background" if partner_type == "background" else "🤝 Partner Commander"
                if current_partner:
                    p_idx, p_disp, p_has_faces, p_faces = get_card_display_details(current_partner, partner_data, scope=f"partner_{deck_id}")
                    psub1, psub2 = st.columns([1, 2])
                    with psub1:
                        st.image(p_disp["image_url"], use_container_width=True)
                        if p_has_faces:
                            other_p_name = p_faces[1 - p_idx]["name"]
                            if st.button(f"🔄 Flip ({other_p_name})", key=f"btn_flip_part_{deck_id}", use_container_width=True):
                                toggle_card_face(current_partner, scope=f"partner_{deck_id}")
                                st.rerun()
                    with psub2:
                        st.markdown(f"#### {badge_title}: **{p_disp['name']}**")
                        pcost = p_disp.get('mana_cost', '')
                        ptype = p_disp.get('type_line', '')
                        st.caption(f"{pcost} • CMC {p_disp.get('cmc', 0):g} • {ptype}")
                        p_emojis = " ".join([COLOR_EMOJI_MAP.get(c, c) for c in sorted(partner_ci)]) if partner_ci else "🔘 Colorless"
                        st.caption(f"Colors: {p_emojis}")
                        p_kw = p_disp.get('keywords', '')
                        if p_kw and str(p_kw).strip():
                            st.caption(f"🏷️ `{p_kw}`")
                        if p_disp['oracle_text']:
                            with st.expander(f"📜 Text ({p_disp['name']})"):
                                st.markdown(f"> *{str(p_disp['oracle_text']).replace(chr(10), '<br>') }*", unsafe_allow_html=True)
                        with st.popover(f"⚙️ Modify {badge_title}"):
                            st.markdown(f"**Choose New {badge_title}**")
                            if partner_type == "background":
                                p_df = run_query("SELECT card_name FROM collection WHERE type_line LIKE '%Background%' ORDER BY card_name ASC")
                            else:
                                p_df = run_query("SELECT card_name FROM collection WHERE (type_line LIKE '%Legendary%' OR type_line LIKE '%Planeswalker%') ORDER BY card_name ASC")
                            p_opts = ["-- Select from Collection --"] + (p_df['card_name'].tolist() if not p_df.empty else [])
                            ch_p_leg = st.selectbox(f"From Collection", p_opts, key=f"ch_p_leg_{deck_id}")
                            ch_p_custom = st.text_input("Or enter Card Name", placeholder="Card name", key=f"ch_p_custom_{deck_id}")
                            if st.button(f"Update {badge_title}", key=f"btn_upd_partner_{deck_id}"):
                                p_picked = ch_p_custom.strip() if ch_p_custom.strip() else (ch_p_leg if ch_p_leg != "-- Select from Collection --" else "")
                                if p_picked:
                                    s_p = fetch_scryfall_data(p_picked)
                                    if s_p:
                                        remove_card_from_deck(deck_id, current_partner, remove_all=True)
                                        execute_db("UPDATE decks SET partner_commander = ? WHERE id = ?", (s_p['name'], deck_id))
                                        add_card_to_deck(deck_id, s_p['name'], 1)
                                        add_card_to_collection(s_p['name'], 1)
                                        st.success(f"{badge_title} updated to {s_p['name']}!")
                                        st.rerun()
                                    else:
                                        st.error("Card not found on Scryfall.")
                            if st.button(f"Remove {badge_title}", key=f"btn_clear_partner_{deck_id}"):
                                remove_card_from_deck(deck_id, current_partner, remove_all=True)
                                execute_db("UPDATE decks SET partner_commander = NULL WHERE id = ?", (deck_id,))
                                st.rerun()
                else:
                    # Partner / Background keyword present on commander, but no partner assigned yet
                    with st.container(border=True):
                        if partner_type == "background":
                            st.markdown("#### 🏰 Choose a Background")
                            st.caption(f"**{current_commander}** has *Choose a Background*! Add a Legendary Background Enchantment as a second commander.")
                            bg_df = run_query("SELECT card_name FROM collection WHERE type_line LIKE '%Background%' ORDER BY card_name ASC")
                            bg_opts = ["-- Select Background from Binder --"] + (bg_df['card_name'].tolist() if not bg_df.empty else [])
                            ch_bg = st.selectbox("From Collection", bg_opts, key=f"sel_bg_{deck_id}")
                            ch_bg_name = st.text_input("Or enter Background Name", placeholder="e.g. Raised by Giants", key=f"txt_bg_{deck_id}")
                            if st.button("Set Background", key=f"btn_set_bg_{deck_id}"):
                                picked_bg = ch_bg_name.strip() if ch_bg_name.strip() else (ch_bg if ch_bg != "-- Select Background from Binder --" else "")
                                if picked_bg:
                                    sc_bg = fetch_scryfall_data(picked_bg)
                                    if sc_bg:
                                        execute_db("UPDATE decks SET partner_commander = ? WHERE id = ?", (sc_bg['name'], deck_id))
                                        add_card_to_deck(deck_id, sc_bg['name'], 1)
                                        add_card_to_collection(sc_bg['name'], 1)
                                        st.success(f"Background set to **{sc_bg['name']}**!")
                                        st.rerun()
                                    else:
                                        st.error("Card not found on Scryfall. Check spelling!")
                        else:
                            st.markdown("#### 🤝 Choose a Partner")
                            st.caption(f"**{current_commander}** has *Partner*! You may choose a second partner commander.")
                            part_df = run_query("SELECT card_name FROM collection WHERE (type_line LIKE '%Legendary%' OR type_line LIKE '%Planeswalker%') AND (keywords LIKE '%Partner%' OR oracle_text LIKE '%Partner%' OR keywords LIKE '%Friends forever%' OR oracle_text LIKE '%Friends forever%' OR keywords LIKE '%Doctor%') ORDER BY card_name ASC")
                            part_opts = ["-- Select Partner from Binder --"] + (part_df['card_name'].tolist() if not part_df.empty else [])
                            ch_pr = st.selectbox("From Collection", part_opts, key=f"sel_pr_{deck_id}")
                            ch_pr_name = st.text_input("Or enter Partner Name", placeholder="e.g. Thrasios, Triton Hero", key=f"txt_pr_{deck_id}")
                            if st.button("Set Partner Commander", key=f"btn_set_pr_{deck_id}"):
                                picked_pr = ch_pr_name.strip() if ch_pr_name.strip() else (ch_pr if ch_pr != "-- Select Partner from Binder --" else "")
                                if picked_pr:
                                    sc_pr = fetch_scryfall_data(picked_pr)
                                    if sc_pr:
                                        execute_db("UPDATE decks SET partner_commander = ? WHERE id = ?", (sc_pr['name'], deck_id))
                                        add_card_to_deck(deck_id, sc_pr['name'], 1)
                                        add_card_to_collection(sc_pr['name'], 1)
                                        st.success(f"Partner commander set to **{sc_pr['name']}**!")
                                        st.rerun()
                                    else:
                                        st.error("Card not found on Scryfall. Check spelling!")
        else:
            # Single commander banner (no partner/background keyword on commander)
            c_idx, c_disp, c_has_faces, c_faces = get_card_display_details(current_commander, cmdr_data, scope=f"cmdr_{deck_id}")
            c_art, c_info, c_action = st.columns([1, 4, 2])
            with c_art:
                st.image(c_disp["image_url"], width=130)
                if c_has_faces:
                    other_face_name = c_faces[1 - c_idx]["name"]
                    if st.button(f"🔄 Flip ({other_face_name})", key=f"btn_flip_single_cmdr_{deck_id}"):
                        toggle_card_face(current_commander, scope=f"cmdr_{deck_id}")
                        st.rerun()
            with c_info:
                st.markdown(f"### 👑 Commander: **{c_disp['name']}**")
                cost_str = c_disp['mana_cost']
                type_str = c_disp['type_line']
                cmc_num = c_disp.get('cmc', 0)
                st.caption(f"{cost_str} • CMC {cmc_num:g} • {type_str}")
                cmdr_kw = c_disp.get('keywords', '')
                if cmdr_kw and str(cmdr_kw).strip():
                    st.markdown(f"**Keywords:** `{cmdr_kw}`")
                if c_disp['oracle_text']:
                    with st.expander(f"📜 Rules Text ({c_disp['name']})"):
                        st.markdown(f"> *{str(c_disp['oracle_text']).replace(chr(10), '<br>') }*", unsafe_allow_html=True)
            with c_action:
                with st.popover("👑 Change Commander"):
                    st.markdown("**Select New Commander**")
                    leg_df = run_query("SELECT card_name FROM collection WHERE (type_line LIKE '%Legendary%Creature%' OR type_line LIKE '%Planeswalker%') ORDER BY card_name ASC")
                    leg_opts = ["-- Select from Collection --"] + (leg_df['card_name'].tolist() if not leg_df.empty else [])
                    ch_leg = st.selectbox("From Collection", leg_opts, key=f"ch_leg_{deck_id}")
                    ch_custom = st.text_input("Or enter Card Name", placeholder="e.g. Atraxa", key=f"ch_custom_{deck_id}")
                    if st.button("Update Commander", key=f"btn_upd_cmdr_{deck_id}"):
                        picked = ch_custom.strip() if ch_custom.strip() else (ch_leg if ch_leg != "-- Select from Collection --" else "")
                        if picked:
                            scry = fetch_scryfall_data(picked)
                            if scry:
                                execute_db("UPDATE decks SET commander = ? WHERE id = ?", (scry['name'], deck_id))
                                add_card_to_deck(deck_id, scry['name'], 1)
                                add_card_to_collection(scry['name'], 1)
                                st.success(f"Commander updated to {scry['name']}!")
                                st.rerun()
                            else:
                                st.error("Card not found on Scryfall.")
                    if st.button("Remove Commander", key=f"btn_clear_cmdr_{deck_id}"):
                        execute_db("UPDATE decks SET commander = NULL WHERE id = ?", (deck_id,))
                        st.rerun()

            # If deck previously had a partner but commander was changed to someone without partner/background
            if current_partner:
                st.warning(f"⚠️ **Note:** {current_commander} does not have the Partner or Background keyword, but **{current_partner}** is set as a partner.")
                if st.button("Remove Inactive Partner Card", key=f"purge_incompat_part_{deck_id}"):
                    remove_card_from_deck(deck_id, current_partner, remove_all=True)
                    execute_db("UPDATE decks SET partner_commander = NULL WHERE id = ?", (deck_id,))
                    st.rerun()

        # Color identity summary
        ci_str = " ".join([COLOR_EMOJI_MAP.get(c, c) for c in sorted(combined_ci)]) if combined_ci else "🔘 Colorless"
        ci_letters = ", ".join(sorted(combined_ci)) if combined_ci else "Colorless"
        st.caption(f"🛡️ **Combined Color Identity:** {ci_str} `[{ci_letters}]` — cards added from your collection must match this identity.")
    else:
        st.warning("⚠️ **No Commander Assigned Yet!** Choose a Commander below to establish this deck's legal Color Identity.")
        with st.expander("👑 Choose Commander for this Deck", expanded=(total_cards == 0)):
            leg_df = run_query("SELECT card_name FROM collection WHERE (type_line LIKE '%Legendary%Creature%' OR type_line LIKE '%Planeswalker%') ORDER BY card_name ASC")
            leg_opts = ["-- Select from Collection --"] + (leg_df['card_name'].tolist() if not leg_df.empty else [])
            col_c1, col_c2 = st.columns(2)
            with col_c1:
                ch_leg = st.selectbox("From Collection", leg_opts, key=f"init_leg_{deck_id}")
            with col_c2:
                ch_custom = st.text_input("Or enter Commander Name", placeholder="e.g. Atraxa, Praetors' Voice", key=f"init_custom_{deck_id}")
            if st.button("Set Commander", key=f"btn_set_cmdr_{deck_id}"):
                picked = ch_custom.strip() if ch_custom.strip() else (ch_leg if ch_leg != "-- Select from Collection --" else "")
                if picked:
                    scry = fetch_scryfall_data(picked)
                    if scry:
                        execute_db("UPDATE decks SET commander = ? WHERE id = ?", (scry['name'], deck_id))
                        add_card_to_deck(deck_id, scry['name'], 1)
                        add_card_to_collection(scry['name'], 1)
                        st.success(f"Commander set to **{scry['name']}**!")
                        st.rerun()
                    else:
                        st.error("Card not found on Scryfall. Check spelling!")

    # ---------------------------------------------------------
    # METRICS BAR & 100-CARD PROGRESS
    # ---------------------------------------------------------
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("🃏 Total Cards", f"{total_cards} / 100")
    m2.metric("🗃️ Unique Titles", f"{unique_cards}")
    m3.metric("⚡ Avg Mana Value", f"{avg_cmc:.2f}")
    m4.metric("🏔️ Lands / Spells", f"{total_lands} / {total_non_land}")
    if combined_ci is not None:
        ci_display = " ".join([COLOR_EMOJI_MAP.get(c, c) for c in sorted(combined_ci)]) if combined_ci else "🔘 Colorless"
    else:
        ci_display = "⚠️ Not Set"
    m5.metric("🎨 Identity", ci_display)

    progress_val = min(1.0, max(0.0, total_cards / 100.0))
    if total_cards == 100:
        st.progress(progress_val, text="✅ Perfect 100-Card Commander Deck")
    elif total_cards < 100:
        st.progress(progress_val, text=f"⏳ {total_cards}/100 Cards ({100 - total_cards} slots remaining)")
    else:
        st.progress(1.0, text=f"⚠️ {total_cards}/100 Cards ({total_cards - 100} cards over the 100-card limit)")

    st.markdown("---")

    # ---------------------------------------------------------
    # TABS FOR DECK VIEWS, OPTIMIZATION AND ACTIONS
    # ---------------------------------------------------------
    tab_add, tab_visual, tab_categorized, tab_stats, tab_lands, tab_optimize, tab_export = st.tabs([
        "➕ Add Cards from Collection",
        "🖼️ Visual Gallery", 
        "📋 Categorized Decklist", 
        "📊 Mana Curve & Analytics",
        "🏔️ Land Base Balancer",
        "🤖 AI Optimizer (Ollama)",
        "📤 Export Decklist"
    ])

    # -------------------------
    # TAB 1: ADD CARDS FROM COLLECTION
    # -------------------------
    with tab_add:
        # Quick Add Basic Lands feature
        with st.expander("🏔️ Quick Add Basic Lands (Auto-Stocks Binder)", expanded=False):
            st.caption("Quickly add basic lands to your deck. If you don't have enough physical copies in your binder, they will be automatically added to your binder.")
            allowed_basics = []
            if combined_ci is not None:
                for b_name, b_colors in [("Plains", {"W"}), ("Island", {"U"}), ("Swamp", {"B"}), ("Mountain", {"R"}), ("Forest", {"G"})]:
                    if b_colors.issubset(combined_ci):
                        allowed_basics.append(b_name)
                allowed_basics.append("Wastes")
            else:
                allowed_basics = ["Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"]

            col_bl1, col_bl2, col_bl3 = st.columns([2, 1, 1])
            with col_bl1:
                chosen_basic = st.selectbox("Basic Land", allowed_basics, key=f"quick_basic_sel_{deck_id}")
            with col_bl2:
                basic_qty = st.number_input("Count", min_value=1, max_value=99, value=1, step=1, key=f"quick_basic_qty_{deck_id}")
            with col_bl3:
                st.write("")
                if st.button("➕ Add Lands to Deck", key=f"quick_basic_btn_{deck_id}", use_container_width=True):
                    res_avail = run_query("SELECT quantity_available FROM v_available_cards WHERE card_name = ?", (chosen_basic,))
                    curr_avail = int(res_avail.iloc[0]['quantity_available']) if not res_avail.empty else 0
                    needed = basic_qty - curr_avail
                    if needed > 0:
                        add_card_to_collection(chosen_basic, needed)
                    add_card_to_deck(deck_id, chosen_basic, basic_qty)
                    st.toast(f"Added {basic_qty}x {chosen_basic} to deck!", icon="🏔️")
                    st.rerun()

        st.subheader("➕ Add Cards to Deck from Collection")

        df_col = run_query("""
            SELECT 
                card_name,
                cmc,
                type_line,
                color_identity,
                image_url,
                mana_cost,
                oracle_text,
                keywords,
                faces_json,
                total_owned,
                total_assigned,
                quantity_available
            FROM v_available_cards
            ORDER BY card_name ASC
        """)

        if df_col.empty:
            st.info("Your collection binder is empty! Add cards to your binder in **📚 My Binder** first.")
        else:
            if combined_ci is not None:
                df_legal = df_col[df_col.apply(
                    lambda r: is_card_legal_for_commander(r['card_name'], r['color_identity'], combined_ci),
                    axis=1
                )].copy()
                st.caption(f"🛡️ Showing **{len(df_legal)}** collection cards matching the deck's color identity ({', '.join(sorted(combined_ci)) if combined_ci else 'Colorless'}).")
            else:
                df_legal = df_col.copy()
                st.warning("⚠️ No Commander assigned! Assign a Commander above to filter by legal color identity.")

            if df_legal.empty:
                st.warning("No cards in your collection match this deck's color identity. Add matching cards in **📚 My Binder**!")
            else:
                col_search_txt, col_type_sel, col_avail_chk, col_grid_cols = st.columns([3, 2, 2, 1])
                with col_search_txt:
                    s_term = st.text_input("🔍 Search Collection (Name, Text, Keyword)", placeholder="e.g. Sol Ring, Flying, Draw...", key=f"col_search_{deck_id}")
                with col_type_sel:
                    t_choice = st.selectbox("Category", ["All Types", "Creatures", "Planeswalkers", "Instants", "Sorceries", "Artifacts", "Enchantments", "Lands"], key=f"col_cat_{deck_id}")
                with col_avail_chk:
                    only_avail = st.checkbox("Only unassigned (>0)", value=False, key=f"only_free_{deck_id}", help="Only show cards that currently have copies available in binder")
                with col_grid_cols:
                    grid_cols_num = st.selectbox("Cards/Row", [3, 4, 5, 6], index=1, key=f"col_grid_n_{deck_id}")

                filtered_legal = df_legal.copy()
                if s_term:
                    term_lower = s_term.lower().strip()
                    filtered_legal = filtered_legal[
                        filtered_legal['card_name'].str.lower().str.contains(term_lower) |
                        filtered_legal['oracle_text'].fillna('').str.lower().str.contains(term_lower) |
                        filtered_legal['keywords'].fillna('').str.lower().str.contains(term_lower)
                    ]
                if t_choice != "All Types":
                    filtered_legal['cat'] = filtered_legal['type_line'].apply(get_card_category)
                    filtered_legal = filtered_legal[filtered_legal['cat'] == t_choice]
                if only_avail:
                    filtered_legal = filtered_legal[filtered_legal['quantity_available'] > 0]

                if filtered_legal.empty:
                    st.info("No cards found matching your search/filter criteria.")
                else:
                    PAGE_SIZE = 24
                    total_items = len(filtered_legal)
                    total_pages = max(1, math.ceil(total_items / PAGE_SIZE))
                    page_key = f"add_page_{deck_id}"
                    curr_page = st.session_state.get(page_key, 1)
                    if curr_page > total_pages:
                        curr_page = total_pages
                    if curr_page < 1:
                        curr_page = 1
                    st.session_state[page_key] = curr_page

                    # Pagination controls (top)
                    if total_pages > 1:
                        col_p1, col_p2, col_p3 = st.columns([1, 2, 1])
                        with col_p1:
                            if st.button("◀ Previous Page", key=f"btn_prev_page_{deck_id}", disabled=(curr_page <= 1), use_container_width=True):
                                st.session_state[page_key] = curr_page - 1
                                st.rerun()
                        with col_p2:
                            st.markdown(f"<div style='text-align:center; padding-top:6px; font-weight:bold;'>Page {curr_page} of {total_pages} ({total_items} cards found)</div>", unsafe_allow_html=True)
                        with col_p3:
                            if st.button("Next Page ▶", key=f"btn_next_page_{deck_id}", disabled=(curr_page >= total_pages), use_container_width=True):
                                st.session_state[page_key] = curr_page + 1
                                st.rerun()
                    else:
                        st.caption(f"Showing **{total_items}** matching cards in your collection:")

                    start_idx = (curr_page - 1) * PAGE_SIZE
                    end_idx = min(start_idx + PAGE_SIZE, total_items)
                    page_cards = filtered_legal.iloc[start_idx:end_idx]

                    # Card Grid
                    grid_rows = [page_cards.iloc[i:i + grid_cols_num] for i in range(0, len(page_cards), grid_cols_num)]
                    for r_df in grid_rows:
                        cols = st.columns(grid_cols_num)
                        for idx, (_, card_row) in enumerate(r_df.iterrows()):
                            with cols[idx]:
                                with st.container(border=True):
                                    c_name = card_row['card_name']
                                    free_qty = int(card_row['quantity_available'])
                                    total_qty = int(card_row['total_owned'])
                                    is_basic = c_name.lower().strip() in BASIC_LANDS

                                    # Check deck presence
                                    already_in_deck = 0
                                    if not df_deck.empty and c_name in df_deck['card_name'].values:
                                        already_in_deck = int(df_deck[df_deck['card_name'] == c_name]['quantity'].iloc[0])

                                    f_idx, card_disp, has_faces, faces = get_card_display_details(c_name, card_row, scope=f"add_{deck_id}")
                                    cost_lbl = card_disp['mana_cost'] if card_disp['mana_cost'] else f"CMC {card_disp.get('cmc', 0):g}"

                                    try:
                                        st.image(card_disp["image_url"], use_container_width=True)
                                    except Exception:
                                        st.caption("Image unavailable")

                                    st.markdown(f"**{card_disp['name']}**")
                                    st.caption(f"{cost_lbl} • {card_disp['type_line']}")

                                    # Availability Badge
                                    if free_qty > 0:
                                        st.caption(f"🟢 **{free_qty}** free / {total_qty} owned")
                                    else:
                                        st.caption(f"🔴 **0** free / {total_qty} owned")

                                    # Deck presence alert
                                    if already_in_deck > 0:
                                        if is_basic:
                                            st.caption(f"🏔️ **{already_in_deck}** in this deck")
                                        else:
                                            st.caption(f"⚠️ **{already_in_deck}** in deck (Singleton)")

                                    # Card tools row (Flip, Oracle Text popover, Scryfall link)
                                    c_tool1, c_tool2, c_tool3 = st.columns([1, 1, 1])
                                    with c_tool1:
                                        if has_faces:
                                            other_face_title = faces[1 - f_idx]["name"]
                                            if st.button("🔄", key=f"g_flip_{deck_id}_{c_name}", help=f"Flip: {other_face_title}"):
                                                toggle_card_face(c_name, scope=f"add_{deck_id}")
                                                st.rerun()
                                        else:
                                            st.write("")
                                    with c_tool2:
                                        with st.popover("📜", help="Rules Text & Keywords"):
                                            st.markdown(f"**{card_disp['name']}**")
                                            st.caption(f"{cost_lbl} • {card_disp['type_line']}")
                                            kw_info = card_disp.get('keywords', '')
                                            if kw_info and pd.notna(kw_info) and str(kw_info).strip():
                                                st.markdown(f"**Keywords:** `{kw_info}`")
                                            txt_info = card_disp.get('oracle_text', '')
                                            if txt_info and pd.notna(txt_info) and str(txt_info).strip():
                                                st.markdown(f"> *{str(txt_info).replace(chr(10), '<br>') }*", unsafe_allow_html=True)
                                            if has_faces:
                                                st.divider()
                                                st.caption(f"Showing Face {f_idx + 1} of 2")
                                    with c_tool3:
                                        scry_link = f"https://scryfall.com/search?q=%21%22{requests.utils.quote(c_name)}%22"
                                        st.markdown(f"<div style='text-align:right; padding-top:4px;'><a href='{scry_link}' target='_blank' style='text-decoration:none;'>↗</a></div>", unsafe_allow_html=True)

                                    # Add to Deck Button / Controls
                                    if is_basic:
                                        col_bq, col_ba = st.columns([1, 2])
                                        with col_bq:
                                            b_qty_in = st.number_input("Qty", min_value=1, max_value=99, value=1, step=1, key=f"g_bqty_{deck_id}_{c_name}", label_visibility="collapsed")
                                        with col_ba:
                                            if st.button("➕ Add", key=f"g_add_{deck_id}_{c_name}", use_container_width=True):
                                                add_card_to_deck(deck_id, c_name, b_qty_in)
                                                st.toast(f"Added {b_qty_in}x {c_name} to deck!", icon="🏔️")
                                                st.rerun()
                                    else:
                                        if st.button("➕ Add to Deck", key=f"g_add_{deck_id}_{c_name}", use_container_width=True):
                                            add_card_to_deck(deck_id, c_name, 1)
                                            if free_qty <= 0:
                                                st.toast(f"Added {c_name} (⚠️ 0 free in binder)", icon="⚠️")
                                            else:
                                                st.toast(f"Added {c_name} to deck!", icon="✅")
                                            st.rerun()

                    # Bottom pagination controls if multi-page
                    if total_pages > 1:
                        st.write("")
                        col_bp1, col_bp2, col_bp3 = st.columns([1, 2, 1])
                        with col_bp1:
                            if st.button("◀ Previous Page", key=f"btn_prev_page_bot_{deck_id}", disabled=(curr_page <= 1), use_container_width=True):
                                st.session_state[page_key] = curr_page - 1
                                st.rerun()
                        with col_bp2:
                            st.markdown(f"<div style='text-align:center; padding-top:6px; font-weight:bold;'>Page {curr_page} of {total_pages}</div>", unsafe_allow_html=True)
                        with col_bp3:
                            if st.button("Next Page ▶", key=f"btn_next_page_bot_{deck_id}", disabled=(curr_page >= total_pages), use_container_width=True):
                                st.session_state[page_key] = curr_page + 1
                                st.rerun()

        # Cards currently in deck section with quick remove
        if not df_deck.empty:
            st.divider()
            st.subheader(f"📋 Cards Currently in this Deck ({total_cards} Total)")
            for _, r in df_deck.iterrows():
                c_q, c_n, c_t, c_cost, c_act1, c_act2 = st.columns([1, 4, 3, 2, 1, 1])
                with c_q:
                    st.markdown(f"**{r['quantity']}x**")
                with c_n:
                    scry_link = f"https://scryfall.com/search?q=%21%22{requests.utils.quote(r['card_name'])}%22"
                    st.markdown(f"[{r['card_name']}]({scry_link})")
                with c_t:
                    st.caption(r['type_line'])
                with c_cost:
                    cost_str = r['mana_cost'] if r['mana_cost'] else f"CMC {r['cmc']:g}"
                    st.caption(cost_str)
                with c_act1:
                    if st.button("➖ 1", key=f"qm_sub_{deck_id}_{r['card_name']}", help="Remove 1 copy and return to collection"):
                        remove_card_from_deck(deck_id, r['card_name'], remove_all=False)
                        st.toast(f"Removed 1x {r['card_name']} — returned to collection!", icon="↩️")
                        st.rerun()
                with c_act2:
                    if st.button("🗑️", key=f"qm_del_{deck_id}_{r['card_name']}", help="Remove all copies and return to collection"):
                        remove_card_from_deck(deck_id, r['card_name'], remove_all=True)
                        st.toast(f"Removed {r['card_name']} — returned to collection!", icon="↩️")
                        st.rerun()

    # -------------------------
    # TAB 2: VISUAL GALLERY (WITH FLIP BUTTON & DETAILS FOR TWO-FACED CARDS)
    # -------------------------
    with tab_visual:
        if df_deck.empty:
            st.info("No cards in this deck yet! Use the '➕ Add Cards from Collection' tab above to add cards.")
        else:
            col_f1, col_f2, col_f3, col_f4 = st.columns([2, 2, 1, 1])
            with col_f1:
                cat_filter = st.selectbox(
                    "Filter Category",
                    ["All Categories"] + [name for name, _ in CATEGORY_ORDER],
                    key=f"vis_cat_{deck_id}"
                )
            with col_f2:
                search_query = st.text_input("Search card name", placeholder="e.g. Sol Ring", key=f"vis_search_{deck_id}")
            with col_f3:
                grid_cols = st.selectbox("Cards / Row", [3, 4, 5, 6], index=1, key=f"vis_cols_{deck_id}")
            with col_f4:
                group_sections = st.checkbox("Section Headers", value=True, key=f"vis_sec_{deck_id}")

            filtered_cards = df_deck.copy()
            if cat_filter != "All Categories":
                filtered_cards = filtered_cards[filtered_cards['category'] == cat_filter]
            if search_query:
                filtered_cards = filtered_cards[filtered_cards['card_name'].str.lower().str.contains(search_query.lower().strip())]

            if filtered_cards.empty:
                st.warning("No cards found matching the criteria.")
            else:
                def render_gallery_card_col(row, col_container):
                    with col_container:
                        f_idx, disp, has_faces, faces = get_card_display_details(row['card_name'], row, scope=f"vis_{deck_id}")
                        try:
                            st.image(disp["image_url"], use_container_width=True)
                        except Exception:
                            st.caption("Image not available")

                        st.markdown(f"**{row['quantity']}x {disp['name']}**")
                        cost_label = disp['mana_cost'] if disp['mana_cost'] else f"CMC {disp['cmc']:g}"
                        st.caption(f"{cost_label} • {disp['type_line']}")

                        if disp.get('power') and disp.get('toughness'):
                            st.caption(f"⚔️ {disp['power']}/{disp['toughness']}")

                        kw_val = disp.get('keywords', '')
                        if kw_val and pd.notna(kw_val) and str(kw_val).strip():
                            kw_badges = " ".join([f"`{k.strip()}`" for k in str(kw_val).split(",") if k.strip()][:3])
                            st.caption(f"🏷️ {kw_badges}")

                        # Action bar with flip button for double-faced cards
                        if has_faces:
                            col_r1, col_flip, col_r2, col_r3 = st.columns([1, 1, 1, 1])
                            with col_flip:
                                other_face_n = faces[1 - f_idx]["name"]
                                if st.button("🔄", key=f"vis_flip_{deck_id}_{row['card_name']}", help=f"Flip to other face: {other_face_n}"):
                                    toggle_card_face(row['card_name'], scope=f"vis_{deck_id}")
                                    st.rerun()
                        else:
                            col_r1, col_r2, col_r3 = st.columns([1, 1, 1])

                        with col_r1:
                            if st.button("➖", key=f"vis_rm_{deck_id}_{row['card_name']}", help="Remove copy"):
                                remove_card_from_deck(deck_id, row['card_name'], remove_all=False)
                                st.toast(f"Removed {row['card_name']} — returned to collection!", icon="↩️")
                                st.rerun()
                        with col_r2:
                            with st.popover("📜", use_container_width=True):
                                st.markdown(f"**{disp['name']}**")
                                st.caption(f"{cost_label} • {disp['type_line']}")
                                if kw_val and pd.notna(kw_val) and str(kw_val).strip():
                                    st.markdown(f"**Keywords:** {kw_val}")
                                if disp.get('oracle_text') and str(disp['oracle_text']).strip():
                                    st.markdown(f"> *{str(disp['oracle_text']).replace(chr(10), '<br>') }*", unsafe_allow_html=True)
                                else:
                                    st.caption("No oracle text on this face.")
                                if has_faces:
                                    st.divider()
                                    st.caption(f"Showing Face {f_idx + 1} of 2 ({disp['name']})")
                        with col_r3:
                            scryfall_url = f"https://scryfall.com/search?q=%21%22{requests.utils.quote(row['card_name'])}%22"
                            st.markdown(f"[↗ Scryfall]({scryfall_url})")

                if group_sections and cat_filter == "All Categories":
                    for cat_name, cat_emoji in CATEGORY_ORDER:
                        cat_group = filtered_cards[filtered_cards['category'] == cat_name]
                        if not cat_group.empty:
                            cat_total_qty = int(cat_group['quantity'].sum())
                            st.markdown(f"#### {cat_emoji} {cat_name} ({cat_total_qty})")
                            cols = st.columns(grid_cols)
                            for idx, (_, row) in enumerate(cat_group.iterrows()):
                                render_gallery_card_col(row, cols[idx % grid_cols])
                else:
                    cols = st.columns(grid_cols)
                    for idx, (_, row) in enumerate(filtered_cards.iterrows()):
                        render_gallery_card_col(row, cols[idx % grid_cols])

    # -------------------------
    # TAB 3: CATEGORIZED DECKLIST (WITH FLIP BUTTON & PREVIEW DETAILS)
    # -------------------------
    with tab_categorized:
        if df_deck.empty:
            st.info("No cards in this deck yet! Use the '➕ Add Cards from Collection' tab above to add cards.")
        else:
            for cat_name, cat_emoji in CATEGORY_ORDER:
                cat_group = df_deck[df_deck['category'] == cat_name]
                if not cat_group.empty:
                    cat_qty = int(cat_group['quantity'].sum())
                    st.markdown(f"### {cat_emoji} {cat_name} ({cat_qty})")

                    for _, row in cat_group.iterrows():
                        f_idx, disp, has_faces, faces = get_card_display_details(row['card_name'], row, scope=f"cat_{deck_id}")
                        c_qty, c_name, c_mana, c_type, c_preview, c_remove = st.columns([1, 4, 2, 4, 2, 1])
                        with c_qty:
                            st.markdown(f"**{row['quantity']}x**")
                        with c_name:
                            scryfall_url = f"https://scryfall.com/search?q=%21%22{requests.utils.quote(row['card_name'])}%22"
                            st.markdown(f"[{disp['name']}]({scryfall_url})")
                        with c_mana:
                            mana_disp = disp['mana_cost'] if disp['mana_cost'] else f"CMC {disp['cmc']:g}"
                            st.write(mana_disp)
                        with c_type:
                            st.write(disp['type_line'])
                        with c_preview:
                            with st.popover("👁️ Preview"):
                                st.image(disp["image_url"], use_container_width=True)
                                if has_faces:
                                    other_face_n = faces[1 - f_idx]["name"]
                                    if st.button(f"🔄 Flip ({other_face_n})", key=f"cat_flip_{deck_id}_{row['card_name']}", use_container_width=True):
                                        toggle_card_face(row['card_name'], scope=f"cat_{deck_id}")
                                        st.rerun()
                                kw_val = disp.get('keywords', '')
                                if kw_val and pd.notna(kw_val) and str(kw_val).strip():
                                    pills = " • ".join([f"`{k.strip()}`" for k in str(kw_val).split(",") if k.strip()])
                                    st.markdown(f"**Keywords:** {pills}")
                                if disp.get('oracle_text') and str(disp['oracle_text']).strip():
                                    st.markdown(f"> *{str(disp['oracle_text']).replace(chr(10), '<br>') }*", unsafe_allow_html=True)
                        with c_remove:
                            if st.button("🗑️", key=f"cat_rm_{deck_id}_{row['card_name']}", help=f"Remove {row['card_name']} and return to collection"):
                                remove_card_from_deck(deck_id, row['card_name'], remove_all=False)
                                st.toast(f"Removed {row['card_name']} — returned to collection!", icon="↩️")
                                st.rerun()
                    st.divider()

    # -------------------------
    # TAB 4: MANA CURVE & ANALYTICS
    # -------------------------
    with tab_stats:
        if df_deck.empty:
            st.info("No cards in deck to analyze.")
        else:
            # Row 1: Mana Curve (CMC) and Card Types Breakdown
            col_c1, col_c2 = st.columns(2)
            with col_c1:
                st.subheader("📈 Mana Curve (CMC)")
                curve_dict = {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0, "7+": 0}
                for _, r in df_non_land.iterrows():
                    val = int(r['cmc']) if pd.notna(r['cmc']) else 0
                    q = int(r['quantity'])
                    if val >= 7:
                        curve_dict["7+"] += q
                    elif str(val) in curve_dict:
                        curve_dict[str(val)] += q
                    else:
                        curve_dict["0"] += q
                df_curve = pd.DataFrame(list(curve_dict.items()), columns=["CMC", "Card Count"])
                cmc_order = ["0", "1", "2", "3", "4", "5", "6", "7+"]
                chart_cmc = (
                    alt.Chart(df_curve)
                    .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color="#6366F1")
                    .encode(
                        x=alt.X("CMC:N", sort=cmc_order, axis=alt.Axis(labelAngle=0, title="CMC (Mana Value)", labelFontSize=12)),
                        y=alt.Y("Card Count:Q", axis=alt.Axis(title="Cards", tickMinStep=1)),
                        tooltip=["CMC", "Card Count"]
                    )
                    .properties(height=260)
                )
                text_cmc = chart_cmc.mark_text(
                    align='center',
                    baseline='bottom',
                    dy=-3,
                    fontWeight='bold'
                ).encode(
                    text=alt.condition(alt.datum["Card Count"] > 0, "Card Count:Q", alt.value(""))
                )
                st.altair_chart(chart_cmc + text_cmc, use_container_width=True)
                st.caption("Distribution of non-land cards by converted mana cost (CMC).")

            with col_c2:
                st.subheader("🧩 Card Types Breakdown")
                type_counts = {}
                for _, r in df_deck.iterrows():
                    c = r['category']
                    type_counts[c] = type_counts.get(c, 0) + int(r['quantity'])
                df_types = pd.DataFrame(list(type_counts.items()), columns=["Card Type", "Count"]).sort_values(by="Count", ascending=False)
                chart_types = (
                    alt.Chart(df_types)
                    .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, color="#0EA5E9")
                    .encode(
                        y=alt.Y("Card Type:N", sort="-x", axis=alt.Axis(labelAngle=0, title=None, labelFontSize=11)),
                        x=alt.X("Count:Q", axis=alt.Axis(title="Cards", tickMinStep=1)),
                        tooltip=["Card Type", "Count"]
                    )
                    .properties(height=260)
                )
                text_types = chart_types.mark_text(
                    align='left',
                    baseline='middle',
                    dx=4,
                    fontWeight='bold'
                ).encode(
                    text="Count:Q"
                )
                st.altair_chart(chart_types + text_types, use_container_width=True)

            # Row 2: Color Breakdown and Keywords Breakdown
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                st.subheader("🎨 Color Breakdown")
                color_counts = {
                    "☀️ White (W)": 0,
                    "🔵 Blue (U)": 0,
                    "⚫ Black (B)": 0,
                    "🔴 Red (R)": 0,
                    "🟢 Green (G)": 0,
                    "🔘 Colorless": 0
                }
                for _, r in df_deck.iterrows():
                    ci = str(r['color_identity']).strip() if pd.notna(r['color_identity']) else ""
                    q = int(r['quantity'])
                    if not ci or ci.lower() == 'nan':
                        color_counts["🔘 Colorless"] += q
                    else:
                        c_list = [c.strip().upper() for c in ci.split(',') if c.strip()]
                        has_color = False
                        if "W" in c_list:
                            color_counts["☀️ White (W)"] += q
                            has_color = True
                        if "U" in c_list:
                            color_counts["🔵 Blue (U)"] += q
                            has_color = True
                        if "B" in c_list:
                            color_counts["⚫ Black (B)"] += q
                            has_color = True
                        if "R" in c_list:
                            color_counts["🔴 Red (R)"] += q
                            has_color = True
                        if "G" in c_list:
                            color_counts["🟢 Green (G)"] += q
                            has_color = True
                        if not has_color:
                            color_counts["🔘 Colorless"] += q

                active_colors = {k: v for k, v in color_counts.items() if v > 0}
                if active_colors:
                    color_palette = {
                        "☀️ White (W)": "#F59E0B",
                        "🔵 Blue (U)": "#3B82F6",
                        "⚫ Black (B)": "#6B7280",
                        "🔴 Red (R)": "#EF4444",
                        "🟢 Green (G)": "#10B981",
                        "🔘 Colorless": "#9CA3AF"
                    }
                    df_colors = pd.DataFrame(list(active_colors.items()), columns=["Color", "Card Count"])
                    chart_colors = (
                        alt.Chart(df_colors)
                        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
                        .encode(
                            y=alt.Y("Color:N", sort=None, axis=alt.Axis(labelAngle=0, title=None, labelFontSize=11)),
                            x=alt.X("Card Count:Q", axis=alt.Axis(title="Cards", tickMinStep=1)),
                            color=alt.Color("Color:N", scale=alt.Scale(
                                domain=list(color_palette.keys()),
                                range=list(color_palette.values())
                            ), legend=None),
                            tooltip=["Color", "Card Count"]
                        )
                        .properties(height=max(180, len(active_colors) * 36))
                    )
                    text_colors = chart_colors.mark_text(
                        align='left',
                        baseline='middle',
                        dx=4,
                        fontWeight='bold'
                    ).encode(
                        text="Card Count:Q"
                    )
                    st.altair_chart(chart_colors + text_colors, use_container_width=True)

            with col_b2:
                # Keywords Breakdown
                st.subheader("🏷️ Deck Keywords Breakdown")
                keyword_counts = {}
                for _, r in df_deck.iterrows():
                    kws = str(r.get('keywords', '')).strip()
                    if kws and kws != 'nan':
                        for kw in kws.split(','):
                            kw_clean = kw.strip().title()
                            if kw_clean:
                                keyword_counts[kw_clean] = keyword_counts.get(kw_clean, 0) + int(r['quantity'])

                if keyword_counts:
                    df_kw = pd.DataFrame(list(keyword_counts.items()), columns=["Keyword", "Card Count"]).sort_values(by="Card Count", ascending=False).head(8)
                    chart_kw = (
                        alt.Chart(df_kw)
                        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, color="#8B5CF6")
                        .encode(
                            y=alt.Y("Keyword:N", sort="-x", axis=alt.Axis(labelAngle=0, title=None, labelFontSize=11)),
                            x=alt.X("Card Count:Q", axis=alt.Axis(title="Cards", tickMinStep=1)),
                            tooltip=["Keyword", "Card Count"]
                        )
                        .properties(height=max(180, len(df_kw) * 32))
                    )
                    text_kw = chart_kw.mark_text(
                        align='left',
                        baseline='middle',
                        dx=4,
                        fontWeight='bold'
                    ).encode(
                        text="Card Count:Q"
                    )
                    st.altair_chart(chart_kw + text_kw, use_container_width=True)
                    kw_badges = " • ".join([f"`{k}: {c}`" for k, c in sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)[:8]])
                    st.caption(f"**Top keywords:** {kw_badges}")
                else:
                    st.caption("No keywords registered on cards in this deck.")

            # Commander Compliance Audit (Checks combined color identity)
            st.divider()
            st.subheader("🛡️ Commander Color Identity Audit")
            if current_commander and combined_ci is not None:
                illegal_list = []
                for _, r in df_deck.iterrows():
                    # Both commanders themselves are legal in command zone
                    if r['card_name'] == current_commander or (current_partner and r['card_name'] == current_partner):
                        continue
                    if not is_card_legal_for_commander(r['card_name'], r['color_identity'], combined_ci):
                        illegal_list.append(r['card_name'])
                if not illegal_list:
                    identity_desc = ', '.join(sorted(combined_ci)) if combined_ci else 'Colorless'
                    cmdr_title = f"**{current_commander}**" + (f" & **{current_partner}**" if current_partner else "")
                    st.success(f"✅ **100% Legal:** All cards match {cmdr_title}'s combined color identity ({identity_desc}).")
                else:
                    st.error(f"⚠️ **{len(illegal_list)} Illegal Cards Detected:** {', '.join(illegal_list)}")
                    if st.button("Purge All Illegal Cards", key=f"purge_illegal_{deck_id}"):
                        for ill in illegal_list:
                            remove_card_from_deck(deck_id, ill, remove_all=True)
                        st.success("Removed all illegal cards and returned them to collection!")
                        st.rerun()
            else:
                st.info("Assign a Commander to run the Color Identity compliance audit.")

    # -------------------------
    # TAB 5: LAND BASE BALANCER & SUGGESTER
    # -------------------------
    with tab_lands:
        st.subheader("🏔️ Automated Land Base Balancer & Suggester")
        st.markdown("""
            Balance your deck's mana base based on **spell colored mana symbols (pips)**, 
            your **existing non-basic lands** (which provide dual/tri-color mana fixing and occupy land slots), 
            and your **custom target total land count**.
        """)

        df_lands = df_deck[df_deck['type_line'].str.lower().str.contains('land')].copy() if not df_deck.empty else pd.DataFrame()
        df_basics = df_lands[df_lands['card_name'].str.lower().isin(BASIC_LANDS)].copy() if not df_lands.empty else pd.DataFrame()
        df_nonbasics = df_lands[~df_lands['card_name'].str.lower().isin(BASIC_LANDS)].copy() if not df_lands.empty else pd.DataFrame()

        cur_basics = int(df_basics['quantity'].sum()) if not df_basics.empty else 0
        cur_nonbasics = int(df_nonbasics['quantity'].sum()) if not df_nonbasics.empty else 0
        cur_total_lands = cur_basics + cur_nonbasics

        # 1. Target Total Lands Configuration
        c_lconf1, c_lconf2 = st.columns([1, 1])
        with c_lconf1:
            st.markdown("#### 🎯 Target Total Lands")
            st.caption("Customize your total land count based on your deck's strategy:")
            
            # Preset buttons
            c_p1, c_p2, c_p3, c_p4 = st.columns(4)
            chosen_preset = None
            with c_p1:
                if st.button("⚡ 32 Lean", key=f"pre_32_{deck_id}", help="Low curve / cEDH (Avg CMC < 2.5)"):
                    chosen_preset = 32
            with c_p2:
                if st.button("⚖️ 36 Standard", key=f"pre_36_{deck_id}", help="Classic balanced Commander standard"):
                    chosen_preset = 36
            with c_p3:
                if st.button("🛡️ 38 Resilient", key=f"pre_38_{deck_id}", help="Midrange / Battlecruiser (Avg CMC ~ 3.5)"):
                    chosen_preset = 38
            with c_p4:
                if st.button("🌱 40 Landfall", key=f"pre_40_{deck_id}", help="Landfall engines or heavy mana curves (Avg CMC > 3.8)"):
                    chosen_preset = 40

            t_val_key = f"target_lands_num_{deck_id}"
            if chosen_preset is not None:
                st.session_state[t_val_key] = chosen_preset

            default_target = st.session_state.get(t_val_key, cur_total_lands if 25 <= cur_total_lands <= 45 else 36)
            target_lands = st.slider(
                "Total Lands Target",
                min_value=25,
                max_value=45,
                value=int(default_target),
                step=1,
                key=t_val_key,
                help="Slide to set your desired total land count (basics + non-basics)."
            )

        with c_lconf2:
            st.markdown("#### ⚖️ Non-Basic Land Weighting Strategy")
            st.caption("How non-basic lands should influence your basic land split:")
            bal_mode_choice = st.radio(
                "Color Distribution Model",
                [
                    "⚖️ Deficit-Compensated (Accounts for Non-Basic Land Colors)",
                    "📊 Pure Spell Pip Proportional (Ignores Non-Basic Colors)"
                ],
                key=f"bal_mode_{deck_id}",
                help="Deficit-Compensated detects which colors your non-basic lands already fix, giving extra basic lands to neglected colors."
            )
            bal_mode = "deficit" if "Deficit" in bal_mode_choice else "pip_only"

        # 2. Land Balance Calculation
        calc_result = calculate_balanced_land_base(
            target_total_lands=target_lands,
            df_deck=df_deck,
            commander_ci=combined_ci,
            mode=bal_mode
        )

        needed_basics = calc_result["needed_basics"]
        pip_counts = calc_result["pip_counts"]
        nb_sources = calc_result["nonbasic_sources"]
        suggested_basics = calc_result["suggested_basics"]
        active_colors = calc_result["active_colors"]

        # 3. High-level Metrics Row
        st.markdown("---")
        m_l1, m_l2, m_l3, m_l4 = st.columns(4)
        m_l1.metric("🎯 Target Total Lands", f"{target_lands}")
        m_l2.metric("🏰 Non-Basic Lands in Deck", f"{cur_nonbasics}")
        m_l3.metric("🏔️ Needed Basic Lands", f"{needed_basics}", delta=f"{needed_basics - cur_basics:+d} vs current basics" if cur_basics else None)
        resulting_deck_size = total_non_land + target_lands
        m_l4.metric("🃏 Projected Deck Size", f"{resulting_deck_size} / 100", delta=f"{resulting_deck_size - 100:+d}" if resulting_deck_size != 100 else "Perfect 100!")

        if cur_nonbasics >= target_lands:
            st.warning(f"⚠️ You already have **{cur_nonbasics}** non-basic lands in the deck, which meets or exceeds your target of **{target_lands}** total lands! To add basic lands, increase your target land count above {cur_nonbasics}.")

        # 4. Color & Pip Analysis Table
        st.markdown("#### 📊 Color Pip Demand vs. Non-Basic Lands & Mana Fixers (Rocks/Dorks)")
        COLOR_NAMES = {
            "W": "White (Plains)",
            "U": "Blue (Island)",
            "B": "Black (Swamp)",
            "R": "Red (Mountain)",
            "G": "Green (Forest)",
            "C": "Colorless (Wastes)"
        }
        BASIC_MAP = {
            "W": "Plains",
            "U": "Island",
            "B": "Swamp",
            "R": "Mountain",
            "G": "Forest",
            "C": "Wastes"
        }

        rd_sources = calc_result.get("rock_dork_sources", {})
        mana_analysis = calc_result.get("mana_analysis", {})

        total_pips = sum([pip_counts.get(c, 0.0) for c in active_colors])
        total_nb_s = sum([nb_sources.get(c, 0) for c in active_colors])
        total_rd_s = sum([rd_sources.get(c, 0) for c in active_colors])

        breakdown_rows = []
        for c in active_colors:
            c_pips = pip_counts.get(c, 0.0)
            pip_pct = (c_pips / total_pips * 100.0) if total_pips > 0 else (100.0 / len(active_colors))
            c_nb = nb_sources.get(c, 0)
            c_rd = rd_sources.get(c, 0)
            b_card = BASIC_MAP.get(c, "Wastes")
            sug_qty = suggested_basics.get(b_card, 0)
            net_sources = c_nb + c_rd + sug_qty
            emoji_str = COLOR_EMOJI_MAP.get(c, "🔘")

            breakdown_rows.append({
                "Color": f"{emoji_str} {COLOR_NAMES.get(c, c)}",
                "Spell Pips": f"{c_pips:g} ({pip_pct:.1f}%)",
                "Non-Basic Lands": f"{c_nb}",
                "Rocks & Dorks (Fixers)": f"{c_rd}",
                "Suggested Basics": f"{sug_qty}x {b_card}",
                "Total Mana Sources": f"{net_sources}"
            })

        if breakdown_rows:
            st.dataframe(pd.DataFrame(breakdown_rows).set_index("Color"), use_container_width=True)

        # Mana Rocks & Dorks (Ramp Engine) Diagnosis
        mana_rocks = mana_analysis.get("mana_rocks", [])
        mana_dorks = mana_analysis.get("mana_dorks", [])
        ramp_spells = mana_analysis.get("ramp_spells", [])
        cheap_ramp = mana_analysis.get("cheap_ramp_count", 0)
        total_ramp = len(mana_rocks) + len(mana_dorks) + len(ramp_spells)

        if total_ramp > 0:
            rec_lands = max(28, min(42, 40 - int(cheap_ramp / 2)))
            st.info(
                f"⚡ **Mana-Fixing Permanents & Ramp Package Detected ({total_ramp} cards):**  \n"
                f"• **{len(mana_rocks)} Mana Rocks** (e.g. Sol Ring, Signets, Talismans) | "
                f"• **{len(mana_dorks)} Mana Dorks** (e.g. Birds of Paradise, Elves) | "
                f"• **{len(ramp_spells)} Ramp Spells** (e.g. Cultivate)  \n"
                f"💡 *Karsten Mana Heuristics*: You run **{cheap_ramp}** low-cost ramp pieces (CMC ≤ 2). "
                f"Every 2 cheap ramp spells offset ~1 land drop, recommending a baseline of **~{rec_lands} lands** "
                f"(effective total mana base: **{target_lands + total_ramp}** sources)."
            )

        # 5. Adjustable Basic Land Allocation & Apply
        st.markdown("#### 🎛️ Proposed Basic Land Allocation")
        st.caption("You can fine-tune the calculated numbers below before applying them to your deck:")

        # Filter basic lands to display based on active colors
        relevant_basics = []
        for c in active_colors:
            b_name = BASIC_MAP.get(c)
            if b_name and b_name not in relevant_basics:
                relevant_basics.append(b_name)
        if not relevant_basics:
            relevant_basics = ["Wastes"]

        alloc_cols = st.columns(max(1, len(relevant_basics)))
        user_adjusted_basics = {}

        for idx, b_name in enumerate(relevant_basics):
            with alloc_cols[idx]:
                sug_val = int(suggested_basics.get(b_name, 0))
                user_val = st.number_input(
                    f"🏔️ {b_name}",
                    min_value=0,
                    max_value=60,
                    value=sug_val,
                    step=1,
                    key=f"input_basic_{deck_id}_{b_name}"
                )
                user_adjusted_basics[b_name] = int(user_val)

        total_adjusted_basics = sum(user_adjusted_basics.values())
        total_resulting_lands = cur_nonbasics + total_adjusted_basics

        col_sum1, col_sum2 = st.columns([2, 1])
        with col_sum1:
            if total_resulting_lands == target_lands:
                st.success(f"✅ **Perfect Match:** {total_adjusted_basics} Basics + {cur_nonbasics} Non-Basics = **{total_resulting_lands} Total Lands** (hits your target of {target_lands}).")
            else:
                diff = total_resulting_lands - target_lands
                st.info(f"ℹ️ **Current Allocation:** {total_adjusted_basics} Basics + {cur_nonbasics} Non-Basics = **{total_resulting_lands} Total Lands** ({diff:+d} from target {target_lands}).")

        with col_sum2:
            st.write("")
            apply_lands_btn = st.button("🚀 Apply Land Base to Deck", type="primary", use_container_width=True, key=f"btn_apply_lands_{deck_id}")

        if apply_lands_btn:
            # 1. Remove all existing basic lands from the deck
            for b in ["Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"]:
                remove_card_from_deck(deck_id, b, remove_all=True)

            # 2. Add adjusted basic lands to the deck (auto-stocking collection if needed)
            for b_name, b_qty in user_adjusted_basics.items():
                if b_qty > 0:
                    res_avail = run_query("SELECT quantity_available FROM v_available_cards WHERE card_name = ?", (b_name,))
                    curr_avail = int(res_avail.iloc[0]['quantity_available']) if not res_avail.empty else 0
                    needed_stock = b_qty - curr_avail
                    if needed_stock > 0:
                        add_card_to_collection(b_name, needed_stock)
                    add_card_to_deck(deck_id, b_name, b_qty)

            st.toast(f"Updated deck land base to {total_resulting_lands} lands ({total_adjusted_basics} basics + {cur_nonbasics} non-basics)!", icon="🏔️")
            st.rerun()

        # 6. Strategic Mana Base Insights
        st.markdown("---")
        with st.expander("💡 Mana Base Diagnostics & Best Practices", expanded=False):
            st.markdown(f"""
            - **Average Mana Value (CMC)**: `{avg_cmc:.2f}` non-land average.
              - Standard Commander recommendations:
                - Avg CMC < 2.5: **30–33 lands**
                - Avg CMC 2.5–3.3: **34–36 lands**
                - Avg CMC 3.3–3.8: **36–38 lands**
                - Avg CMC > 3.8: **38–40+ lands**
            - **Non-Basic Land Coverage**: You currently have **{cur_nonbasics}** non-basic lands providing fixing.
            """)
            for c in active_colors:
                if nb_sources.get(c, 0) == 0:
                    c_name = COLOR_NAMES.get(c, c)
                    st.warning(f"⚠️ **Notice:** You currently have **0 non-basic lands** producing {c_name}. Your mana base will rely strictly on Basic {BASIC_MAP.get(c)} to cast {c_name} spells. Consider adding dual lands or *Command Tower*.")

    with tab_optimize:
        st.subheader("🤖 AI Deck Optimizer (Local Ollama LLM)")
        st.markdown("""
            Analyze your deck and receive intelligent upgrade recommendations from your **physical binder collection**.
            To prevent context window overflow and maximize reasoning performance, cards are evaluated in **compact batches**, 
            saved to a **candidate proposal pool**, and finally **confirmed and synthesized** into an optimal upgrade plan.
            Runs **100% locally via Ollama** strictly using your local database.
        """)

        # 1. Ollama Host & Model Configuration
        c_ol1, c_ol2, c_ol3 = st.columns([2, 2, 1])
        with c_ol1:
            ollama_url = st.text_input("Ollama Host URL", value="http://localhost:11434", key=f"ol_host_{deck_id}")

        is_online, installed_models = get_ollama_status_and_models(ollama_url)

        with c_ol2:
            if is_online and installed_models:
                default_ix = 0
                for idx, m in enumerate(installed_models):
                    if "llama3" in m.lower():
                        default_ix = idx
                        break
                chosen_model = st.selectbox("Select Ollama Model", installed_models, index=default_ix, key=f"ol_mod_sel_{deck_id}")
            else:
                chosen_model = st.text_input("Model Name", value="llama3", key=f"ol_mod_txt_{deck_id}")

        with c_ol3:
            st.write("")
            st.write("")
            if is_online:
                st.success("🟢 Online", icon="✅")
            else:
                st.error("🔴 Offline", icon="⚠️")

        if not is_online:
            st.info("💡 **Ollama not detected?** Start Ollama on your computer with `ollama serve` in a terminal. (Make sure you have pulled a model like `llama3` or `gpt-oss:20b`).")

        # 2. Local Candidate Pool Stats (strictly local DB)
        deck_card_names = set(df_deck['card_name'].tolist()) if not df_deck.empty else set()
        df_all_avail = run_query("""
            SELECT card_name, type_line, mana_cost, cmc, oracle_text, keywords, quantity_available, color_identity
            FROM v_available_cards
            WHERE quantity_available > 0
            ORDER BY cmc ASC, card_name ASC
        """)

        candidate_cards = []
        if not df_all_avail.empty:
            for _, r in df_all_avail.iterrows():
                cn = r['card_name']
                if cn in deck_card_names and cn.lower() not in BASIC_LANDS:
                    continue
                if not is_card_legal_for_commander(cn, r['color_identity'], combined_ci):
                    continue
                candidate_cards.append(r)

        p_col1, p_col2, p_col3 = st.columns(3)
        p_col1.metric("🃏 Current Deck Cards", f"{total_cards} / 100")
        p_col2.metric("📦 Compatible Binder Candidates", f"{len(candidate_cards)}")
        p_col3.metric("🎨 Deck Identity", f"{', '.join(sorted(combined_ci)) if combined_ci else 'Colorless'}")

        # 3. Optional User Prompt & Batch Settings
        user_guidance = st.text_area(
            "💡 Strategic Guidance & Focus Areas (Optional)",
            placeholder="e.g. Focus on ramp and card draw, lower the average mana curve, strengthen sacrifice synergies, recommend more instant-speed removal...",
            key=f"ol_guidance_{deck_id}",
            height=80
        )

        with st.expander("⚙️ Advanced Batching & Context Window Tuning", expanded=False):
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                batch_size = st.select_slider(
                    "Candidate Batch Size",
                    options=[10, 15, 20, 25],
                    value=15,
                    key=f"b_size_{deck_id}",
                    help="Smaller batches keep token counts low so the model never exceeds its context window."
                )
            with col_b2:
                max_cand = st.slider(
                    "Max Binder Candidates to Evaluate",
                    min_value=15,
                    max_value=150,
                    value=min(60, len(candidate_cards)) if candidate_cards else 60,
                    step=15,
                    key=f"max_cand_{deck_id}",
                    help="Caps total candidates to keep multi-batch runtime fast on local LLMs."
                )

        eval_candidates = candidate_cards[:max_cand] if candidate_cards else []
        batches = [eval_candidates[i:i + batch_size] for i in range(0, len(eval_candidates), batch_size)] if eval_candidates else []

        # 4. Action Button
        opt_key = f"ollama_opt_result_{deck_id}"

        col_btn1, col_btn2 = st.columns([2, 1])
        with col_btn1:
            run_opt = st.button("🚀 Analyze & Optimize Deck with Ollama", type="primary", use_container_width=True, key=f"btn_run_opt_{deck_id}")
        with col_btn2:
            if opt_key in st.session_state and st.session_state[opt_key]:
                if st.button("🗑️ Clear Analysis", key=f"btn_clear_opt_{deck_id}", use_container_width=True):
                    st.session_state[opt_key] = None
                    st.rerun()

        if run_opt:
            if df_deck.empty:
                st.warning("Your deck is currently empty! Add cards to your deck first.")
            elif not eval_candidates:
                st.warning("No compatible cards found in your binder to evaluate.")
            else:
                progress_bar = st.progress(0.0)
                status_box = st.status(f"🚀 Running Multi-Batch Optimization ({len(batches)} Batches)...", expanded=True)
                t_start = time.time()

                concise_deck = format_deck_concise_for_llm(df_deck)
                ci_letters = ", ".join(sorted(combined_ci)) if combined_ci else "Colorless"
                cmdr_str = f"{current_commander}" + (f" & {current_partner}" if current_partner else "")

                # Temporary array to store proposed changes from each batch
                proposed_swaps_pool = []
                batch_logs = []

                try:
                    for b_idx, batch in enumerate(batches):
                        b_num = b_idx + 1
                        progress_val = float(b_idx) / float(len(batches) + 1)
                        progress_bar.progress(progress_val)
                        status_box.write(f"🔍 **Batch {b_num}/{len(batches)}**: Evaluating {len(batch)} binder candidates against current deck...")

                        batch_cand_str = "\n".join([format_candidate_card_concise(r) for r in batch])

                        batch_prompt = f"""You are an MTG Commander deck optimization assistant.
You are evaluating BATCH {b_num} of {len(batches)} candidate cards from the user's binder against their current deck.

=== COMMANDER & DECK CONTEXT ===
Commander: {cmdr_str}
Color Identity: {ci_letters}
Total Deck Cards: {total_cards} | Average CMC: {avg_cmc:.2f}

=== CURRENT DECKLIST ===
{concise_deck}

=== CANDIDATE BINDER CARDS IN BATCH {b_num} ({len(batch)} cards) ===
{batch_cand_str}

=== USER GUIDANCE ===
{user_guidance.strip() if user_guidance.strip() else "(General optimization: mana curve efficiency, card draw, interaction, commander synergy.)"}

=== TASK FOR THIS BATCH ===
Review ONLY the {len(batch)} candidate cards in this batch.
Identify up to 3 high-impact swaps where a candidate from THIS batch clearly upgrades a card in the current deck.
If none of the cards in this batch are worth swapping, reply ONLY with: NO SWAPS

FORMAT FOR PROPOSED SWAPS (one per line):
SWAP: Cut [Card Name in Deck] -> Add [Candidate Card Name] | Reason: [Concise 1-sentence justification]
"""
                        batch_resp = call_ollama(batch_prompt, model=chosen_model, base_url=ollama_url)

                        batch_logs.append({
                            "batch": b_num,
                            "count": len(batch),
                            "response": batch_resp
                        })

                        # Save proposed changes of this batch into the temporary array
                        if "no swaps" not in batch_resp.lower() and len(batch_resp.strip()) > 5:
                            status_box.write(f"  ✨ Found potential upgrades in Batch {b_num}!")
                            proposed_swaps_pool.append(f"### Batch {b_num} Proposals ({len(batch)} candidates evaluated):\n{batch_resp.strip()}")
                        else:
                            status_box.write(f"  ⏩ No upgrades warranted in Batch {b_num}.")

                    # Confirmation and Synthesis Pass
                    progress_bar.progress(float(len(batches)) / float(len(batches) + 1))
                    status_box.write("🧠 **Final Confirmation Pass**: Sending all proposed changes from the approved array to confirm and synthesize the final cohesive upgrade plan...")

                    if proposed_swaps_pool:
                        pool_text = "\n\n".join(proposed_swaps_pool)
                        confirm_prompt = f"""You are a master MTG Commander tournament deck builder and strategist.

You previously evaluated candidate cards from the user's physical collection across {len(batches)} batches. Below is the complete pool of all candidate swaps proposed across those batches:

=== POOL OF PROPOSED SWAPS GATHERED FROM BATCHES ===
{pool_text}

=== DECK METADATA ===
Commander: {cmdr_str}
Color Identity: {ci_letters}
Total Cards: {total_cards} | Avg CMC: {avg_cmc:.2f}

=== USER STRATEGIC GUIDANCE ===
{user_guidance.strip() if user_guidance.strip() else "(Focus on cohesive deck synergy, curve balance, and card advantage.)"}

=== FINAL TASK ===
Review the entire pool of proposed swaps above as a complete deck.
Select and confirm the BEST 3 to 8 non-conflicting, cohesive swaps that together create the strongest overall upgrade.

CRITICAL RULES:
1. ONLY confirm additions that were proposed in the pool above.
2. Do NOT cut the same card more than once.
3. Ensure the mana curve and land count remain functional.

Please format your response in clean Markdown:
1. 📊 **Executive Assessment & Mana Curve Diagnosis**
2. 🔄 **Final Confirmed Swaps**:
   Format each confirmed swap clearly:
   - ❌ **Cut:** [Card Name] (CMC, Type) — *Why to cut*
   - ➕ **Add:** [Card Name] (CMC, Type) — *Tactical justification & synergy*
3. 🎯 **Deck Impact & Playstyle Evolution**
"""
                        final_resp = call_ollama(confirm_prompt, model=chosen_model, base_url=ollama_url)
                    else:
                        final_resp = "### 📋 Deck Assessment\nThe AI evaluated all compatible cards in your binder across all batches and determined that your current deck is already well-optimized relative to your available collection! No high-impact swaps were recommended."

                    t_elapsed = time.time() - t_start
                    progress_bar.progress(1.0)
                    status_box.update(label=f"✅ Optimization complete in {t_elapsed:.1f}s ({len(batches)} batches evaluated)!", state="complete", expanded=False)

                    st.session_state[opt_key] = {
                        "model": chosen_model,
                        "response": final_resp,
                        "batch_count": len(batches),
                        "candidates_evaluated": len(eval_candidates),
                        "raw_batch_proposals": proposed_swaps_pool,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "duration": t_elapsed
                    }
                    st.rerun()

                except Exception as e:
                    status_box.update(label="❌ Optimization encountered an error", state="error")
                    st.error(f"Error calling local Ollama LLM: {e}")

        # 5. Display Stored Results
        if opt_key in st.session_state and st.session_state[opt_key]:
            res = st.session_state[opt_key]
            st.divider()
            st.markdown(f"### 📋 Final Optimization Report (`{res['model']}`) — *{res['candidates_evaluated']} binder cards evaluated in {res['batch_count']} batches ({res['duration']:.1f}s)*")

            with st.container(border=True):
                st.markdown(res["response"])

            # Quick-Apply Confirmed Swaps
            deck_set = set(df_deck['card_name'].tolist())
            binder_set = set(df_all_avail[df_all_avail['quantity_available'] > 0]['card_name'].tolist())
            extracted = extract_swaps_from_text(res["response"], deck_set, binder_set)

            if extracted:
                st.markdown("#### ⚡ Quick-Apply Confirmed Swaps")
                st.caption("Apply recommended swaps directly to your deck with one click:")
                for cut_c, add_c in extracted:
                    col_sw1, col_sw2 = st.columns([3, 1])
                    with col_sw1:
                        st.markdown(f"• **Cut** `{cut_c}` ➔ **Add** `{add_c}`")
                    with col_sw2:
                        if st.button(f"⚡ Apply Swap", key=f"btn_swap_{deck_id}_{cut_c}_{add_c}", use_container_width=True):
                            remove_card_from_deck(deck_id, cut_c, remove_all=False)
                            add_card_to_deck(deck_id, add_c, 1)
                            st.toast(f"Swapped {cut_c} for {add_c}!", icon="⚡")
                            st.rerun()

            # Inspect intermediate proposals from the temporary array
            if res.get("raw_batch_proposals"):
                with st.expander(f"📦 Inspect Intermediate Batch Proposals Pool ({len(res['raw_batch_proposals'])} batches with proposals)"):
                    st.caption("Below is the temporary array of candidate proposals gathered across all batches before the final confirmation pass:")
                    for prop in res["raw_batch_proposals"]:
                        st.markdown(prop)

    # -------------------------
    # TAB 6: EXPORT DECKLIST
    # -------------------------
    with tab_export:
        st.subheader("📤 Export Decklist")
        export_mode = st.radio("Export Format", ["Categorized MTG Format", "MTG Arena / MTGO Flat Format"], horizontal=True, key=f"exp_mode_{deck_id}")

        if export_mode == "Categorized MTG Format":
            lines = []
            if current_commander:
                lines.append("// Commander")
                lines.append(f"1 {current_commander}")
                if current_partner:
                    lines.append(f"1 {current_partner}")
                lines.append("")
            lines.extend([f"// Deck: {deck_name}", f"// Total Cards: {total_cards}\n"])
            for cat_name, _ in CATEGORY_ORDER:
                cat_group = df_deck[df_deck['category'] == cat_name]
                filtered_group = cat_group[~cat_group['card_name'].isin([current_commander, current_partner])]
                if not filtered_group.empty:
                    lines.append(f"// {cat_name} ({int(filtered_group['quantity'].sum())})")
                    for _, r in filtered_group.iterrows():
                        lines.append(f"{r['quantity']} {r['card_name']}")
                    lines.append("")
            export_text = "\n".join(lines).strip()
        else:
            lines = []
            if current_commander:
                lines.append(f"1 {current_commander}")
            if current_partner:
                lines.append(f"1 {current_partner}")
            for _, r in df_deck.iterrows():
                if r['card_name'] in [current_commander, current_partner]:
                    continue
                lines.append(f"{r['quantity']} {r['card_name']}")
            export_text = "\n".join(lines).strip()

        st.text_area("Decklist Text", export_text, height=260, key=f"exp_text_{deck_id}")
        st.download_button(
            label="💾 Download Decklist (.txt)",
            data=export_text,
            file_name=f"{deck_name.replace(' ', '_')}.txt",
            mime="text/plain",
            key=f"download_{deck_id}"
        )

    # ---------------------------------------------------------
    # DECK OPTIONS (RENAME, DUPLICATE, DELETE)
    # ---------------------------------------------------------
    with st.expander("⚙️ Deck Options"):
        col_opt1, col_opt2 = st.columns(2)
        with col_opt1:
            st.markdown("**Rename Deck**")
            ren_name = st.text_input("New Deck Name", value=deck_name, key=f"ren_name_{deck_id}")
            if st.button("Save New Name", key=f"btn_ren_{deck_id}"):
                if ren_name.strip() and ren_name.strip() != deck_name:
                    execute_db("UPDATE decks SET name = ? WHERE id = ?", (ren_name.strip(), deck_id))
                    st.session_state.selected_deck_display = ren_name.strip()
                    st.success(f"Renamed deck to '{ren_name.strip()}'!")
                    st.rerun()

        with col_opt2:
            st.markdown("**Duplicate Deck**")
            dup_name = st.text_input("Duplicate Name", value=f"{deck_name} (Copy)", key=f"dup_name_{deck_id}")
            if st.button("Create Copy", key=f"btn_dup_{deck_id}"):
                if dup_name.strip():
                    execute_db("INSERT INTO decks (name, commander, partner_commander) VALUES (?, ?, ?)",
                               (dup_name.strip(), current_commander, current_partner))
                    conn = sqlite3.connect(DB_NAME)
                    new_d_id = pd.read_sql_query("SELECT id FROM decks WHERE name = ?", conn, params=(dup_name.strip(),)).iloc[0]['id']
                    conn.close()
                    for _, r in df_deck.iterrows():
                        execute_db("INSERT INTO deck_cards (deck_id, card_name, quantity) VALUES (?, ?, ?)",
                                   (int(new_d_id), r['card_name'], int(r['quantity'])))
                    st.session_state.selected_deck_display = dup_name.strip()
                    st.success(f"Duplicated deck as '{dup_name.strip()}'!")
                    st.rerun()

        st.divider()
        if st.button("🗑️ Delete this deck", key=f"del_entire_deck_{deck_id}"):
            execute_db("DELETE FROM deck_cards WHERE deck_id = ?", (deck_id,))
            execute_db("DELETE FROM decks WHERE id = ?", (deck_id,))
            st.success(f"Deck '{deck_name}' deleted — all cards returned to collection!")
            st.rerun()


# ==========================================
# 5. STREAMLIT USER INTERFACE
# ==========================================
st.set_page_config(page_title="MTG Inventory Manager", layout="wide")
init_db()

# Custom CSS for polished aesthetics
st.markdown("""
<style>
    img {
        border-radius: 8px;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    img:hover {
        transform: scale(1.03);
        box-shadow: 0 8px 16px rgba(0,0,0,0.3);
    }
    [data-testid="stMetricValue"] {
        font-size: 1.5rem !important;
        font-weight: 700;
    }
</style>
""", unsafe_allow_html=True)

st.title("🧙‍♂️ MTG Collection & Deck Manager")

# Sidebar Navigation
NAV_PAGES = ["📚 My Binder (Collection)", "🃏 Deck Builder", "🎴 Deck Display", "📥 Import Decklist"]

if "nav_radio" not in st.session_state:
    st.session_state.nav_radio = NAV_PAGES[0]

page = st.sidebar.radio("Navigation", NAV_PAGES, key="nav_radio")

# ------------------------------------------
# PAGE 1: MY BINDER (INVENTORY + MASS ADD + EDIT/REMOVE)
# ------------------------------------------
if page == "📚 My Binder (Collection)":
    st.header("Your Physical Collection")

    tab1, tab2, tab3 = st.tabs(["➕ Add Single Card", "📦 Mass Add Cards", "✏️ Edit / Remove Cards"])

    # --- TAB 1: SINGLE CARD ---
    with tab1:
        with st.form("add_single_card_form"):
            col1, col2, col3 = st.columns([3, 1, 1])
            with col1:
                new_card = st.text_input("Card Name")
            with col2:
                qty = st.number_input("Quantity", min_value=1, value=1)
            with col3:
                submit = st.form_submit_button("Add to Binder")

            if submit and new_card:
                added_name = add_card_to_collection(new_card, qty)
                if added_name:
                    st.success(f"Added {qty}x **{added_name}** to your binder!")
                    st.rerun()
                else:
                    st.error("Card not found on Scryfall. Check spelling!")

    # --- TAB 2: MASS ADD ---
    with tab2:
        st.markdown("Paste a list of cards to add to your collection at once.")
        st.caption("Supported formats: `4 Sol Ring`, `1x Counterspell`, or just `Brainstorm` (defaults to 1).")

        mass_text = st.text_area("Paste Card List", height=200,
                                 placeholder="4 Lightning Bolt\n2x Counterspell\n1 Sol Ring\nSwamp")

        if st.button("Add All Cards to Binder") and mass_text:
            lines = mass_text.strip().split("\n")
            parsed_items = [parse_card_line(line) for line in lines if line.strip()]

            if parsed_items:
                progress_bar = st.progress(0)
                status_text = st.empty()

                successful = []
                failed = []

                total = len(parsed_items)
                for idx, (qty, card_name) in enumerate(parsed_items):
                    status_text.text(f"Processing ({idx + 1}/{total}): {card_name}...")
                    added_name = add_card_to_collection(card_name, qty)

                    if added_name:
                        successful.append({"Card": added_name, "Qty Added": qty})
                    else:
                        failed.append({"Card": card_name, "Qty": qty})

                    progress_bar.progress((idx + 1) / total)
                    time.sleep(0.08)

                status_text.text("Import complete!")

                if successful:
                    st.success(f"Successfully added {len(successful)} unique card titles to your binder!")
                    st.dataframe(pd.DataFrame(successful), use_container_width=True)
                if failed:
                    st.error(f"Failed to find {len(failed)} cards on Scryfall:")
                    st.table(pd.DataFrame(failed))

                st.rerun()

    # --- TAB 3: EDIT / REMOVE CARDS (INTERACTIVE GRID WITH - [QTY] +) ---
    with tab3:
        st.subheader("Manage Collection Quantities (Interactive Grid)")
        st.caption("Use the **➖ [qty] ➕** buttons on each card to increment or decrement physical copies owned.")

        all_col_cards = run_query("""
            SELECT card_name, total_owned, total_assigned, quantity_available, 
                   faces_json, image_url, mana_cost, type_line, oracle_text, keywords, produced_mana 
            FROM v_available_cards 
            ORDER BY card_name ASC
        """)
        if all_col_cards.empty:
            st.info("No cards in your binder yet. Add cards in **➕ Add Single Card** or **📦 Mass Add Cards**!")
        else:
            # Filter bar for the grid
            col_gf1, col_gf2, col_gf3 = st.columns([2, 1, 1])
            with col_gf1:
                grid_search = st.text_input("🔍 Search Card (Name, Text)", placeholder="e.g. Sol Ring, Dragon, Draw a card...", key="edit_grid_search")
            with col_gf2:
                grid_cat = st.selectbox("Category", ["All Types", "Creatures", "Planeswalkers", "Instants", "Sorceries", "Artifacts", "Enchantments", "Lands"], key="edit_grid_cat")
            with col_gf3:
                grid_cols_num = st.selectbox("Cards per Row", [3, 4, 5, 6], index=1, key="edit_grid_cols")

            filtered_grid = all_col_cards.copy()
            if grid_search:
                s_l = grid_search.lower().strip()
                filtered_grid = filtered_grid[
                    filtered_grid['card_name'].str.lower().str.contains(s_l) |
                    filtered_grid['oracle_text'].fillna('').str.lower().str.contains(s_l) |
                    filtered_grid['keywords'].fillna('').str.lower().str.contains(s_l)
                ]
            if grid_cat != "All Types":
                filtered_grid['cat'] = filtered_grid['type_line'].apply(get_card_category)
                filtered_grid = filtered_grid[filtered_grid['cat'] == grid_cat]

            if filtered_grid.empty:
                st.warning("No cards found matching your search filter.")
            else:
                st.caption(f"Showing **{len(filtered_grid)}** cards in collection:")
                grid_rows = [filtered_grid.iloc[i:i + grid_cols_num] for i in range(0, len(filtered_grid), grid_cols_num)]
                for r_df in grid_rows:
                    cols = st.columns(grid_cols_num)
                    for idx, (_, row_c) in enumerate(r_df.iterrows()):
                        with cols[idx]:
                            c_name = row_c['card_name']
                            cur_owned = int(row_c['total_owned'])
                            cur_assigned = int(row_c['total_assigned'])
                            cur_avail = int(row_c['quantity_available'])

                            f_idx, bdisp, b_has_faces, b_faces = get_card_display_details(c_name, row_c, scope="binder_grid")

                            try:
                                st.image(bdisp["image_url"], use_container_width=True)
                            except Exception:
                                st.caption("Image not available")

                            st.markdown(f"**{bdisp['name']}**")
                            cost_l = bdisp['mana_cost'] if bdisp['mana_cost'] else ""
                            st.caption(f"{cost_l} • {bdisp['type_line']}")

                            if cur_avail > 0:
                                st.caption(f"🟢 **{cur_avail}** free / **{cur_assigned}** in decks")
                            else:
                                st.caption(f"🔴 **0** free / **{cur_assigned}** in decks")

                            # Stepper: [-]  {cur_owned}  [+]
                            c_dec, c_num, c_inc = st.columns([1, 1, 1])
                            with c_dec:
                                if st.button("➖", key=f"b_dec_{c_name}", help="Remove 1 copy"):
                                    if cur_owned > 1:
                                        new_q = cur_owned - 1
                                        execute_db("UPDATE collection SET total_owned = ? WHERE card_name = ?", (new_q, c_name))
                                        if new_q < cur_assigned:
                                            st.toast(f"⚠️ {c_name} owned ({new_q}) is now less than assigned across decks ({cur_assigned})!", icon="⚠️")
                                        else:
                                            st.toast(f"Updated {c_name} to {new_q} copies!", icon="➖")
                                    else:
                                        execute_db("DELETE FROM deck_cards WHERE card_name = ?", (c_name,))
                                        execute_db("DELETE FROM collection WHERE card_name = ?", (c_name,))
                                        st.toast(f"Removed {c_name} from binder!", icon="🗑️")
                                    st.rerun()

                            with c_num:
                                st.markdown(f"<div style='text-align:center; font-size:1.25rem; font-weight:bold; padding-top:4px;'>{cur_owned}</div>", unsafe_allow_html=True)

                            with c_inc:
                                if st.button("➕", key=f"b_inc_{c_name}", help="Add 1 copy"):
                                    new_q = cur_owned + 1
                                    execute_db("UPDATE collection SET total_owned = ? WHERE card_name = ?", (new_q, c_name))
                                    st.toast(f"Updated {c_name} to {new_q} copies!", icon="➕")
                                    st.rerun()

                            # Flip button & Details / Delete Popover
                            col_sub1, col_sub2 = st.columns([1, 1])
                            with col_sub1:
                                if b_has_faces:
                                    other_face_n = b_faces[1 - f_idx]["name"]
                                    if st.button("🔄", key=f"b_flip_{c_name}", help=f"Flip: {other_face_n}"):
                                        toggle_card_face(c_name, scope="binder_grid")
                                        st.rerun()
                                else:
                                    st.write("")
                            with col_sub2:
                                with st.popover("🗑️", help="Delete card completely"):
                                    st.markdown(f"Delete **{c_name}** from collection?")
                                    if cur_assigned > 0:
                                        st.warning(f"⚠️ {cur_assigned} copy/copies are in decks! Deleting removes them too.")
                                    if st.button("Confirm Delete", key=f"del_pop_{c_name}", type="primary"):
                                        execute_db("DELETE FROM deck_cards WHERE card_name = ?", (c_name,))
                                        execute_db("DELETE FROM collection WHERE card_name = ?", (c_name,))
                                        st.toast(f"Deleted {c_name}!", icon="🗑️")
                                        st.rerun()
                            st.divider()

    # Display Current Inventory
    st.divider()
    st.subheader("Current Inventory Status")

    # Check for cards missing metadata and offer sync
    missing_meta = run_query("""
        SELECT COUNT(*) as count FROM collection 
        WHERE oracle_text IS NULL OR oracle_text = '' OR keywords IS NULL
    """)
    missing_count = int(missing_meta['count'].iloc[0]) if not missing_meta.empty else 0
    if missing_count > 0:
        c_sync1, c_sync2 = st.columns([3, 1])
        with c_sync1:
            st.info(f"ℹ️ **{missing_count} cards** in your database are missing card text or keywords.")
        with c_sync2:
            if st.button("🔄 Sync Card Text & Keywords", help="Fetches card text, keywords, and face data for cards missing them"):
                cards_to_sync = run_query("SELECT card_name FROM collection WHERE oracle_text IS NULL OR oracle_text = '' OR keywords IS NULL")['card_name'].tolist()
                p_sync = st.progress(0)
                status_sync = st.empty()
                for idx, c_name in enumerate(cards_to_sync):
                    status_sync.text(f"Syncing ({idx + 1}/{len(cards_to_sync)}): {c_name}...")
                    fetch_scryfall_data(c_name)
                    p_sync.progress((idx + 1) / len(cards_to_sync))
                    time.sleep(0.08)
                status_sync.text("Sync complete!")
                st.success("Successfully updated card text and keywords in the database!")
                st.rerun()

    # Search and Filter Binder
    col_bs, col_bkw = st.columns([2, 2])
    with col_bs:
        binder_search = st.text_input("🔍 Search Binder (Card Name, Oracle Text)", placeholder="e.g. draw a card, counter target...", key="binder_search_input")
    with col_bkw:
        binder_kw_filter = st.text_input("🏷️ Filter by Keyword", placeholder="e.g. Flying, Trample, Ward...", key="binder_kw_input")

    query_binder = """
        SELECT 
            card_name as 'Card', 
            mana_cost as 'Mana Cost',
            type_line as 'Type', 
            keywords as 'Keywords',
            total_owned as 'Total Owned', 
            total_assigned as 'In Decks', 
            quantity_available as 'Available in Binder',
            oracle_text
        FROM v_available_cards
        WHERE 1=1
    """
    params_binder = []
    if binder_search:
        query_binder += " AND (card_name LIKE ? OR oracle_text LIKE ?)"
        params_binder.extend([f"%{binder_search}%", f"%{binder_search}%"])
    if binder_kw_filter:
        query_binder += " AND keywords LIKE ?"
        params_binder.append(f"%{binder_kw_filter}%")
    query_binder += " ORDER BY card_name ASC"

    df_inventory = run_query(query_binder, params_binder)
    if not df_inventory.empty:
        disp_df = df_inventory.drop(columns=['oracle_text'], errors='ignore')
        st.dataframe(disp_df, use_container_width=True)
        st.caption(f"Showing **{len(disp_df)}** unique card titles in your collection.")
    else:
        st.info("Your binder is currently empty or no cards match your filters.")

# ------------------------------------------
# PAGE 2: DECK BUILDER
# ------------------------------------------
elif page == "🃏 Deck Builder":
    st.header("Deck Builder")

    decks = run_query("SELECT id, name FROM decks")
    deck_names = decks['name'].tolist() if not decks.empty else []

    col1, col2 = st.columns(2)
    with col1:
        selected_deck = st.selectbox("Select a Deck", ["-- Create New Deck --"] + deck_names)

    if selected_deck == "-- Create New Deck --":
        with col2:
            new_deck_name = st.text_input("New Deck Name")
            new_cmdr_name = st.text_input("Commander Name (Optional)", placeholder="e.g. Atraxa, Praetors' Voice")
            if st.button("Create Deck") and new_deck_name:
                execute_db("INSERT INTO decks (name, commander) VALUES (?, ?)", 
                           (new_deck_name.strip(), new_cmdr_name.strip() if new_cmdr_name else None))
                if new_cmdr_name and new_cmdr_name.strip():
                    c_data = fetch_scryfall_data(new_cmdr_name.strip())
                    if c_data:
                        conn = sqlite3.connect(DB_NAME)
                        new_id = pd.read_sql_query("SELECT id FROM decks WHERE name = ?", conn, params=(new_deck_name.strip(),)).iloc[0]['id']
                        conn.close()
                        add_card_to_deck(int(new_id), c_data['name'], 1)
                        add_card_to_collection(c_data['name'], 1)
                st.success(f"Created Commander deck: {new_deck_name.strip()}")
                st.session_state.selected_deck_display = new_deck_name.strip()
                st.rerun()
    else:
        deck_id = int(decks[decks['name'] == selected_deck]['id'].iloc[0])
        render_deck_display(deck_id, selected_deck)

# ------------------------------------------
# PAGE 3: DECK DISPLAY
# ------------------------------------------
elif page == "🎴 Deck Display":
    st.header("🎴 Deck Display & Analytics")

    decks = run_query("SELECT id, name FROM decks")
    deck_names = decks['name'].tolist() if not decks.empty else []

    if not deck_names:
        st.info("No decks found yet! Create a deck in the **🃏 Deck Builder** or import one from **📥 Import Decklist**.")
    else:
        selected_idx = 0
        if "selected_deck_display" in st.session_state and st.session_state.selected_deck_display in deck_names:
            selected_idx = deck_names.index(st.session_state.selected_deck_display)

        col_deck_sel, col_space = st.columns([3, 1])
        with col_deck_sel:
            chosen_deck = st.selectbox("Select Deck to Display", deck_names, index=selected_idx, key="deck_display_select")
            st.session_state.selected_deck_display = chosen_deck

        deck_id = int(decks[decks['name'] == chosen_deck]['id'].iloc[0])
        render_deck_display(deck_id, chosen_deck)

# ------------------------------------------
# PAGE 4: IMPORT DECKLIST
# ------------------------------------------
elif page == "📥 Import Decklist":
    st.header("Import Decklist & Audit Collection")
    st.markdown("Paste a decklist to assign your owned cards and highlight missing cards.")

    # Show persistent audit results from session state to avoid nested button issues
    if "import_audit_result" in st.session_state and st.session_state.import_audit_result:
        res = st.session_state.import_audit_result
        d_name = res["deck_name"]
        st.success(f"Deck **{d_name}** has been processed!")

        c_nav1, c_nav2 = st.columns([1, 3])
        with c_nav1:
            if st.button(f"👁️ View '{d_name}' in Deck Display", key="btn_view_imported_nav"):
                st.session_state.selected_deck_display = d_name
                st.session_state.nav_radio = "🎴 Deck Display"
                st.session_state.nav_page = "🎴 Deck Display"
                st.rerun()
        with c_nav2:
            if st.button("🔄 Clear & Import Another Deck", key="btn_clear_imported_nav"):
                st.session_state.import_audit_result = None
                st.rerun()

        col1, col2 = st.columns(2)
        with col1:
            st.subheader("✅ Assigned from Binder")
            if res["assigned"]:
                st.table(pd.DataFrame(res["assigned"]))
            else:
                st.info("No cards matched your binder.")
        with col2:
            st.subheader("❌ Missing from Collection")
            if res["missing"]:
                st.table(pd.DataFrame(res["missing"]))
            else:
                st.success("You own all cards in this list!")
        st.divider()

    deck_name = st.text_input("Name for imported deck", key="inp_import_deck_name")
    decklist_text = st.text_area("Paste Decklist", height=250, placeholder="1 Sol Ring\n1 Counterspell\n1 Mana Crypt", key="inp_import_decklist_txt")

    if st.button("Import & Audit", key="btn_do_import") and deck_name and decklist_text:
        lines = decklist_text.strip().split("\n")
        parsed_cards = [parse_card_line(line) for line in lines if line.strip()]

        if not parsed_cards:
            st.error("Could not parse the decklist.")
        else:
            execute_db("INSERT OR IGNORE INTO decks (name) VALUES (?)", (deck_name.strip(),))
            conn = sqlite3.connect(DB_NAME)
            deck_id = pd.read_sql_query("SELECT id FROM decks WHERE name = ?", conn, params=(deck_name.strip(),)).iloc[0]['id']
            conn.close()

            assigned, missing = [], []

            for qty_needed, card in parsed_cards:
                # Find canonical card name from binder or Scryfall cache
                df_avail = run_query("SELECT card_name, quantity_available FROM v_available_cards WHERE card_name LIKE ?", (card,))
                if not df_avail.empty:
                    canonical_name = df_avail.iloc[0]['card_name']
                    available = int(df_avail.iloc[0]['quantity_available'])
                else:
                    s_card = fetch_scryfall_data(card)
                    canonical_name = s_card['name'] if s_card else card
                    available = 0

                if available >= qty_needed:
                    execute_db("INSERT INTO deck_cards (deck_id, card_name, quantity) VALUES (?, ?, ?)",
                               (int(deck_id), canonical_name, qty_needed))
                    assigned.append({'Card': canonical_name, 'Qty Added': qty_needed})
                elif available > 0:
                    execute_db("INSERT INTO deck_cards (deck_id, card_name, quantity) VALUES (?, ?, ?)",
                               (int(deck_id), canonical_name, available))
                    assigned.append({'Card': canonical_name, 'Qty Added': available})
                    missing.append({'Card': canonical_name, 'Shortage': qty_needed - available})
                else:
                    missing.append({'Card': canonical_name, 'Shortage': qty_needed})

            st.session_state.import_audit_result = {
                "deck_name": deck_name.strip(),
                "assigned": assigned,
                "missing": missing
            }
            st.rerun()