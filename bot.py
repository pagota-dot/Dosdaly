import os
import re
import json
import hashlib
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

STOCK_URL = "https://www.gag2.gg/stock"
SELL_URL = "https://www.gag2.gg/stock/sell"
STATE_PATH = Path("state.json")

WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "").strip()

RARITIES = r"(?:COMMON|UNCOMMON|RARE|EPIC|LEGENDARY|MYTHIC|SUPER)"
RARITY_WORDS = ("common", "uncommon", "rare", "epic", "legendary", "mythic", "super")
WEBHOOK_RE = re.compile(r"^https://discord\.com/api/webhooks/\d+/[A-Za-z0-9._-]+$")

SELL_MULTIPLIERS = {2.0, 4.0}
MAX_READ_ATTEMPTS = 3
SOURCE_SYNC_WAIT_SECONDS = 6
BOUNDARY_EXTRA_SECONDS = 8
BOUNDARY_TIMER_THRESHOLD = 20
MIN_STOCK_ITEMS = 3
MIN_SELL_ITEMS = 1
HEALTH_ALERT_COOLDOWN_HOURS = 1

# Exact GAG2 targets
EXACT_STOCK_TARGETS = {
    "atlantic giant pumpkin": {
        "label": "Atlantic Giant Pumpkin",
        "emoji": "🎃",
    },
    "super syrup watering can": {
        "label": "Super Syrup Watering Can",
        "emoji": "🪣",
    },
    "super syrup sprinkler": {
        "label": "Super Syrup Sprinkler",
        "emoji": "💦",
    },
    "amber cranberry": {
        "label": "Amber Cranberry",
        "emoji": "🟠",
    },
}

SELL_TARGETS = {
    "maple mushroom": {
        "label": "Maple Mushroom",
        "emoji": "🍄",
    },
    "atlantic giant pumpkin": {
        "label": "Atlantic Giant Pumpkin",
        "emoji": "🎃",
    },
}


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def norm(s):
    return re.sub(r"\s+", " ", str(s or "").replace("\xa0", " ")).strip()


def key(s):
    s = norm(s).lower().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def stable_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def valid_name(s):
    s = norm(s)
    if not (2 <= len(s) <= 90):
        return False
    if re.fullmatch(RARITIES, s, re.I):
        return False
    return not re.fullmatch(
        r"(Seed Shop|Gear Shop|Crates?|Props?|items in stock|Sell|Multiplier)",
        s,
        re.I,
    )


def clean_stock_name(s):
    return re.sub(
        r"^(?:Seed Shop|Gear Shop|Crates?|Props?)\s+\d+\s+items?\s+in\s+stock\s+\d+:\d+\s*",
        "",
        norm(s),
        flags=re.I,
    ).strip()


def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,1200")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147 Safari/537.36"
    )
    return webdriver.Chrome(options=opts)


def rendered_text(driver, url, hints):
    driver.get(url)

    def ready(d):
        try:
            text = d.find_element("tag name", "body").text
        except Exception:
            return False
        low = text.lower()
        return len(text) > 100 and any(h.lower() in low for h in hints)

    try:
        WebDriverWait(driver, 20, poll_frequency=1).until(ready)
    except Exception:
        pass

    # Let client-rendered live cards settle.
    time.sleep(3)
    return driver.find_element("tag name", "body").text



def extract_countdowns(text):
    """Return plausible mm:ss countdowns from rendered page text."""
    values = []
    for m in re.finditer(r"(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)", text or ""):
        mm = int(m.group(1))
        ss = int(m.group(2))
        total = mm * 60 + ss
        if 0 <= total <= 60 * 60:
            values.append(total)
    return values


def stock_snapshot(driver):
    text = driver.find_element("tag name", "body").text
    stock = parse_stock(text)
    timers = extract_countdowns(text)
    return {
        "stock": stock,
        "text": text,
        "fingerprint": stable_hash(stock),
        "min_timer_seconds": min(timers) if timers else None,
    }


