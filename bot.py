import os
import re
import json
import hashlib
import time
import copy
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
TRIGGER_SOURCE = os.environ.get("TRIGGER_SOURCE", "").strip().lower()

RARITIES = r"(?:COMMON|UNCOMMON|RARE|EPIC|LEGENDARY|MYTHIC|SUPER)"
RARITY_WORDS = ("common", "uncommon", "rare", "epic", "legendary", "mythic", "super")
WEBHOOK_RE = re.compile(r"^https://discord\.com/api/webhooks/\d+/[A-Za-z0-9._-]+$")

SELL_MULTIPLIERS = {2.0, 4.0}
MAX_READ_ATTEMPTS = 3
SOURCE_SYNC_WAIT_SECONDS = 6
BOUNDARY_EXTRA_SECONDS = 8
BOUNDARY_TIMER_THRESHOLD = 30

# GAG2 Timer-Sync:
# The page can show the previous cycle briefly after a countdown rolls over.
# For frequent 5-minute shop timers, don't trust the first 30 seconds after reset.
GAG2_FREQUENT_CYCLE_SECONDS = 300
GAG2_POST_RESET_SAFE_AGE_SECONDS = 30
GAG2_AFTER_ZERO_GRACE_SECONDS = 25
GAG2_TIMER_SYNC_MAX_PASSES = 4
GAG2_TIMER_SYNC_MAX_TOTAL_WAIT = 95
GAG2_TIMER_TARGET_SHOPS = ("seed", "gear")

# v6.4.7 Multi-Snapshot Cycle Verify
# Compare the FULL rendered stock list, not only watched targets.
# If two consecutive safe snapshots match, the page is considered settled.
# If they differ, sample again until two consecutive snapshots agree.
MULTI_SNAPSHOT_CONFIRM_SECONDS = 8
MULTI_SNAPSHOT_MAX_EXTRA_READS = 2
MULTI_SNAPSHOT_REQUIRED_STABLE_PAIRS = 1
MULTI_SNAPSHOT_MIN_COMPARE_ITEMS = 3

# v6.4.8 Per-Shop Cycle ID
# Build a stable source-cycle key from GAG2's own countdown.
# The estimated next reset is rounded to a 30-second bucket so a few seconds
# of page/render delay do not create fake cycles.
SHOP_CYCLE_KEY_BUCKET_SECONDS = 30
SHOP_CYCLE_NAMES = ("seed", "gear", "crate")

MIN_STOCK_ITEMS = 3
MIN_SELL_ITEMS = 1
HEALTH_ALERT_COOLDOWN_HOURS = 1
ALERT_LOGIC_VERSION = "6.4.4-image-alert-v1"

# v6.5.0 Daily Statistics
THAILAND_TZ = timezone(timedelta(hours=7))
DAILY_STATS_RETENTION_DAYS = 4

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
    # Chrome performance log lets us detect exact document HTTP 403/429
    # without making extra requests to GAG2.
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return webdriver.Chrome(options=opts)



class GAG2AccessBlocked(RuntimeError):
    def __init__(self, kind, evidence, status_code=None):
        self.kind = kind
        self.evidence = norm(evidence)[:180]
        self.status_code = status_code
        super().__init__(
            f"{kind}"
            + (f" HTTP {status_code}" if status_code else "")
            + f": {self.evidence}"
        )


def detect_block_markers(title="", body="", current_url="", page_source=""):
    """
    Pure text detector for common access/rate-limit/challenge pages.
    Does NOT treat a bare number 403/429 as a block to avoid false positives.
    """
    blob = "\n".join([
        norm(title),
        norm(body)[:12000],
        norm(current_url),
        norm(page_source)[:20000],
    ]).lower()

    patterns = [
        ("429-rate-limit", 429, [
            "429 too many requests",
            "http 429",
            "error 429",
            "too many requests",
            "rate limit exceeded",
            "rate limited",
        ]),
        ("403-forbidden", 403, [
            "403 forbidden",
            "http 403",
            "error 403",
            "access denied",
            "request forbidden",
            "you don't have permission to access",
        ]),
        ("challenge-captcha", None, [
            "verify you are human",
            "checking your browser",
            "just a moment",
            "attention required",
            "security verification",
            "captcha",
            "cf-chl",
            "challenge-platform",
        ]),
    ]

    for kind, status, markers in patterns:
        for marker in markers:
            if marker in blob:
                return {
                    "blocked": True,
                    "kind": kind,
                    "status_code": status,
                    "evidence": marker,
                }

    return {
        "blocked": False,
        "kind": None,
        "status_code": None,
        "evidence": "",
    }


def detect_network_document_block(driver):
    """
    Inspect Chrome's own Network.responseReceived events.
    Only the main Document from gag2.gg counts, so a blocked image/font
    cannot falsely mark the whole site as blocked.
    """
    try:
        entries = driver.get_log("performance")
    except Exception:
        return None

    found = []
    for entry in entries:
        try:
            payload = json.loads(entry.get("message", "{}"))
            message = payload.get("message", {})
            if message.get("method") != "Network.responseReceived":
                continue

            params = message.get("params", {})
            if params.get("type") != "Document":
                continue

            response = params.get("response", {})
            url = str(response.get("url", ""))
            if "gag2.gg" not in url.lower():
                continue

            status = int(float(response.get("status", 0)))
            if status in (403, 429):
                found.append((status, url))
        except Exception:
            continue

    if not found:
        return None

    status, url = found[-1]
    return {
        "blocked": True,
        "kind": "429-rate-limit" if status == 429 else "403-forbidden",
        "status_code": status,
        "evidence": f"main document returned HTTP {status}: {url}",
    }


def assert_not_blocked(driver, body_text=None):
    network = detect_network_document_block(driver)
    if network:
        raise GAG2AccessBlocked(
            network["kind"],
            network["evidence"],
            network["status_code"],
        )

    try:
        title = driver.title
    except Exception:
        title = ""

    try:
        current_url = driver.current_url
    except Exception:
        current_url = ""

    if body_text is None:
        try:
            body_text = driver.find_element("tag name", "body").text
        except Exception:
            body_text = ""

    try:
        page_source = driver.page_source
    except Exception:
        page_source = ""

    marker = detect_block_markers(
        title=title,
        body=body_text,
        current_url=current_url,
        page_source=page_source,
    )
    if marker["blocked"]:
        raise GAG2AccessBlocked(
            marker["kind"],
            marker["evidence"],
            marker["status_code"],
        )


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
    body_text = driver.find_element("tag name", "body").text
    assert_not_blocked(driver, body_text)
    return body_text



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


def _timer_seconds(mm, ss):
    return int(mm) * 60 + int(ss)


def extract_shop_timers(text):
    """
    Extract timers next to the GAG2 Stock shop headings.
    Expected rendered forms include things like:
      Seed Shop ... 04:42
      Gear Shop ... 01:19
      Crate / Crates ... 12:08

    We deliberately use the timer shown by GAG2 itself instead of local clock time.
    """
    text = text or ""
    result = {}

    shop_patterns = {
        "seed": r"Seed\s+Shop",
        "gear": r"Gear\s+Shop",
        "crate": r"(?:Crate|Crates|Props?)",
    }

    # First: line-oriented parsing. This is the safest if heading + timer are together.
    lines = [norm(x) for x in text.splitlines() if norm(x)]
    for i, line in enumerate(lines):
        for shop, pat in shop_patterns.items():
            if shop in result or not re.search(pat, line, re.I):
                continue

            window = " ".join(lines[i:i + 4])
            m = re.search(r"(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)", window)
            if m:
                result[shop] = _timer_seconds(m.group(1), m.group(2))

    # Fallback: search a limited text window after each heading.
    for shop, pat in shop_patterns.items():
        if shop in result:
            continue
        m = re.search(
            rf"{pat}.{{0,180}}?(?<!\d)(\d{{1,2}}):([0-5]\d)(?!\d)",
            text,
            re.I | re.S,
        )
        if m:
            # crate pattern has a non-capturing internal group, timer stays 1/2.
            result[shop] = _timer_seconds(m.group(1), m.group(2))

    return result