def read_source_synced_stock(driver):
    """
    Read GAG2 stock more than once so a client-side delay after restock
    is less likely to make us accept the previous cycle.

    Strategy:
      1) Load and settle.
      2) Snapshot A.
      3) If a timer is near 0, wait past boundary + grace period.
         Otherwise wait a short confirmation interval.
      4) Refresh and snapshot B.
      5) If A != B, wait/refresh once more and use C.
    """
    rendered_text(
        driver,
        STOCK_URL,
        ["Seed Shop", "Gear Shop", "Crate", "stock"],
    )

    a = stock_snapshot(driver)

    near_boundary = (
        a["min_timer_seconds"] is not None
        and a["min_timer_seconds"] <= BOUNDARY_TIMER_THRESHOLD
    )

    if near_boundary:
        wait_s = min(
            BOUNDARY_TIMER_THRESHOLD + BOUNDARY_EXTRA_SECONDS,
            max(BOUNDARY_EXTRA_SECONDS, a["min_timer_seconds"] + BOUNDARY_EXTRA_SECONDS),
        )
    else:
        wait_s = SOURCE_SYNC_WAIT_SECONDS

    time.sleep(wait_s)
    driver.refresh()
    time.sleep(3)
    b = stock_snapshot(driver)

    changed = a["fingerprint"] != b["fingerprint"]

    if changed:
        # The page changed while we were sampling. Confirm the new cycle once more.
        time.sleep(SOURCE_SYNC_WAIT_SECONDS)
        driver.refresh()
        time.sleep(3)
        c = stock_snapshot(driver)
        chosen = c
        samples = [a, b, c]
    else:
        chosen = b
        samples = [a, b]

    return {
        "stock": chosen["stock"],
        "samples": len(samples),
        "changed_during_sync": changed,
        "min_timer_seconds": chosen["min_timer_seconds"],
        "sample_counts": [len(x["stock"]) for x in samples],
        "sample_fingerprints": [x["fingerprint"] for x in samples],
    }


def _sell_contexts_for_target(driver, target_name):
    """
    Pull nearby visible text around an exact sell target.
    We do not need the full sell table; only our two watched fruits.
    """
    script = r"""
    const target = arguments[0].trim().toLowerCase();
    const out = [];
    const all = Array.from(document.querySelectorAll('body *'));

    for (const el of all) {
      const own = (el.innerText || el.textContent || '').trim();
      if (!own) continue;

      const low = own.toLowerCase();
      if (low !== target && !low.includes(target)) continue;

      let p = el;
      for (let depth = 0; depth < 6 && p; depth++, p = p.parentElement) {
        const t = (p.innerText || p.textContent || '').trim();
        if (t && t.length <= 1000) out.push(t);
      }
    }

    return [...new Set(out)].slice(0, 40);
    """
    try:
        return driver.execute_script(script, target_name) or []
    except Exception:
        return []


def _extract_multiplier_from_text(target_name, text):
    if not text:
        return None

    low = text.lower()
    pos = low.find(target_name.lower())
    region = text if pos < 0 else text[max(0, pos - 300): pos + len(target_name) + 500]

    patterns = [
        r"[×x]\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*[×x]",
        r"(?:multiplier|sell\s*multiplier)\s*[:=-]?\s*(\d+(?:\.\d+)?)",
    ]

    for pat in patterns:
        vals = []
        for m in re.finditer(pat, region, re.I):
            try:
                v = float(m.group(1))
            except Exception:
                continue
            if 0 < v <= 100:
                vals.append(v)
        if vals:
            # Nearby card usually has only one meaningful multiplier.
            return vals[0]

    return None


def _extract_multiplier_from_html(target_name, html):
    """
    Fallback for React/Next serialized props. Search a small window around
    the target name for multiplier-like JSON fields first.
    """
    if not html:
        return None

    low = html.lower()
    target = target_name.lower()
    starts = [m.start() for m in re.finditer(re.escape(target), low)]
    for pos in starts[:20]:
        region = html[max(0, pos - 1200): pos + len(target_name) + 1800]

        field_patterns = [
            r'["\'](?:sell_?multiplier|multiplier|multi|value)["\']\s*:\s*["\']?(\d+(?:\.\d+)?)',
            r'[×x]\s*(\d+(?:\.\d+)?)',
            r'(\d+(?:\.\d+)?)\s*[×x]',
        ]
        for pat in field_patterns:
            m = re.search(pat, region, re.I)
            if not m:
                continue
            try:
                v = float(m.group(1))
            except Exception:
                continue
            if 0 < v <= 100:
                return v

    return None