def canonical_stock_map(stock):
    """
    Full-page stock identity map used only for source freshness verification.
    Identity = shop + normalized name + rarity; value = quantity.
    """
    out = {}
    for item in stock or []:
        typ = norm(item.get("type", "unknown")).lower() or "unknown"
        name = key(item.get("name", ""))
        rarity = norm(item.get("rarity", "")).upper()
        if not name:
            continue
        ident = f"{typ}|{name}|{rarity}"
        out[ident] = int(item.get("qty", 0))
    return out


def compare_stock_maps(old_stock, new_stock, limit=20):
    """
    Human-readable change summary across many stock items.
    This is diagnostic only; notification rules are unchanged.
    """
    a = canonical_stock_map(old_stock)
    b = canonical_stock_map(new_stock)

    added = []
    removed = []
    changed = []

    for ident in sorted(set(a) | set(b)):
        av = a.get(ident)
        bv = b.get(ident)

        if av is None:
            added.append({"id": ident, "qty": bv})
        elif bv is None:
            removed.append({"id": ident, "qty": av})
        elif av != bv:
            changed.append({"id": ident, "from": av, "to": bv})

    total = len(added) + len(removed) + len(changed)

    def short_ident(ident):
        parts = ident.split("|", 2)
        if len(parts) == 3:
            typ, name, rarity = parts
            label = name.title()
            if rarity:
                label += f" [{rarity}]"
            return f"{typ}:{label}"
        return ident

    examples = []
    for x in added[:6]:
        examples.append(f"+ {short_ident(x['id'])} ×{x['qty']}")
    for x in removed[:6]:
        examples.append(f"- {short_ident(x['id'])} ×{x['qty']}")
    for x in changed[:8]:
        examples.append(
            f"~ {short_ident(x['id'])} ×{x['from']}→×{x['to']}"
        )

    return {
        "total_changes": total,
        "added": len(added),
        "removed": len(removed),
        "qty_changed": len(changed),
        "examples": examples[:limit],
    }


def snapshot_is_comparable(snapshot):
    return len(snapshot.get("stock") or []) >= MULTI_SNAPSHOT_MIN_COMPARE_ITEMS


def consecutive_snapshot_stability(samples):
    """
    Find whether the newest two FULL-stock snapshots are identical.
    Matching is based on all parsed items/qty/rarity/shop, not only target items.
    """
    if len(samples) < 2:
        return False, 0, None

    stable_pairs = 0
    last_diff = None

    for i in range(1, len(samples)):
        prev = samples[i - 1]
        cur = samples[i]
        same = (
            snapshot_is_comparable(prev)
            and snapshot_is_comparable(cur)
            and prev.get("fingerprint") == cur.get("fingerprint")
        )

        if same:
            stable_pairs += 1
        else:
            stable_pairs = 0
            last_diff = compare_stock_maps(
                prev.get("stock", []),
                cur.get("stock", []),
            )

    return (
        stable_pairs >= MULTI_SNAPSHOT_REQUIRED_STABLE_PAIRS,
        stable_pairs,
        last_diff,
    )


def stock_snapshot(driver):
    captured_at_epoch = time.time()
    text = driver.find_element("tag name", "body").text
    assert_not_blocked(driver, text)
    stock = parse_stock(text)
    timers = extract_countdowns(text)
    shop_timers = extract_shop_timers(text)
    return {
        "stock": stock,
        "text": text,
        "fingerprint": stable_hash(stock),
        "timers": timers,
        "shop_timers": shop_timers,
        "captured_at_epoch": captured_at_epoch,
        "min_timer_seconds": min(timers) if timers else None,
    }



def shop_cycle_key_from_timer(captured_at_epoch, remaining_seconds):
    """
    Estimate GAG2's next reset time from the source countdown:
        capture_time + remaining
    and round it to a small bucket to absorb render/network jitter.

    Same source cycle => same key.
    Next source cycle => key advances by roughly 300 seconds.
    """
    try:
        captured_at_epoch = float(captured_at_epoch)
        remaining_seconds = int(remaining_seconds)
    except Exception:
        return None

    if captured_at_epoch <= 0 or remaining_seconds < 0:
        return None

    estimated_reset = captured_at_epoch + remaining_seconds
    bucket = SHOP_CYCLE_KEY_BUCKET_SECONDS
    rounded = int(round(estimated_reset / bucket) * bucket)
    return str(rounded)


def derive_shop_cycle_keys(source_sync, current_shop_fp):
    """
    Produce current cycle keys for Seed/Gear/Crate.

    Primary evidence = GAG2 countdown.
    Fallback = shop fingerprint when a labelled timer is unavailable.
    """
    timers = source_sync.get("shop_timers") or {}
    captured_at = source_sync.get("captured_at_epoch")
    result = {}

    for shop in SHOP_CYCLE_NAMES:
        timer = timers.get(shop)
        timer_key = (
            shop_cycle_key_from_timer(captured_at, timer)
            if timer is not None
            else None
        )

        if timer_key:
            result[shop] = {
                "key": f"timer:{timer_key}",
                "source": "gag2-timer",
                "remaining_seconds": int(timer),
            }
        else:
            fp = current_shop_fp.get(shop)
            result[shop] = {
                "key": f"fp:{fp}" if fp else None,
                "source": "fingerprint-fallback",
                "remaining_seconds": None,
            }

    return result


def update_shop_cycles(old_state, current_keys, current_shop_fp, source_sync):
    """
    Persistent per-shop cycle IDs.

    Crucial migration rule:
    - If old state has no cycle key yet, initialize without declaring a
      'new cycle', so installing v6.4.8 does not itself generate alerts.

    New-cycle evidence:
    - GAG2-derived cycle key changed; OR
    - fallback fingerprint changed when timer is unavailable; OR
    - source-sync explicitly observed a timer rollover inside this job.
    """
    old_cycles = (
        old_state.get("shop_cycles", {})
        if isinstance(old_state, dict)
        else {}
    )
    rollover_shops = {
        x.get("shop")
        for x in (source_sync.get("rollover_details") or [])
        if x.get("shop")
    }

    out = {}

    for shop in SHOP_CYCLE_NAMES:
        old = old_cycles.get(shop, {})
        old_id = int(old.get("id", 0) or 0)
        old_key = old.get("key")
        cur = current_keys.get(shop, {})
        cur_key = cur.get("key")

        # First time after upgrade: initialize, do not announce a new cycle.
        initialized = bool(old_key)
        changed = False

        if initialized and cur_key and cur_key != old_key:
            changed = True

        if initialized and shop in rollover_shops:
            changed = True

        # If neither timer nor explicit rollover is available, fingerprint is
        # a conservative fallback.
        if (
            initialized
            and cur.get("source") == "fingerprint-fallback"
            and old_state.get("shop_fingerprints", {}).get(shop)
            != current_shop_fp.get(shop)
        ):
            changed = True

        if old_id <= 0:
            cycle_id = 1
        else:
            cycle_id = old_id + 1 if changed else old_id

        out[shop] = {
            "id": cycle_id,
            "key": cur_key,
            "source": cur.get("source"),
            "remaining_seconds": cur.get("remaining_seconds"),
            "changed": changed,
        }

    return out


def shop_cycle_changed(shop_cycles, shop):
    return bool((shop_cycles.get(shop) or {}).get("changed"))