def read_sell_targets(driver):
    """
    Read only the two sell fruits we actually alert on.
    This is more robust than requiring the complete dynamically rendered table.
    """
    rendered_text(
        driver,
        SELL_URL,
        ["Sell", "Multiplier", "Fruit"],
    )

    # Trigger lazy-rendered rows/cards on mobile-like pages.
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.55)")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, 0)")
        time.sleep(1)
    except Exception:
        pass

    body_text = driver.find_element("tag name", "body").text
    html = driver.page_source

    results = []
    diagnostics = {}

    for target_key, meta in SELL_TARGETS.items():
        target_name = meta["label"]
        contexts = _sell_contexts_for_target(driver, target_name)

        multiplier = None

        # Prefer the smallest contexts first, which are more likely to be the card/row.
        for ctx in sorted(contexts, key=len):
            multiplier = _extract_multiplier_from_text(target_name, ctx)
            if multiplier is not None:
                break

        if multiplier is None:
            multiplier = _extract_multiplier_from_text(target_name, body_text)

        if multiplier is None:
            multiplier = _extract_multiplier_from_html(target_name, html)

        diagnostics[target_key] = {
            "contexts_found": len(contexts),
            "multiplier": multiplier,
        }

        if multiplier is not None:
            results.append(
                {
                    "name": target_name,
                    "multi": float(multiplier),
                }
            )

    return results, diagnostics


def parse_stock(text):
    lines = [norm(x) for x in text.splitlines() if norm(x)]
    out = []
    current_type = "unknown"

    def push(name, qty, typ, rarity=""):
        name = clean_stock_name(name)
        if not valid_name(name):
            return
        out.append(
            {
                "name": name,
                "qty": int(qty or 0),
                "type": typ,
                "rarity": norm(rarity).upper() if rarity else "",
            }
        )

    for i, line in enumerate(lines):
        if re.search(r"Seed Shop", line, re.I):
            current_type = "seed"
        elif re.search(r"Gear Shop", line, re.I):
            current_type = "gear"
        elif re.search(r"Crates?|Props?", line, re.I):
            current_type = "crate"

        # Single-line card: Name RARITY ×N
        for m in re.finditer(
            rf"([A-Z][A-Za-z0-9'’\- ]{{1,70}}?)\s+({RARITIES})\s*[×x]\s*(\d+)",
            line,
            re.I,
        ):
            push(m.group(1), m.group(3), current_type, m.group(2))

        m = re.match(rf"^(.+?)\s+({RARITIES})\s*[×x]\s*(\d+)$", line, re.I)
        if m:
            push(m.group(1), m.group(3), current_type, m.group(2))
            continue

        # Multiline: Name / Rarity / ×N
        if i + 2 < len(lines) and valid_name(clean_stock_name(line)):
            if re.fullmatch(RARITIES, lines[i + 1], re.I):
                q = re.match(r"^[×x]\s*(\d+)$", lines[i + 2], re.I)
                if q:
                    push(line, q.group(1), current_type, lines[i + 1])
                    continue

        # Multiline: Name / ×N
        if i + 1 < len(lines) and valid_name(clean_stock_name(line)):
            q = re.match(r"^[×x]\s*(\d+)$", lines[i + 1], re.I)
            if q:
                push(line, q.group(1), current_type)

    dedup = {}
    for x in out:
        k = f"{x['type']}|{key(x['name'])}|{x.get('rarity','')}"
        old = dedup.get(k)
        if old is None or x["qty"] > old["qty"]:
            dedup[k] = x

    return sorted(
        dedup.values(),
        key=lambda x: (x["type"], key(x["name"]), x.get("rarity", "")),
    )


def parse_sell(text):
    lines = [norm(x) for x in text.splitlines() if norm(x)]
    out = []

    def push(name, multi):
        name = re.sub(
            r"^(Sell|Multiplier|Sell Multiplier|Fruit)\s*",
            "",
            norm(name),
            flags=re.I,
        ).strip()

        try:
            multi = float(multi)
        except Exception:
            return

        if not valid_name(name) or not (0 < multi <= 100):
            return

        out.append({"name": name, "multi": multi})

    for i, line in enumerate(lines):
        m = re.match(r"^(.+?)\s*[×x]\s*(\d+(?:\.\d+)?)$", line, re.I)
        if m:
            push(m.group(1), m.group(2))
            continue

        m = re.match(r"^(.+?)\s+(\d+(?:\.\d+)?)\s*[×x]$", line, re.I)
        if m:
            push(m.group(1), m.group(2))
            continue

        if i + 1 < len(lines) and valid_name(line):
            q = re.match(r"^[×x]\s*(\d+(?:\.\d+)?)$", lines[i + 1], re.I)
            if q:
                push(line, q.group(1))

    dedup = {}
    for x in out:
        dedup.setdefault(f"{key(x['name'])}|{x['multi']}", x)

    return sorted(dedup.values(), key=lambda x: (key(x["name"]), x["multi"]))


def rarity_from_item(item):
    rarity = norm(item.get("rarity", "")).lower()
    if rarity:
        return rarity

    tokens = set(key(item.get("name", "")).split())
    for r in RARITY_WORDS:
        if r in tokens:
            return r
    return ""


def is_allowed_magic_mail(item):
    n = key(item.get("name"))
    if "magic mail" not in n:
        return False

    rarity = rarity_from_item(item)

    # Rare Magic Mail must never alert.
    if rarity == "rare":
        return False
    if re.search(r"\brare\b", n):
        return False

    return True


def shop_fingerprints(stock, sell):
    result = {}

    for typ in ("seed", "gear", "crate", "unknown"):
        items = [
            {
                "name": key(x["name"]),
                "qty": int(x.get("qty", 0)),
                "rarity": norm(x.get("rarity", "")).upper(),
            }
            for x in stock
            if x.get("type") == typ
        ]
        items.sort(key=lambda x: (x["name"], x["rarity"], x["qty"]))
        result[typ] = stable_hash(items)

    sell_items = [
        {
            "name": key(x["name"]),
            "multi": float(x.get("multi", 0)),
        }
        for x in sell
    ]
    sell_items.sort(key=lambda x: (x["name"], x["multi"]))
    result["sell"] = stable_hash(sell_items)

    return result


def target_snapshot(stock, sell):
    """
    Store current state for every target, including states that do NOT trigger.
    This lets us distinguish:
      absent -> present
      x1 -> x2
      x2 -> x4
      target disappears -> later returns
    """
    snapshot = {
        "stock": {},
        "magic_mail": {},
        "sell": {},
    }

    # Exact stock targets
    for target_key, meta in EXACT_STOCK_TARGETS.items():
        matches = [x for x in stock if key(x.get("name")) == target_key]

        if matches:
            # In normal layout there should be one. Keep all details if duplicates appear.
            matches = sorted(
                matches,
                key=lambda x: (
                    x.get("type", ""),
                    norm(x.get("rarity", "")),
                    int(x.get("qty", 0)),
                ),
            )
            snapshot["stock"][target_key] = {
                "present": True,
                "items": matches,
                "label": meta["label"],
            }
        else:
            snapshot["stock"][target_key] = {
                "present": False,
                "items": [],
                "label": meta["label"],
            }

    # Magic Mail variants: every rarity except Rare.
    magic = [x for x in stock if is_allowed_magic_mail(x)]
    for item in magic:
        rarity = rarity_from_item(item) or "unknown"
        identity = f"{key(item.get('name'))}|{rarity}"
        snapshot["magic_mail"][identity] = {
            "present": True,
            "name": item.get("name", "Magic Mail"),
            "rarity": rarity.upper() if rarity != "unknown" else "",
            "qty": int(item.get("qty", 0)),
            "type": item.get("type", "gear"),
        }

    # Sell: store actual current multiplier even if it is x1/x3/etc.
    for target_key, meta in SELL_TARGETS.items():
        candidates = [x for x in sell if key(x.get("name")) == target_key]
        if candidates:
            # If duplicate parser rows exist, prefer highest multiplier.
            item = max(candidates, key=lambda x: float(x.get("multi", 0)))
            snapshot["sell"][target_key] = {
                "present": True,
                "multi": float(item.get("multi", 0)),
                "label": meta["label"],
            }
        else:
            snapshot["sell"][target_key] = {
                "present": False,
                "multi": None,
                "label": meta["label"],
            }

    return snapshot