def timer_guard_for_snapshot(snapshot):
    """
    Decide how long to wait before trusting this GAG2 snapshot.

    Two dangerous windows:
    1) Timer is close to 00:00 -> wait through rollover + GAG2 grace.
    2) A frequent 5-minute timer has JUST reset high (e.g. 04:55) ->
       wait until at least ~30 seconds of the new cycle have elapsed.

    Returns (wait_seconds, reason, timers_used).
    """
    shop_timers = snapshot.get("shop_timers") or {}

    watched = {
        shop: int(shop_timers[shop])
        for shop in GAG2_TIMER_TARGET_SHOPS
        if shop in shop_timers
    }

    # Fallback only if shop-labelled timers could not be parsed.
    timer_source = "shop"
    if not watched:
        timer_source = "generic"
        generic = [
            int(v) for v in snapshot.get("timers", [])
            if 0 <= int(v) <= GAG2_FREQUENT_CYCLE_SECONDS
        ]
        watched = {f"timer{i+1}": v for i, v in enumerate(generic[:4])}

    waits = []

    for label, remaining in watched.items():
        # About to hit zero: cross the boundary and give the GAG2 frontend
        # enough time to replace old cards with the new cycle.
        if 0 <= remaining <= BOUNDARY_TIMER_THRESHOLD:
            waits.append(
                (
                    remaining + GAG2_AFTER_ZERO_GRACE_SECONDS,
                    f"{label} ใกล้ 00:00 ({remaining}s)",
                )
            )
            continue

        # Just after a 5-minute timer reset:
        # 04:59 means only ~1s into new cycle, 04:40 means ~20s in.
        if (
            GAG2_FREQUENT_CYCLE_SECONDS - GAG2_POST_RESET_SAFE_AGE_SECONDS
            < remaining
            <= GAG2_FREQUENT_CYCLE_SECONDS
        ):
            elapsed = GAG2_FREQUENT_CYCLE_SECONDS - remaining
            wait = GAG2_POST_RESET_SAFE_AGE_SECONDS - elapsed
            if wait > 0:
                waits.append(
                    (
                        wait,
                        f"{label} เพิ่งรีรอบ ({remaining}s เหลือ)",
                    )
                )

    if not waits:
        return 0, "timer-safe", watched, timer_source

    wait, reason = max(waits, key=lambda x: x[0])
    return max(1, int(wait)), reason, watched, timer_source