def stock_group_for_target(target_key, current_state):
    items = current_state.get("items") or []
    if items:
        typ = items[0].get("type")
        if typ in {"seed", "gear", "crate", "unknown"}:
            return typ

    # Fallback expected groups if page type classification is missing.
    if target_key in {"atlantic giant pumpkin", "amber cranberry"}:
        return "seed"
    if target_key in {"super syrup watering can", "super syrup sprinkler"}:
        return "gear"
    return "unknown"


def compare_target_events(old_state, current_snapshot, old_shop_fp, current_shop_fp):
    """
    Returns ONLY newly relevant alert events.

    For stock:
    - absent -> present => alert
    - qty/rarity changed => alert
    - same target is still present but its own shop restocked => alert again
      (lets consecutive restock cycles notify even if the wanted item remains)

    For Sell:
    - alert only when current value is x2 or x4
    - x1 -> x2, x2 -> x4, absent -> x2 => alert
    - if Sell rotation changes while the same wanted x2/x4 remains => alert again
    """
    events = []

    old_targets = old_state.get("targets", {}) if isinstance(old_state, dict) else {}
    old_stock = old_targets.get("stock", {})
    old_magic = old_targets.get("magic_mail", {})
    old_sell = old_targets.get("sell", {})

    # Exact stock targets
    for target_key, cur in current_snapshot["stock"].items():
        if not cur.get("present"):
            continue

        prev = old_stock.get(target_key, {"present": False, "items": []})
        group = stock_group_for_target(target_key, cur)
        group_changed = current_shop_fp.get(group) != old_shop_fp.get(group)

        cur_sig = stable_hash(cur.get("items", []))
        prev_sig = stable_hash(prev.get("items", []))

        if (not prev.get("present")) or (cur_sig != prev_sig) or group_changed:
            meta = EXACT_STOCK_TARGETS[target_key]
            events.append(
                {
                    "kind": "stock",
                    "target_key": target_key,
                    "label": meta["label"],
                    "emoji": meta["emoji"],
                    "items": cur.get("items", []),
                    "reason": (
                        "กลับเข้า Stock"
                        if not prev.get("present")
                        else "รอบร้านเปลี่ยน/จำนวนเปลี่ยน"
                    ),
                }
            )

    # Magic Mail variants
    all_magic_ids = set(current_snapshot["magic_mail"]) | set(old_magic)
    for identity in sorted(all_magic_ids):
        cur = current_snapshot["magic_mail"].get(identity)
        prev = old_magic.get(identity)

        if not cur or not cur.get("present"):
            continue

        group = cur.get("type") if cur.get("type") in current_shop_fp else "gear"
        group_changed = current_shop_fp.get(group) != old_shop_fp.get(group)

        cur_sig = stable_hash(cur)
        prev_sig = stable_hash(prev or {})

        if (not prev) or (cur_sig != prev_sig) or group_changed:
            events.append(
                {
                    "kind": "stock",
                    "target_key": identity,
                    "label": cur.get("name", "Magic Mail"),
                    "emoji": "✨",
                    "items": [
                        {
                            "name": cur.get("name", "Magic Mail"),
                            "qty": cur.get("qty", 0),
                            "rarity": cur.get("rarity", ""),
                            "type": cur.get("type", "gear"),
                        }
                    ],
                    "reason": "Magic Mail ที่ต้องการอยู่ใน Stock",
                }
            )

    # Sell targets
    for target_key, cur in current_snapshot["sell"].items():
        if not cur.get("present"):
            continue

        current_multi = cur.get("multi")
        if current_multi not in SELL_MULTIPLIERS:
            continue

        prev = old_sell.get(target_key, {"present": False, "multi": None})
        sell_rotation_changed = current_shop_fp.get("sell") != old_shop_fp.get("sell")

        if (
            (not prev.get("present"))
            or prev.get("multi") != current_multi
            or sell_rotation_changed
        ):
            meta = SELL_TARGETS[target_key]
            events.append(
                {
                    "kind": "sell",
                    "target_key": target_key,
                    "label": meta["label"],
                    "emoji": meta["emoji"],
                    "multi": current_multi,
                    "reason": (
                        f"Sell เปลี่ยนเป็น ×{current_multi:.0f}"
                        if prev.get("multi") != current_multi
                        else "Sell รอบใหม่ยังเข้าเงื่อนไข"
                    ),
                }
            )

    return events