def read_source_synced_stock(driver):
    """
    v6.4.7 GAG2 Timer-Sync + Multi-Snapshot Cycle Verify.

    Evidence used together:
      1) GAG2's own countdown timer
      2) Full-stock fingerprint (all parsed names / qty / rarity / shop)
      3) Two consecutive settled snapshots

    Important:
    - A new cycle is allowed to have the SAME stock as the previous cycle.
      Timer rollover evidence + stable post-reset snapshots still make it valid.
    - A snapshot immediately after 00:00 is NOT trusted by itself.
    """
    rendered_text(
        driver,
        STOCK_URL,
        ["Seed Shop", "Gear Shop", "Crate", "stock"],
    )

    samples = [stock_snapshot(driver)]
    total_wait = 0
    guard_log = []
    rollover_seen = False
    rollover_details = []
    snapshot_diffs = []

    # Phase A: obey GAG2 timer boundary / post-reset safety window.
    for pass_no in range(1, GAG2_TIMER_SYNC_MAX_PASSES + 1):
        current = samples[-1]
        wait_s, reason, timers_used, timer_source = timer_guard_for_snapshot(current)

        if wait_s > 0:
            remaining_budget = GAG2_TIMER_SYNC_MAX_TOTAL_WAIT - total_wait
            if remaining_budget <= 0:
                break

            wait_s = min(wait_s, remaining_budget)
            guard_log.append(
                {
                    "pass": pass_no,
                    "phase": "timer-guard",
                    "wait_seconds": wait_s,
                    "reason": reason,
                    "timers": timers_used,
                    "source": timer_source,
                }
            )

            before = current.get("shop_timers") or {}
            time.sleep(wait_s)
            total_wait += wait_s

            driver.refresh()
            time.sleep(3)
            nxt = stock_snapshot(driver)
            after = nxt.get("shop_timers") or {}

            diff = compare_stock_maps(current.get("stock", []), nxt.get("stock", []))
            snapshot_diffs.append(
                {
                    "from_sample": len(samples),
                    "to_sample": len(samples) + 1,
                    **diff,
                }
            )

            # A real timer rollover can prove a new cycle even when the resulting
            # stock happens to be identical to the old cycle.
            for shop in set(before) & set(after):
                if (
                    before[shop] <= BOUNDARY_TIMER_THRESHOLD
                    and after[shop] > before[shop] + 30
                ):
                    rollover_seen = True
                    rollover_details.append(
                        {
                            "shop": shop,
                            "before_seconds": before[shop],
                            "after_seconds": after[shop],
                        }
                    )

            samples.append(nxt)
            continue

        # Mid-cycle / already-safe page: one initial spaced confirmation.
        if len(samples) == 1:
            time.sleep(SOURCE_SYNC_WAIT_SECONDS)
            total_wait += SOURCE_SYNC_WAIT_SECONDS

            driver.refresh()
            time.sleep(3)
            nxt = stock_snapshot(driver)

            diff = compare_stock_maps(current.get("stock", []), nxt.get("stock", []))
            snapshot_diffs.append(
                {
                    "from_sample": len(samples),
                    "to_sample": len(samples) + 1,
                    **diff,
                }
            )
            samples.append(nxt)
            continue

        break

    # Phase B: Full-stock stability verification.
    # If the latest snapshots differ, keep sampling at intervals until TWO
    # consecutive snapshots agree. This catches a late GAG2 frontend update.
    multi_stable, stable_pairs, last_diff = consecutive_snapshot_stability(samples)
    extra_reads = 0

    while (
        not multi_stable
        and extra_reads < MULTI_SNAPSHOT_MAX_EXTRA_READS
        and total_wait + MULTI_SNAPSHOT_CONFIRM_SECONDS
        <= GAG2_TIMER_SYNC_MAX_TOTAL_WAIT
    ):
        prev = samples[-1]

        time.sleep(MULTI_SNAPSHOT_CONFIRM_SECONDS)
        total_wait += MULTI_SNAPSHOT_CONFIRM_SECONDS

        driver.refresh()
        time.sleep(3)
        nxt = stock_snapshot(driver)

        diff = compare_stock_maps(prev.get("stock", []), nxt.get("stock", []))
        snapshot_diffs.append(
            {
                "from_sample": len(samples),
                "to_sample": len(samples) + 1,
                **diff,
            }
        )

        guard_log.append(
            {
                "pass": len(guard_log) + 1,
                "phase": "multi-snapshot",
                "wait_seconds": MULTI_SNAPSHOT_CONFIRM_SECONDS,
                "reason": (
                    "Full Stock ยังเปลี่ยนอยู่ จึงอ่านยืนยันซ้ำ"
                    if diff["total_changes"]
                    else "Full Stock ตรงกัน ยืนยันว่าหน้านิ่ง"
                ),
                "changes": diff["total_changes"],
            }
        )

        # Also detect timer rollover during confirmation.
        before = prev.get("shop_timers") or {}
        after = nxt.get("shop_timers") or {}
        for shop in set(before) & set(after):
            if (
                before[shop] <= BOUNDARY_TIMER_THRESHOLD
                and after[shop] > before[shop] + 30
            ):
                rollover_seen = True
                rollover_details.append(
                    {
                        "shop": shop,
                        "before_seconds": before[shop],
                        "after_seconds": after[shop],
                    }
                )

        samples.append(nxt)
        extra_reads += 1
        multi_stable, stable_pairs, last_diff = consecutive_snapshot_stability(samples)

    chosen = samples[-1]
    final_wait, final_reason, final_timers, final_source = timer_guard_for_snapshot(chosen)

    timer_available = bool(chosen.get("shop_timers") or chosen.get("timers"))
    timer_safe = final_wait == 0

    # Full snapshot must be comparable and settled.
    multi_snapshot_stable = (
        multi_stable
        and snapshot_is_comparable(chosen)
    )

    fingerprints = [x["fingerprint"] for x in samples]
    changed = len(set(fingerprints)) > 1

    # Cycle-confidence text for diagnostics / Discord Health Check.
    if not timer_available:
        cycle_confidence = "NO_TIMER"
    elif not timer_safe:
        cycle_confidence = "TIMER_UNSAFE"
    elif not multi_snapshot_stable:
        cycle_confidence = "SNAPSHOT_UNSTABLE"
    elif rollover_seen:
        cycle_confidence = "ROLLOVER_CONFIRMED_STABLE"
    elif changed:
        cycle_confidence = "CHANGED_THEN_STABLE"
    else:
        cycle_confidence = "MID_CYCLE_STABLE"

    last_two_same = (
        len(samples) >= 2
        and samples[-1]["fingerprint"] == samples[-2]["fingerprint"]
    )

    # Compare first observed stock vs final accepted candidate.
    first_to_final_diff = compare_stock_maps(
        samples[0].get("stock", []),
        chosen.get("stock", []),
    )

    return {
        "stock": chosen["stock"],
        "samples": len(samples),
        "changed_during_sync": changed,
        "rollover_seen": rollover_seen,
        "rollover_details": rollover_details,
        "timer_available": timer_available,
        "timer_safe": timer_safe,
        "timer_source": final_source,
        "shop_timers": chosen.get("shop_timers") or {},
        "captured_at_epoch": chosen.get("captured_at_epoch"),
        "timers_used": final_timers,
        "total_timer_wait_seconds": total_wait,
        "guard_log": guard_log,
        "min_timer_seconds": chosen["min_timer_seconds"],
        "sample_counts": [len(x["stock"]) for x in samples],
        "sample_fingerprints": fingerprints,
        "final_guard_reason": final_reason,

        # v6.4.7
        "multi_snapshot_stable": multi_snapshot_stable,
        "stable_pairs": stable_pairs,
        "last_two_same": last_two_same,
        "extra_confirm_reads": extra_reads,
        "cycle_confidence": cycle_confidence,
        "snapshot_diffs": snapshot_diffs,
        "first_to_final_diff": first_to_final_diff,
        "last_diff": last_diff,
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
    assert_not_blocked(driver, body_text)
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



GAG2_ITEM_IMAGE_OVERRIDES = {
    "atlantic giant pumpkin": "https://cdn.gag2.gg/items/atlantic_giant_pumpkin.webp",
    "maple mushroom": "https://cdn.gag2.gg/items/maple_mushroom.webp",
    "amber cranberry": "https://cdn.gag2.gg/items/amber_cranberry.webp",
    "super syrup sprinkler": "https://cdn.gag2.gg/items/super_syrup_sprinkler.webp",
    "super syrup watering can": "https://cdn.gag2.gg/items/super_syrup_watering_can.webp",
}


def gag2_item_image_url(item_name):
    """
    Exact GAG2 item image URL.

    Known watched items use verified explicit URLs.
    Magic Mail and any future simple item names use GAG2's item slug convention:
      "Legendary Magic Mail" -> legendary_magic_mail.webp
    """
    item_key = key(item_name)
    if item_key in GAG2_ITEM_IMAGE_OVERRIDES:
        return GAG2_ITEM_IMAGE_OVERRIDES[item_key]

    if "magic mail" in item_key:
        slug = re.sub(r"[^a-z0-9]+", "_", item_key).strip("_")
        if slug:
            return f"https://cdn.gag2.gg/items/{slug}.webp"

    return None


def _is_reasonable_image_url(url):
    url = norm(url)
    if not url:
        return False
    if url.startswith("data:"):
        return False
    return url.startswith("http://") or url.startswith("https://")


def _image_probe(driver, targets):
    script = r"""
    const targets = (arguments[0] || []).map(x => String(x || '').trim().toLowerCase()).filter(Boolean);
    const out = {};
    for (const t of targets) out[t] = [];

    function norm(s) {
      return String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    }

    function push(target, src) {
      if (!src) return;
      if (!/^https?:/i.test(src)) return;
      if (!out[target].includes(src)) out[target].push(src);
    }

    const nodes = Array.from(document.querySelectorAll('body *'));
    for (const el of nodes) {
      const own = norm(el.innerText || el.textContent || '');
      if (!own || own.length > 400) continue;

      for (const target of targets) {
        if (!(own === target || own.includes(target))) continue;

        let p = el;
        for (let depth = 0; depth < 7 && p; depth++, p = p.parentElement) {
          const scopeText = norm(p.innerText || p.textContent || '');
          if (!scopeText || scopeText.length > 1600) continue;

          const imgs = Array.from(p.querySelectorAll('img'));
          for (const img of imgs) {
            const src = img.currentSrc || img.src || img.getAttribute('src') || img.getAttribute('data-src');
            push(target, src);
          }

          if (out[target].length) break;
        }
      }
    }

    return out;
    """

    try:
        raw = driver.execute_script(script, targets) or {}
    except Exception:
        raw = {}

    cleaned = {}
    for t in targets:
        values = []
        for url in raw.get(t, []) or []:
            if _is_reasonable_image_url(url) and url not in values:
                values.append(url)
        cleaned[t] = values
    return cleaned


def read_stock_target_images(driver):
    targets = list(EXACT_STOCK_TARGETS.keys()) + [
        'magic mail',
        'legendary magic mail',
        'epic magic mail',
        'mythic magic mail',
        'super magic mail',
        'common magic mail',
        'uncommon magic mail',
    ]
    probed = _image_probe(driver, targets)
    result = {}
    for t, urls in probed.items():
        if urls:
            result[t] = urls[0]
    return result


def read_sell_target_images(driver):
    targets = list(SELL_TARGETS.keys())
    probed = _image_probe(driver, targets)
    result = {}
    for t, urls in probed.items():
        if urls:
            result[t] = urls[0]
    return result


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


def target_snapshot(stock, sell, stock_image_map=None, sell_image_map=None):
    """
    Store current state for every target, including states that do NOT trigger.
    This lets us distinguish:
      absent -> present
      x1 -> x2
      x2 -> x4
      target disappears -> later returns
    """
    stock_image_map = stock_image_map or {}
    sell_image_map = sell_image_map or {}

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
                "image_url": gag2_item_image_url(meta["label"]),
            }
        else:
            snapshot["stock"][target_key] = {
                "present": False,
                "items": [],
                "label": meta["label"],
                "image_url": gag2_item_image_url(meta["label"]),
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
            "image_url": gag2_item_image_url(item.get("name", "Magic Mail")),
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
                "image_url": gag2_item_image_url(meta["label"]),
            }
        else:
            snapshot["sell"][target_key] = {
                "present": False,
                "multi": None,
                "label": meta["label"],
                "image_url": gag2_item_image_url(meta["label"]),
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



def current_active_events(current_snapshot):
    """
    Return every target that is RIGHT NOW inside the user's alert rules.
    Used by Manual Run, first-run/bootstrap, and version migrations so a
    currently-active wanted item can never be silently swallowed as baseline.
    """
    events = []

    for target_key, cur in current_snapshot.get("stock", {}).items():
        if not cur.get("present"):
            continue
        meta = EXACT_STOCK_TARGETS[target_key]
        events.append(
            {
                "kind": "stock",
                "target_key": target_key,
                "label": meta["label"],
                "emoji": meta["emoji"],
                "items": cur.get("items", []),
                "reason": "ตอนนี้อยู่ใน Stock",
                "image_url": cur.get("image_url"),
            }
        )

    # target_snapshot already excludes Rare Magic Mail.
    for identity, cur in sorted(current_snapshot.get("magic_mail", {}).items()):
        if not cur.get("present"):
            continue
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
                "image_url": cur.get("image_url"),
            }
        )

    for target_key, cur in current_snapshot.get("sell", {}).items():
        if not cur.get("present"):
            continue
        multi = cur.get("multi")
        if multi not in SELL_MULTIPLIERS:
            continue
        meta = SELL_TARGETS[target_key]
        events.append(
            {
                "kind": "sell",
                "target_key": target_key,
                "label": meta["label"],
                "emoji": meta["emoji"],
                "multi": float(multi),
                "reason": f"ตอนนี้ Sell ×{float(multi):.0f} เข้าเงื่อนไข",
                "image_url": cur.get("image_url"),
            }
        )

    return events


def alert_rule_self_test():
    """
    Regression test for ALL seven alert classes:
      4 exact Stock targets
      Magic Mail except Rare
      2 Sell targets at x2/x4
    """
    stock = [
        {"name": "Atlantic Giant Pumpkin", "qty": 1, "rarity": "SUPER", "type": "seed"},
        {"name": "Super Syrup Watering Can", "qty": 2, "rarity": "SUPER", "type": "gear"},
        {"name": "Super Syrup Sprinkler", "qty": 3, "rarity": "SUPER", "type": "gear"},
        {"name": "Amber Cranberry", "qty": 4, "rarity": "LEGENDARY", "type": "seed"},
        {"name": "Legendary Magic Mail", "qty": 1, "rarity": "LEGENDARY", "type": "gear"},
        {"name": "Rare Magic Mail", "qty": 99, "rarity": "RARE", "type": "gear"},
    ]
    sell = [
        {"name": "Maple Mushroom", "multi": 2.0},
        {"name": "Atlantic Giant Pumpkin", "multi": 4.0},
    ]

    snapshot = target_snapshot(stock, sell)
    events = current_active_events(snapshot)

    stock_keys = {
        e["target_key"] for e in events
        if e["kind"] == "stock" and e["target_key"] in EXACT_STOCK_TARGETS
    }
    sell_keys = {e["target_key"] for e in events if e["kind"] == "sell"}
    magic_events = [
        e for e in events
        if e["kind"] == "stock" and "magic mail" in key(e.get("label"))
    ]

    errors = []
    if stock_keys != set(EXACT_STOCK_TARGETS):
        errors.append(f"exact-stock mismatch: {sorted(stock_keys)}")
    if sell_keys != set(SELL_TARGETS):
        errors.append(f"sell mismatch: {sorted(sell_keys)}")
    if len(magic_events) != 1 or "legendary magic mail" not in key(magic_events[0]["label"]):
        errors.append("Magic Mail allow/exclude rule failed")
    if any("rare magic mail" in key(e.get("label")) for e in events):
        errors.append("Rare Magic Mail incorrectly alerted")

    silent_snapshot = target_snapshot(
        [],
        [
            {"name": "Maple Mushroom", "multi": 1.0},
            {"name": "Atlantic Giant Pumpkin", "multi": 3.0},
        ],
    )
    if any(e["kind"] == "sell" for e in current_active_events(silent_snapshot)):
        errors.append("non-target sell multiplier incorrectly alerted")

    return {
        "ok": not errors,
        "passed_classes": 7 if not errors else 0,
        "total_classes": 7,
        "errors": errors,
    }


def compare_target_events(old_state, current_snapshot, old_shop_fp, current_shop_fp, current_shop_cycles):
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
        cycle_changed = shop_cycle_changed(current_shop_cycles, group)

        cur_sig = stable_hash(cur.get("items", []))
        prev_sig = stable_hash(prev.get("items", []))

        if (
            (not prev.get("present"))
            or (cur_sig != prev_sig)
            or cycle_changed
            or group_changed
        ):
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
                        else (
                            "รอบร้านใหม่"
                            if cycle_changed
                            else "รายการ/จำนวนเปลี่ยน"
                        )
                    ),
                    "image_url": cur.get("image_url"),
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
        cycle_changed = shop_cycle_changed(current_shop_cycles, group)

        cur_sig = stable_hash(cur)
        prev_sig = stable_hash(prev or {})

        if (
            (not prev)
            or (cur_sig != prev_sig)
            or cycle_changed
            or group_changed
        ):
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
                    "image_url": cur.get("image_url"),
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
                    "image_url": cur.get("image_url"),
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

            # v6.4.5: do not infer image-to-item pairing from nearby DOM cards.
            # Exact CDN image URLs are generated from the actual target item name.
            stock_image_map = {}
            sell_image_map = {}

            stock_ok = (
                len(stock) >= MIN_STOCK_ITEMS
                and stock_sync.get("timer_available")
                and stock_sync.get("timer_safe")
                and stock_sync.get("multi_snapshot_stable")
            )

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
                    "timer_available": stock_sync.get("timer_available"),
                    "timer_safe": stock_sync.get("timer_safe"),
                    "timer_source": stock_sync.get("timer_source"),
                    "shop_timers": stock_sync.get("shop_timers"),
                    "timer_wait_seconds": stock_sync.get("total_timer_wait_seconds"),
                    "timer_guard_log": stock_sync.get("guard_log"),
                    "multi_snapshot_stable": stock_sync.get("multi_snapshot_stable"),
                    "stable_pairs": stock_sync.get("stable_pairs"),
                    "cycle_confidence": stock_sync.get("cycle_confidence"),
                    "snapshot_diffs": stock_sync.get("snapshot_diffs"),
                    "first_to_final_diff": stock_sync.get("first_to_final_diff"),
                    "rollover_seen": stock_sync.get("rollover_seen"),
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
                    "stock_image_map": stock_image_map,
                    "sell_image_map": sell_image_map,
                }

        except GAG2AccessBlocked as e:
            diagnostics.append(
                {
                    "attempt": attempt,
                    "access_block": True,
                    "block_kind": e.kind,
                    "status_code": e.status_code,
                    "block_evidence": e.evidence,
                    "error": f"GAG2AccessBlocked: {str(e)[:180]}",
                }
            )
            print(
                f"GAG2 access block detected on attempt {attempt}: "
                f"kind={e.kind} status={e.status_code or 'marker'} evidence={e.evidence}"
            )
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
        "stock_image_map": {},
        "sell_image_map": {},
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