def collect_live_data():
    diagnostics = []

    for attempt in range(1, MAX_READ_ATTEMPTS + 1):
        driver = None
        try:
            driver = make_driver()

            stock_sync = read_source_synced_stock(driver)
            stock = stock_sync["stock"]

            sell, sell_diag = read_sell_targets(driver)

            stock_ok = len(stock) >= MIN_STOCK_ITEMS

            # We watch exactly two sell fruits. Requiring both proves the Sell reader
            # can see the two values that matter to this bot.
            sell_ok = len(sell) >= len(SELL_TARGETS)

            diagnostics.append(
                {
                    "attempt": attempt,
                    "stock_count": len(stock),
                    "sell_count": len(sell),
                    "stock_ok": stock_ok,
                    "sell_ok": sell_ok,
                    "stock_sync_samples": stock_sync["samples"],
                    "stock_changed_during_sync": stock_sync["changed_during_sync"],
                    "stock_sample_counts": stock_sync["sample_counts"],
                    "stock_min_timer_seconds": stock_sync["min_timer_seconds"],
                    "sell_targets": sell_diag,
                }
            )

            if stock_ok and sell_ok:
                return {
                    "ok": True,
                    "attempts": attempt,
                    "stock": stock,
                    "sell": sell,
                    "diagnostics": diagnostics,
                    "source_sync": stock_sync,
                }

        except Exception as e:
            diagnostics.append(
                {
                    "attempt": attempt,
                    "error": f"{type(e).__name__}: {str(e)[:180]}",
                }
            )
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

        if attempt < MAX_READ_ATTEMPTS:
            time.sleep(3)

    return {
        "ok": False,
        "attempts": MAX_READ_ATTEMPTS,
        "stock": [],
        "sell": [],
        "diagnostics": diagnostics,
        "source_sync": None,
    }


def load_state():
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def send_discord(content):
    if not WEBHOOK_RE.fullmatch(WEBHOOK):
        raise RuntimeError("DISCORD_WEBHOOK secret is missing or invalid")

    r = requests.post(
        WEBHOOK,
        json={"content": content[:1950]},
        headers={"User-Agent": "GAG2-Reliability-Discord-Bot/6.1"},
        timeout=30,
    )

    if r.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord webhook failed: HTTP {r.status_code} {r.text[:200]}"
        )


def format_event_message(events, attempts):
    lines = [
        "🚨 **GAG2 เป้าหมายที่เฝ้าเจอแล้ว!**",
        f"🛡️ Reliability Mode · อ่านสำเร็จในครั้งที่ {attempts}",
    ]

    for event in events:
        if event["kind"] == "stock":
            lines += ["", f"{event['emoji']} **{event['label']}**"]
            for item in event["items"]:
                rarity = f" · {item.get('rarity')}" if item.get("rarity") else ""
                lines.append(
                    f"• {item.get('name', event['label'])} ×{item.get('qty', 0)}{rarity}"
                )
            lines.append(f"↳ {event['reason']}")

        elif event["kind"] == "sell":
            lines += [
                "",
                f"{event['emoji']} **{event['label']} Sell ×{event['multi']:.0f}**",
                f"↳ {event['reason']}",
            ]

    return "\n".join(lines)


def find_exact_stock(stock, target_key):
    return [x for x in stock if key(x.get("name")) == target_key]


def find_sell_value(sell, target_key):
    items = [x for x in sell if key(x.get("name")) == target_key]
    if not items:
        return None
    return max(items, key=lambda x: float(x.get("multi", 0))).get("multi")


def format_health_message(stock, sell, snapshot, attempts, recovered=False):
    lines = [
        "✅ **GAG2 Bot Health Check**",
        "🛡️ Reliability v6.1 Source-Sync",
        f"• Stock parser: **OK** ({len(stock)} รายการ)",
        f"• Sell parser: **OK** ({len(sell)} รายการ)",
        f"• อ่านสำเร็จในครั้งที่: **{attempts}/{MAX_READ_ATTEMPTS}**",
        "• Source-Sync: **ON** (อ่าน Stock ยืนยันซ้ำก่อนรับรอบ)",
        "• Sell reader: **Target DOM Probe**",
    ]

    if recovered:
        lines.append("• สถานะ: 🟢 **กลับมาทำงานปกติแล้ว**")

    lines += ["", "**เป้าหมาย Stock ตอนนี้**"]

    for target_key, meta in EXACT_STOCK_TARGETS.items():
        matches = find_exact_stock(stock, target_key)
        if matches:
            info = matches[0]
            rarity = f" · {info.get('rarity')}" if info.get("rarity") else ""
            lines.append(
                f"{meta['emoji']} {meta['label']}: ✅ ×{info.get('qty',0)}{rarity}"
            )
        else:
            lines.append(f"{meta['emoji']} {meta['label']}: ❌")

    magic = [x for x in stock if is_allowed_magic_mail(x)]
    rare_magic = [
        x for x in stock
        if "magic mail" in key(x.get("name"))
        and not is_allowed_magic_mail(x)
    ]

    if magic:
        desc = ", ".join(
            f"{x.get('rarity') or rarity_from_item(x).upper() or '?'} ×{x.get('qty',0)}"
            for x in magic[:6]
        )
        lines.append(f"✨ Magic Mail (ไม่รวม Rare): ✅ {desc}")
    else:
        lines.append("✨ Magic Mail (ไม่รวม Rare): ❌")

    if rare_magic:
        lines.append("🚫 Rare Magic Mail: พบแต่ **ตั้งใจไม่แจ้ง**")

    lines += ["", "**Sell ตอนนี้**"]
    for target_key, meta in SELL_TARGETS.items():
        v = find_sell_value(sell, target_key)
        if v is None:
            lines.append(f"{meta['emoji']} {meta['label']}: ไม่พบ")
        else:
            status = "🔔 เข้าเงื่อนไข" if float(v) in SELL_MULTIPLIERS else "เงียบ"
            lines.append(f"{meta['emoji']} {meta['label']}: ×{float(v):.2f} · {status}")

    lines += [
        "",
        "กฎ Sell: แจ้งเฉพาะ **×2 / ×4**",
        "กฎ Magic Mail: แจ้งทุกเกรด **ยกเว้น Rare**",
    ]

    return "\n".join(lines)


def should_send_health_failure_alert(old_state):
    health = old_state.get("health", {}) if isinstance(old_state, dict) else {}

    if health.get("status") != "error":
        return True

    last = health.get("last_error_alert_at")
    if not last:
        return True

    try:
        last_dt = datetime.fromisoformat(last)
        return utc_now() - last_dt >= timedelta(hours=HEALTH_ALERT_COOLDOWN_HOURS)
    except Exception:
        return True