def send_discord(content="", embeds=None):
    if not WEBHOOK_RE.fullmatch(WEBHOOK):
        raise RuntimeError("DISCORD_WEBHOOK secret is missing or invalid")

    payload = {"allowed_mentions": {"parse": []}}
    if content:
        payload["content"] = content[:1950]
    if embeds:
        payload["embeds"] = embeds[:10]
    if not payload.get("content") and not payload.get("embeds"):
        raise RuntimeError("send_discord called with no content and no embeds")

    r = requests.post(
        WEBHOOK,
        json=payload,
        headers={"User-Agent": "GAG2-Reliability-Discord-Bot/6.5.0"},
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


def format_single_event_message(event, attempts):
    lines = [
        f"🚨 **{event['label']}**",
        f"🛡️ Reliability Mode · อ่านสำเร็จในครั้งที่ {attempts}",
    ]

    if event["kind"] == "stock":
        for item in event.get("items", []):
            rarity = f" · {item.get('rarity')}" if item.get("rarity") else ""
            lines.append(
                f"• {item.get('name', event['label'])} ×{item.get('qty', 0)}{rarity}"
            )
    elif event["kind"] == "sell":
        lines.append(f"• Sell ×{float(event.get('multi', 0)):.0f}")

    lines.append(f"↳ {event['reason']}")
    return "\n".join(lines)


def build_event_embed(event, attempts):
    image_url = event.get("image_url")
    if not _is_reasonable_image_url(image_url):
        return None

    if event["kind"] == "stock":
        title = f"{event['emoji']} {event['label']} เข้า Stock!"
        desc_lines = []
        for item in event.get("items", []):
            rarity = f" · {item.get('rarity')}" if item.get("rarity") else ""
            desc_lines.append(
                f"• {item.get('name', event['label'])} ×{item.get('qty', 0)}{rarity}"
            )
        color = 0x57F287
    else:
        title = f"{event['emoji']} {event['label']} Sell ×{float(event.get('multi', 0)):.0f}"
        desc_lines = [f"• เข้าเงื่อนไข Sell ×{float(event.get('multi', 0)):.0f}"]
        color = 0xFEE75C

    desc_lines.append(f"↳ {event['reason']}")

    embed = {
        "title": title[:250],
        "description": "\n".join(desc_lines)[:4000],
        "color": color,
        "thumbnail": {"url": image_url},
        "footer": {"text": f"v6.5.0 Daily Statistics • attempts {attempts}"},
    }
    return embed


def send_event_alerts(events, attempts):
    for event in events:
        content = format_single_event_message(event, attempts)
        embed = build_event_embed(event, attempts)
        send_discord(content, [embed] if embed else None)



def send_image_self_test():
    """
    Manual-only visual test.
    This is explicitly labeled as TEST and does not touch target state/baseline.
    """
    tests = [
        {
            "title": "🎃 TEST — Atlantic Giant Pumpkin",
            "description": "🧪 ทดสอบรูปเท่านั้น — **ไม่ใช่สต็อกจริง**",
            "thumbnail": {"url": gag2_item_image_url("Atlantic Giant Pumpkin")},
            "color": 0x57F287,
        },
        {
            "title": "🍄 TEST — Maple Mushroom",
            "description": "🧪 ทดสอบรูปเท่านั้น — **ไม่ใช่ Sell จริง**",
            "thumbnail": {"url": gag2_item_image_url("Maple Mushroom")},
            "color": 0xFEE75C,
        },
    ]

    valid = []
    for embed in tests:
        url = embed.get("thumbnail", {}).get("url")
        if _is_reasonable_image_url(url):
            embed["footer"] = {"text": "v6.4.9 Thumbnail Self-Test"}
            valid.append(embed)

    send_discord(
        "🧪 **GAG2 Image Self-Test**\n"
        "ข้อความนี้ใช้เช็กรูปเท่านั้น **ไม่ใช่การแจ้ง Stock/Sell จริง**",
        valid,
    )


def find_exact_stock(stock, target_key):
    return [x for x in stock if key(x.get("name")) == target_key]


def find_sell_value(sell, target_key):
    items = [x for x in sell if key(x.get("name")) == target_key]
    if not items:
        return None
    return max(items, key=lambda x: float(x.get("multi", 0))).get("multi")


def format_health_message(stock, sell, snapshot, attempts, recovered=False, self_test=None, source_sync=None, shop_cycles=None):
    lines = [
        "✅ **GAG2 Bot Health Check**",
        "🛡️ Reliability v6.5.0 Daily Statistics + Thumbnail + Per-Shop Cycle + Smart State + Block Detector + Timer-Sync",
        f"• Stock parser: **OK** ({len(stock)} รายการ)",
        f"• Sell parser: **OK** ({len(sell)} รายการ)",
        f"• อ่านสำเร็จในครั้งที่: **{attempts}/{MAX_READ_ATTEMPTS}**",
        "• Source-Sync: **ON**",
        "• GAG2 Timer-Sync: **ON** (อิง Countdown จากหน้า GAG2)",
        "• Multi-Snapshot Verify: **ON** (เทียบ Full Stock หลายช่วง)",
        "• Daily Statistics: **ON** (เวลาไทย · นับต่อรอบ)",
        "• Sell reader: **Target DOM Probe**",
        "• Block detector: **ON** (403 / 429 / CAPTCHA / Access Denied)",
    ]

    if source_sync:
        shops = source_sync.get("shop_timers") or {}
        if shops:
            pretty = []
            for shop in ("seed", "gear", "crate"):
                if shop in shops:
                    value = int(shops[shop])
                    pretty.append(f"{shop} {value//60:02d}:{value%60:02d}")
            if pretty:
                lines.append("• GAG timers: **" + " · ".join(pretty) + "**")

        lines.append(
            f"• Timer wait รอบนี้: **{int(source_sync.get('total_timer_wait_seconds', 0))}s**"
        )
        lines.append(
            f"• Snapshot: **{source_sync.get('samples', 0)} ครั้ง** · "
            f"Stable: **{'YES' if source_sync.get('multi_snapshot_stable') else 'NO'}**"
        )
        lines.append(
            f"• Cycle confidence: **{source_sync.get('cycle_confidence', 'UNKNOWN')}**"
        )

        if shop_cycles:
            cycle_parts = []
            for shop in SHOP_CYCLE_NAMES:
                c = shop_cycles.get(shop) or {}
                if c.get("id"):
                    marker = "🆕" if c.get("changed") else ""
                    cycle_parts.append(
                        f"{shop} #{c.get('id')}{marker}"
                    )
            if cycle_parts:
                lines.append(
                    "• Shop Cycle IDs: **" + " · ".join(cycle_parts) + "**"
                )
        first_final = source_sync.get("first_to_final_diff") or {}
        if first_final.get("total_changes", 0):
            lines.append(
                "• Full Stock เปลี่ยนระหว่างตรวจ: "
                f"**{first_final.get('total_changes', 0)} รายการ** "
                f"(+{first_final.get('added', 0)} / "
                f"-{first_final.get('removed', 0)} / "
                f"qty {first_final.get('qty_changed', 0)})"
            )
        elif source_sync.get("rollover_seen"):
            lines.append(
                "• Timer ยืนยัน **รอบใหม่** แม้รายการ Stock จะบังเอิญเหมือนรอบก่อน"
            )

    if self_test and self_test.get("ok"):
        lines.append(
            f"• Alert rules self-test: **PASS ({self_test.get('passed_classes', 0)}/{self_test.get('total_classes', 7)})**"
        )
    elif self_test:
        lines.append("• Alert rules self-test: **FAIL**")

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

    if health.get("status") not in {"error", "blocked"}:
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

    diagnostics = result.get("diagnostics", [])
    block_diags = [d for d in diagnostics if d.get("access_block")]
    blocked = bool(block_diags)

    send_warning = should_send_health_failure_alert(old_state)

    if send_warning:
        if blocked:
            last = block_diags[-1]
            status_code = last.get("status_code")
            kind = last.get("block_kind", "access-block")
            evidence = last.get("block_evidence", "")

            if status_code == 429:
                headline = "🚦 **GAG2 Rate Limit Warning (HTTP 429)**"
                advice = (
                    "ถ้าเกิดซ้ำหลายรอบ แนะนำเพิ่ม Cloudflare จาก **2 นาที → 3–5 นาที**"
                )
            elif status_code == 403:
                headline = "🚧 **GAG2 Access Warning (HTTP 403)**"
                advice = (
                    "ถ้าเกิดซ้ำหลายรอบ แนะนำลดความถี่เป็น **3–5 นาที** และตรวจว่าเว็บเปิดปกติ"
                )
            else:
                headline = "🧩 **GAG2 Challenge / CAPTCHA Warning**"
                advice = (
                    "หน้าเว็บดูเหมือนมี CAPTCHA/Challenge; ถ้าเกิดซ้ำให้ลดความถี่เป็น **3–5 นาที**"
                )

            attempts_hit = ", ".join(str(d.get("attempt")) for d in block_diags)
            send_discord(
                "\n".join(
                    [
                        headline,
                        "บอทตรวจพบว่า GAG2 อาจกำลังจำกัด/ตรวจสอบการเข้าถึง",
                        f"• ประเภท: **{kind}**",
                        f"• พบในครั้งที่: **{attempts_hit}**",
                        f"• หลักฐาน: `{evidence[:120]}`",
                        "",
                        "บอทจะ **ไม่เปลี่ยน baseline และไม่ส่ง Stock มั่ว**",
                        advice,
                        "ระบบจะลองใหม่อัตโนมัติในรอบถัดไป",
                    ]
                )
            )
        else:
            summary = []
            for d in diagnostics[-3:]:
                if d.get("error"):
                    summary.append(f"ครั้ง {d['attempt']}: {d['error']}")
                else:
                    timer_state = (
                        "NO TIMER"
                        if not d.get("timer_available")
                        else ("SAFE" if d.get("timer_safe") else "UNSAFE")
                    )
                    snap_state = (
                        "STABLE"
                        if d.get("multi_snapshot_stable")
                        else "UNSTABLE"
                    )
                    confidence = d.get("cycle_confidence") or "UNKNOWN"

                    summary.append(
                        f"ครั้ง {d['attempt']}: Stock {d.get('stock_count',0)} / "
                        f"Sell {d.get('sell_count',0)} · "
                        f"Timer {timer_state} · Snapshot {snap_state} · {confidence}"
                    )

                    diff = d.get("first_to_final_diff") or {}
                    if diff.get("total_changes", 0):
                        summary.append(
                            f"↳ เทียบ Full Stock เปลี่ยน {diff.get('total_changes',0)} รายการ "
                            f"(+{diff.get('added',0)} / -{diff.get('removed',0)} / "
                            f"qty {diff.get('qty_changed',0)})"
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
            "status": "blocked" if blocked else "error",
            "last_checked_at": now,
            "last_diagnostics": diagnostics,
            "last_block_kind": block_diags[-1].get("block_kind") if blocked else None,
            "last_block_status_code": block_diags[-1].get("status_code") if blocked else None,
        }
    )

    new_state["health"] = health
    new_state.setdefault("version", "6.5.0")
    save_state(new_state)




def thailand_now():
    return utc_now().astimezone(THAILAND_TZ)


def thailand_date_str():
    return thailand_now().date().isoformat()


def yesterday_thailand_date_str():
    return (thailand_now().date() - timedelta(days=1)).isoformat()


def empty_daily_day():
    return {
        "stock_occurrences": {},
        "stock_seen_cycles": {},
        "magic_mail": {},
        "magic_seen_cycles": {},
        "sell": {},
        "sell_seen_rotations": {},
        "alerts_sent": 0,
    }


def normalize_daily_stats(old_state):
    raw = copy.deepcopy(
        old_state.get("daily_stats", {})
        if isinstance(old_state, dict)
        else {}
    )
    if not isinstance(raw, dict):
        raw = {}

    raw.setdefault("timezone", "Asia/Bangkok")
    raw.setdefault("last_summary_date", None)
    raw.setdefault("days", {})

    if not isinstance(raw["days"], dict):
        raw["days"] = {}

    return raw


def ensure_daily_day(stats, day_key):
    days = stats.setdefault("days", {})
    day = days.get(day_key)

    if not isinstance(day, dict):
        day = empty_daily_day()
        days[day_key] = day

    defaults = empty_daily_day()
    for k, default in defaults.items():
        if k not in day or not isinstance(day[k], type(default)):
            day[k] = copy.deepcopy(default)

    return day


def cycle_marker_for_shop(shop_cycles, shop, current_shop_fp):
    cycle = (shop_cycles.get(shop) or {})
    key_value = cycle.get("key")
    cycle_id = int(cycle.get("id", 0) or 0)

    if key_value:
        return f"{shop}|{key_value}"

    if cycle_id:
        return f"{shop}|id:{cycle_id}"

    fp = current_shop_fp.get(shop)
    return f"{shop}|fp:{fp}" if fp else None


def _register_unique_seen(seen_map, counter_map, key_name, marker):
    if not marker:
        return False

    seen = seen_map.setdefault(key_name, [])
    if marker in seen:
        return False

    seen.append(marker)
    counter_map[key_name] = int(counter_map.get(key_name, 0) or 0) + 1
    return True


def update_daily_occurrence_stats(
    daily_stats,
    stock,
    sell,
    current_snapshot,
    current_shop_cycles,
    current_shop_fp,
):
    """
    Count detected occurrences once per source cycle/rotation.

    Exact Stock:
      once per Seed/Gear/Crate cycle.

    Magic Mail:
      every rarity INCLUDING Rare for statistics, once per shop cycle.
      Rare is still excluded from alerts.

    Sell:
      x2/x4 once per distinct detected Sell rotation fingerprint.

    Manual runs never call this.
    """
    day_key = thailand_date_str()
    day = ensure_daily_day(daily_stats, day_key)

    # Exact Stock targets.
    for target_key, cur in current_snapshot.get("stock", {}).items():
        if not cur.get("present"):
            continue

        group = stock_group_for_target(target_key, cur)
        marker = cycle_marker_for_shop(
            current_shop_cycles,
            group,
            current_shop_fp,
        )
        _register_unique_seen(
            day["stock_seen_cycles"],
            day["stock_occurrences"],
            target_key,
            marker,
        )

    # All Magic Mail rarities, including Rare, for statistics.
    magic_rarities_in_cycle = {}
    for item in stock or []:
        if "magic mail" not in key(item.get("name")):
            continue

        rarity = (rarity_from_item(item) or "unknown").lower()
        group = item.get("type")
        if group not in SHOP_CYCLE_NAMES:
            group = "gear"

        magic_rarities_in_cycle.setdefault(rarity, group)

    for rarity, group in magic_rarities_in_cycle.items():
        marker = cycle_marker_for_shop(
            current_shop_cycles,
            group,
            current_shop_fp,
        )
        _register_unique_seen(
            day["magic_seen_cycles"],
            day["magic_mail"],
            rarity,
            marker,
        )

    # Sell x2/x4.
    sell_fp = current_shop_fp.get("sell")
    for target_key, cur in current_snapshot.get("sell", {}).items():
        if not cur.get("present"):
            continue

        multi = cur.get("multi")
        if multi not in SELL_MULTIPLIERS:
            continue

        multi_key = str(int(float(multi)))
        target_counts = day["sell"].setdefault(
            target_key,
            {"2": 0, "4": 0},
        )
        target_seen = day["sell_seen_rotations"].setdefault(
            target_key,
            {"2": [], "4": []},
        )

        marker = (
            f"sell|{sell_fp}|{target_key}|x{multi_key}"
            if sell_fp
            else None
        )

        if marker and marker not in target_seen.setdefault(multi_key, []):
            target_seen[multi_key].append(marker)
            target_counts[multi_key] = int(
                target_counts.get(multi_key, 0) or 0
            ) + 1

    return day_key


def add_daily_alert_count(daily_stats, count):
    if not count:
        return

    day = ensure_daily_day(daily_stats, thailand_date_str())
    day["alerts_sent"] = int(day.get("alerts_sent", 0) or 0) + int(count)


def format_daily_summary(day_key, day):
    stock_counts = day.get("stock_occurrences", {}) or {}
    magic_counts = day.get("magic_mail", {}) or {}
    sell_counts = day.get("sell", {}) or {}

    lines = [
        f"📊 **GAG2 Daily Summary — {day_key}**",
        "🕛 เวลาไทย 00:00–23:59",
        "นับเป็น **รอบที่ตรวจพบจริง** ไม่ได้นับทุกการสแกน 2 นาที",
        "",
        "📦 **Stock**",
    ]

    for target_key, meta in EXACT_STOCK_TARGETS.items():
        count = int(stock_counts.get(target_key, 0) or 0)
        lines.append(
            f"{meta['emoji']} {meta['label']}: **{count} รอบ**"
        )

    lines += ["", "✨ **Magic Mail**"]

    rarity_order = [
        "common",
        "uncommon",
        "epic",
        "legendary",
        "mythic",
        "super",
        "rare",
        "unknown",
    ]
    shown = set()

    for rarity in rarity_order:
        count = int(magic_counts.get(rarity, 0) or 0)
        if count <= 0 and rarity == "unknown":
            continue

        shown.add(rarity)
        label = rarity.title()
        suffix = " *(ไม่นำไปแจ้ง)*" if rarity == "rare" else ""
        lines.append(f"• {label}: **{count} รอบ**{suffix}")

    for rarity in sorted(set(magic_counts) - shown):
        count = int(magic_counts.get(rarity, 0) or 0)
        lines.append(f"• {rarity.title()}: **{count} รอบ**")

    lines += ["", "💰 **Sell ×2 / ×4**"]

    for target_key, meta in SELL_TARGETS.items():
        cur = sell_counts.get(target_key, {}) or {}
        x2 = int(cur.get("2", 0) or 0)
        x4 = int(cur.get("4", 0) or 0)
        lines.append(
            f"{meta['emoji']} {meta['label']}: "
            f"×2 **{x2} รอบ** · ×4 **{x4} รอบ**"
        )

    lines += [
        "",
        f"🔔 แจ้งเตือนเป้าหมายจริงทั้งหมด: **{int(day.get('alerts_sent', 0) or 0)} ครั้ง**",
        "🧪 Manual Run / Health Check / Image Self-Test: **ไม่นับ**",
    ]

    return "\n".join(lines)


def prune_daily_stats(daily_stats):
    days = daily_stats.get("days", {})
    if not isinstance(days, dict):
        return

    keys = sorted(days.keys())
    if len(keys) <= DAILY_STATS_RETENTION_DAYS:
        return

    keep = set(keys[-DAILY_STATS_RETENTION_DAYS:])
    for k in list(days):
        if k not in keep:
            days.pop(k, None)