def handle_read_failure(old_state, result):
    now = iso_now()
    new_state = dict(old_state) if isinstance(old_state, dict) else {}
    health = dict(new_state.get("health", {}))

    send_warning = should_send_health_failure_alert(old_state)

    if send_warning:
        diag = result.get("diagnostics", [])
        summary = []
        for d in diag[-3:]:
            if d.get("error"):
                summary.append(f"ครั้ง {d['attempt']}: {d['error']}")
            else:
                summary.append(
                    f"ครั้ง {d['attempt']}: Stock {d.get('stock_count',0)} / "
                    f"Sell {d.get('sell_count',0)}"
                )

        send_discord(
            "\n".join(
                [
                    "⚠️ **GAG2 Bot Health Warning**",
                    f"อ่านข้อมูลไม่ผ่านหลังลอง {MAX_READ_ATTEMPTS} ครั้ง",
                    "บอทจะ **ไม่เปลี่ยน baseline และไม่ส่ง Stock มั่ว**",
                    "",
                    *summary,
                    "",
                    "ระบบจะลองใหม่อัตโนมัติในรอบถัดไป",
                ]
            )
        )
        health["last_error_alert_at"] = now

    health.update(
        {
            "status": "error",
            "last_checked_at": now,
            "last_diagnostics": result.get("diagnostics", []),
        }
    )

    new_state["health"] = health
    new_state.setdefault("version", "6.1")
    save_state(new_state)


def main():
    if not WEBHOOK:
        raise RuntimeError("Missing GitHub Actions secret: DISCORD_WEBHOOK")

    old_state = load_state()
    result = collect_live_data()

    if not result["ok"]:
        handle_read_failure(old_state, result)
        print("Health failure saved; no stock baseline was changed")
        return

    stock = result["stock"]
    sell = result["sell"]
    attempts = result["attempts"]

    current_shop_fp = shop_fingerprints(stock, sell)
    current_snapshot = target_snapshot(stock, sell)

    old_shop_fp = old_state.get("shop_fingerprints", {})
    old_health_status = old_state.get("health", {}).get("status")
    recovered = old_health_status == "error"

    new_state = {
        "version": "6.1",
        "updated_at": iso_now(),
        "shop_fingerprints": current_shop_fp,
        "targets": current_snapshot,
        "health": {
            "status": "ok",
            "last_checked_at": iso_now(),
            "last_success_at": iso_now(),
            "stock_count": len(stock),
            "sell_count": len(sell),
            "attempts_used": attempts,
            "last_diagnostics": result.get("diagnostics", []),
            "last_error_alert_at": old_state.get("health", {}).get("last_error_alert_at"),
        },
    }

    has_baseline = bool(old_state.get("targets")) and bool(old_shop_fp)

    print(f"Parsed stock: {len(stock)} | sell: {len(sell)}")
    print(f"Read attempts used: {attempts}")
    print(f"Has v6.1 baseline: {has_baseline}")

    # Manual Run = explicit proof that the bot can currently read GAG2.
    if EVENT_NAME == "workflow_dispatch":
        send_discord(
            format_health_message(
                stock,
                sell,
                current_snapshot,
                attempts,
                recovered=recovered,
            )
        )
        save_state(new_state)
        print("Manual Health Check sent and v6.1 baseline saved")
        return

    # First scheduled v6 run establishes a clean baseline without spam.
    if not has_baseline:
        save_state(new_state)
        print("v6.1 initial baseline saved; no alert")
        return

    events = compare_target_events(
        old_state,
        current_snapshot,
        old_shop_fp,
        current_shop_fp,
    )

    # If the bot just recovered from an outage, tell the user once.
    if recovered:
        send_discord(
            "🟢 **GAG2 Bot recovered**\n"
            f"อ่าน Stock {len(stock)} / Sell {len(sell)} ได้ปกติแล้ว\n"
            "Reliability Mode กลับมาเฝ้าต่อเรียบร้อย"
        )

    if events:
        send_discord(format_event_message(events, attempts))
        print(f"Sent {len(events)} wanted event(s)")
    else:
        print("No new wanted event; silent")

    # Always save current target presence/absence after a valid read.
    # This is what lets a target disappear and later trigger again when it returns.
    save_state(new_state)


if __name__ == "__main__":
    main()