def maybe_send_daily_summary(daily_stats):
    """
    First successful automatic run after midnight Thailand sends
    exactly one summary for yesterday.

    If there is no collected previous-day data, stay silent instead of
    sending a misleading zero summary.
    """
    yesterday = yesterday_thailand_date_str()

    if daily_stats.get("last_summary_date") == yesterday:
        return False

    day = (daily_stats.get("days", {}) or {}).get(yesterday)

    if not isinstance(day, dict):
        daily_stats["last_summary_date"] = yesterday
        prune_daily_stats(daily_stats)
        return False

    send_discord(format_daily_summary(yesterday, day))
    daily_stats["last_summary_date"] = yesterday
    prune_daily_stats(daily_stats)
    return True



def persistent_cycle_state(shop_cycles):
    """
    Remove run-local fields so state.json changes only when a cycle identity
    actually changes, not because countdown remaining seconds changed.
    """
    out = {}
    for shop in SHOP_CYCLE_NAMES:
        cur = shop_cycles.get(shop) or {}
        out[shop] = {
            "id": int(cur.get("id", 1) or 1),
            "key": cur.get("key"),
            "source": cur.get("source"),
        }
    return out


def semantic_state_view(state):
    """
    Fields that affect future alert correctness.
    Volatile timestamps/diagnostics/image presentation are intentionally excluded.
    """
    if not isinstance(state, dict):
        return {}

    health = state.get("health", {}) or {}

    targets = copy.deepcopy(state.get("targets", {}))
    for cur in (targets.get("stock", {}) or {}).values():
        cur.pop("image_url", None)
    for cur in (targets.get("magic_mail", {}) or {}).values():
        cur.pop("image_url", None)
    for cur in (targets.get("sell", {}) or {}).values():
        cur.pop("image_url", None)

    return {
        "alert_logic_version": state.get("alert_logic_version"),
        "shop_fingerprints": state.get("shop_fingerprints", {}),
        "shop_cycles": state.get("shop_cycles", {}),
        "targets": targets,
        "daily_stats": state.get("daily_stats", {}),
        "health": {
            "status": health.get("status"),
            "last_error_alert_at": health.get("last_error_alert_at"),
            "last_block_kind": health.get("last_block_kind"),
            "last_block_status_code": health.get("last_block_status_code"),
        },
    }


def smart_save_state(old_state, new_state, force=False):
    """
    Save only when future alert behavior could change.

    Cloudflare still scans every 2 minutes.
    This only reduces state.json commits.
    """
    changed = semantic_state_view(old_state) != semantic_state_view(new_state)

    if not changed and not force:
        print("Smart State Save: no semantic change; state.json unchanged")
        return False

    new_state = copy.deepcopy(new_state)
    new_state["updated_at"] = iso_now()
    save_state(new_state)
    print("Smart State Save: semantic state changed; state.json updated")
    return True


def main():
    if not WEBHOOK:
        raise RuntimeError("Missing GitHub Actions secret: DISCORD_WEBHOOK")

    self_test = alert_rule_self_test()
    if not self_test["ok"]:
        raise RuntimeError(
            "Alert rule self-test failed: " + "; ".join(self_test.get("errors", []))
        )

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

    current_cycle_keys = derive_shop_cycle_keys(
        result.get("source_sync") or {},
        current_shop_fp,
    )
    current_shop_cycles = update_shop_cycles(
        old_state,
        current_cycle_keys,
        current_shop_fp,
        result.get("source_sync") or {},
    )

    current_snapshot = target_snapshot(
        stock,
        sell,
        result.get("stock_image_map"),
        result.get("sell_image_map"),
    )
    current_events = current_active_events(current_snapshot)

    old_shop_fp = old_state.get("shop_fingerprints", {})
    old_health_status = old_state.get("health", {}).get("status")
    recovered = old_health_status in {"error", "blocked"}

    old_health = old_state.get("health", {}) if isinstance(old_state, dict) else {}

    new_state = {
        "version": "6.5.0",
        "alert_logic_version": ALERT_LOGIC_VERSION,
        "updated_at": old_state.get("updated_at"),
        "shop_fingerprints": current_shop_fp,
        "shop_cycles": persistent_cycle_state(current_shop_cycles),
        "targets": current_snapshot,
        "daily_stats": normalize_daily_stats(old_state),
        "health": {
            "status": "ok",
            # Preserve error metadata until a meaningful state write.
            "last_error_alert_at": old_health.get("last_error_alert_at"),
            "last_block_kind": None,
            "last_block_status_code": None,
        },
    }

    has_baseline = bool(old_state.get("targets")) and bool(old_shop_fp)
    logic_migration = old_state.get("alert_logic_version") != ALERT_LOGIC_VERSION

    print(f"Parsed stock: {len(stock)} | sell: {len(sell)}")
    print(f"Read attempts used: {attempts}")
    print(f"Has v6.5.0 baseline: {has_baseline}")
    print(f"Alert rules self-test: PASS ({self_test['passed_classes']}/{self_test['total_classes']})")
    print(f"Current wanted conditions: {len(current_events)}")
    print(f"Alert logic migration required: {logic_migration}")
    print(
        "Shop cycles: "
        + " | ".join(
            f"{shop}#{current_shop_cycles.get(shop, {}).get('id')}"
            + (" NEW" if current_shop_cycles.get(shop, {}).get("changed") else "")
            for shop in SHOP_CYCLE_NAMES
        )
    )
    print(f"Trigger: event={EVENT_NAME or 'unknown'} source={TRIGGER_SOURCE or 'manual/default'}")

    is_manual_run = (
        EVENT_NAME == "workflow_dispatch"
        and TRIGGER_SOURCE != "cloudflare"
    )
    is_automatic_run = not is_manual_run

    daily_stats = new_state["daily_stats"]

    if is_automatic_run:
        summary_sent = maybe_send_daily_summary(daily_stats)
        if summary_sent:
            print("Daily Summary sent for previous Thailand date")

        update_daily_occurrence_stats(
            daily_stats,
            stock,
            sell,
            current_snapshot,
            current_shop_cycles,
            current_shop_fp,
        )

    # Manual Run is a Health/Image/Current Alert test only.
    # It does NOT affect Daily Statistics.
    if is_manual_run:
        send_discord(
            format_health_message(
                stock,
                sell,
                current_snapshot,
                attempts,
                recovered=recovered,
                self_test=self_test,
                source_sync=result.get("source_sync"),
                shop_cycles=current_shop_cycles,
            )
        )

        send_image_self_test()
        print("Manual image self-test sent")

        if current_events:
            send_event_alerts(current_events, attempts)
            print(f"Manual run sent {len(current_events)} current wanted event(s)")
        else:
            print("Manual run: no current wanted event; health check only")

        smart_save_state(old_state, new_state, force=True)
        print("Manual Health Check + current alerts sent; v6.5.0 state handled")
        return

    # On first run or migration, alert currently-active targets instead of
    # silently swallowing them into baseline.
    if logic_migration or not has_baseline:
        if current_events:
            send_event_alerts(current_events, attempts)
            add_daily_alert_count(daily_stats, len(current_events))
            print(f"Bootstrap/migration sent {len(current_events)} current wanted event(s)")
        else:
            print("Bootstrap/migration: no current wanted event; silent")

        smart_save_state(old_state, new_state, force=True)
        return

    events = compare_target_events(
        old_state,
        current_snapshot,
        old_shop_fp,
        current_shop_fp,
        current_shop_cycles,
    )

    if recovered:
        send_discord(
            "🟢 **GAG2 Bot recovered**\n"
            f"อ่าน Stock {len(stock)} / Sell {len(sell)} ได้ปกติแล้ว\n"
            "Reliability Mode กลับมาเฝ้าต่อเรียบร้อย"
        )

    if events:
        send_event_alerts(events, attempts)
        add_daily_alert_count(daily_stats, len(events))
        print(f"Sent {len(events)} wanted event(s)")
    else:
        print("No new wanted event; silent")

    smart_save_state(old_state, new_state)


if __name__ == "__main__":
    main()
