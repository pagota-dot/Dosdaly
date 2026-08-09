import os
import re
import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait


WEATHER_URL = "https://www.gag2.gg/stock/weather"
STATE_PATH = Path("moon_state.json")

WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()

THAILAND_TZ = timezone(timedelta(hours=7))

MAX_READ_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 3

# FINAL-only mode: track a verified Moon once it enters this window, then keep
# the first reliable start prediction frozen until the one FINAL alert is sent.
FINAL_TRACK_THRESHOLD_SECONDS = 5 * 60

# Aim to notify this long before the predicted event start.
FINAL_TARGET_SECONDS = 45

# If a refreshed countdown after waiting is still above this,
# the schedule likely shifted; do not fire too early.
FINAL_MAX_ACCEPTABLE_SECONDS = 80

# Freeze the predicted start time from the FIRST reliable <=5m alert.
# Later GAG2 countdown changes must NOT move the final alert later.
ANCHOR_LOGIC_VERSION = "v7.0-final-only-round-ledger"
BOT_DISPLAY_VERSION = "v7.0.2"

# UI-only category color.  Every Moon FINAL uses one unmistakable moonlight
# border so it cannot be confused with Stock rarity colors or SELL ×2/×4.
# This value is never read by parsing, anchor, timing, duplicate, or ledger
# logic.
MOON_SYSTEM_COLOR = 0xD7D9FF
MOON_SYSTEM_BADGE = "🌙 MOON FINAL ALERT"

# Locked reference for the palettes already assigned to the Stock/Sell bot.
# Regression tests require the Moon border to stay outside this set.
NON_MOON_RESERVED_COLORS = frozenset(
    {
        0xA0A0A0,  # Stock Common
        0x3BA55D,  # Stock Uncommon
        0x3498DB,  # Stock Rare
        0x9B59B6,  # Stock Epic
        0xF1C40F,  # Stock Legendary
        0xE74C3C,  # Stock Mythic
        0xFF4FD8,  # Stock Super
        0x57F287,  # Stock unknown fallback
        0x00F5D4,  # SELL ×2
        0xFF6B00,  # SELL ×4
    }
)

# Persistent audit trail. This lives inside moon_state.json, so the workflow's
# existing state commit/retry protection also protects the ledger.
ROUND_LEDGER_RETENTION_SECONDS = 14 * 24 * 60 * 60
ROUND_LEDGER_MAX_ENTRIES = 200

# Match the same event even if GAG2's later prediction drifts.
# Same moon type can appear only ~10 minutes apart on GAG2.
# Keep matching much tighter so adjacent Gold/Rainbow/Mega slots never merge.
ANCHOR_MATCH_TOLERANCE_SECONDS = 4 * 60

# Multi-signal row verification.
ROW_CLOCK_TOLERANCE_MINUTES = 2
SNAPSHOT_VERIFY_DELAY_SECONDS = 4
HEALTH_WARNING_COOLDOWN_SECONDS = 60 * 60

# Grow a Garden 2 day/night cycle reference (GAG2.GG current guide):
# Day 7m30s -> Sunset 30s -> Night 2m = 10 minutes per full cycle.
GAME_DAY_SECONDS = 7 * 60 + 30
GAME_SUNSET_SECONDS = 30
GAME_NIGHT_SECONDS = 2 * 60
GAME_CYCLE_SECONDS = 10 * 60
GAME_CYCLE_MINUTES = GAME_CYCLE_SECONDS // 60

# We infer the current night-start minute phase dynamically from upcoming Moon
# rows instead of hard-coding a wall-clock phase. This survives a future global
# phase shift while still rejecting one bad/out-of-cycle row.
GAME_CYCLE_MIN_SAMPLES = 2
GAME_CYCLE_MIN_CONSENSUS_RATIO = 2 / 3

TARGET_MOONS = {
    "gold": {
        "label": "Gold Moon",
        "emoji": "🌕",
        "seed": "Golden Seed",
        "seed_emoji": "🌟",
        "color": MOON_SYSTEM_COLOR,
    },
    "rainbow": {
        "label": "Rainbow Moon",
        "emoji": "🌈",
        "seed": "Rainbow Seed",
        "seed_emoji": "🌈",
        "color": MOON_SYSTEM_COLOR,
    },
    "mega": {
        "label": "Mega Moon",
        "emoji": "🌙",
        "seed": "Mega Seed",
        "seed_emoji": "💠",
        "color": MOON_SYSTEM_COLOR,
    },
}


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def norm(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def key(value):
    return re.sub(r"[^a-z0-9]+", " ", norm(value).lower()).strip()


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
    driver = webdriver.Chrome(options=opts)
    try:
        driver.execute_cdp_cmd(
            "Emulation.setTimezoneOverride",
            {"timezoneId": "Asia/Bangkok"},
        )
    except Exception as exc:
        print(f"Timezone override warning: {exc}")
    return driver


def assert_not_blocked(text):
    low = norm(text).lower()
    markers = [
        "429 too many requests",
        "403 forbidden",
        "access denied",
        "verify you are human",
        "checking your browser",
        "just a moment",
        "captcha",
        "cf-chl",
    ]
    for marker in markers:
        if marker in low:
            raise RuntimeError(f"GAG2 blocked/challenge: {marker}")


def rendered_weather_text(driver):
    driver.get(WEATHER_URL)

    def ready(d):
        try:
            text = d.find_element("tag name", "body").text
        except Exception:
            return False
        low = text.lower()
        return len(text) > 100 and "upcoming moons" in low

    try:
        WebDriverWait(driver, 20, poll_frequency=1).until(ready)
    except Exception:
        pass

    time.sleep(3)
    text = driver.find_element("tag name", "body").text
    assert_not_blocked(text)
    return text


def canonical_moon_name(text):
    k = key(text)
    if k in {"goldmoon", "gold moon"}:
        return "gold"
    if k in {"rainbowmoon", "rainbow moon"}:
        return "rainbow"
    if k in {"megamoon", "mega moon"}:
        return "mega"
    return None


def is_any_moon_row_name(text):
    """
    Row-boundary detector for target and non-target moons (Bloodmoon, etc.).
    This prevents a broken Gold/Rainbow/Mega row from borrowing the time or
    countdown belonging to the next Bloodmoon/other moon row.
    """
    compact = re.sub(r"[^a-z]", "", norm(text).lower())
    return bool(compact and compact.endswith("moon") and len(compact) <= 32)


def parse_duration_seconds(text):
    """
    Handles GAG2 countdown forms such as:
      33:26
      1h 13m
      2h 3m
      4m 23s
      45s
      now
    """
    s = norm(text).lower()

    if s == "now":
        return 0

    m = re.fullmatch(r"(\d{1,3}):([0-5]\d)", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    if not re.fullmatch(
        r"(?:\d+\s*h\s*)?(?:\d+\s*m\s*)?(?:\d+\s*s\s*)?",
        s,
    ):
        return None

    hours = re.search(r"(\d+)\s*h", s)
    mins = re.search(r"(\d+)\s*m", s)
    secs = re.search(r"(\d+)\s*s", s)

    if not any((hours, mins, secs)):
        return None

    return (
        (int(hours.group(1)) if hours else 0) * 3600
        + (int(mins.group(1)) if mins else 0) * 60
        + (int(secs.group(1)) if secs else 0)
    )


def parse_clock_text(text):
    s = norm(text)
    if re.fullmatch(r"\d{1,2}:\d{2}\s*(?:AM|PM)", s, re.I):
        return s.upper()
    return None


def clock_text_to_minutes(clock_text):
    value = parse_clock_text(clock_text)
    if not value:
        return None

    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*(AM|PM)", value, re.I)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = match.group(3).upper()

    if hour == 12:
        hour = 0
    if ampm == "PM":
        hour += 12

    return hour * 60 + minute


def minute_distance(a, b):
    diff = abs(int(a) - int(b)) % (24 * 60)
    return min(diff, 24 * 60 - diff)


def cycle_phase_minute(clock_minutes):
    """Return the minute phase within the 10-minute global game cycle."""
    if clock_minutes is None:
        return None
    return int(clock_minutes) % GAME_CYCLE_MINUTES


def infer_game_cycle_phase(clock_samples):
    """
    Infer the global Night/Moon-start phase from ALL upcoming Moon rows,
    including non-target rows such as Bloodmoon.

    Example: 19:28, 19:58, 20:18 all have minute phase 8 (mod 10).
    We do not hard-code phase=8; the majority phase is learned every scan.
    """
    phases = []
    for sample in clock_samples:
        minutes = sample.get("clock_minutes")
        if minutes is None:
            continue
        phases.append(cycle_phase_minute(minutes))

    if len(phases) < GAME_CYCLE_MIN_SAMPLES:
        return {
            "verified": False,
            "phase": None,
            "sample_count": len(phases),
            "consensus_count": 0,
            "consensus_ratio": 0.0,
            "reason": "not enough Moon clock samples for 10-minute cycle verification",
        }

    counts = {}
    for phase in phases:
        counts[phase] = counts.get(phase, 0) + 1

    best_phase, best_count = max(
        counts.items(),
        key=lambda item: (item[1], -item[0]),
    )
    ratio = best_count / len(phases)
    verified = (
        best_count >= GAME_CYCLE_MIN_SAMPLES
        and ratio >= GAME_CYCLE_MIN_CONSENSUS_RATIO
    )

    return {
        "verified": verified,
        "phase": best_phase if verified else None,
        "sample_count": len(phases),
        "consensus_count": best_count,
        "consensus_ratio": ratio,
        "phase_counts": counts,
        "reason": (
            f"10-minute Night/Moon grid verified: phase={best_phase} "
            f"samples={best_count}/{len(phases)}"
            if verified
            else f"no reliable 10-minute Moon-grid consensus: {counts}"
        ),
    }


def countdown_is_precise(countdown_text):
    s = norm(countdown_text).lower()
    return bool(re.fullmatch(r"\d{1,3}:[0-5]\d", s) or re.search(r"\d+\s*s", s))


def build_slot_identity(kind, clock_text, countdown, observed_epoch):
    """
    Primary identity = moon name + projected local date + displayed GAG2 clock.
    Countdown is used to determine the date and verify that the clock belongs
    to the same row/event. The exact alert second remains frozen from the first
    reliable countdown, because GAG2's displayed clock has minute precision.
    """
    projected_epoch = int(observed_epoch) + int(countdown)
    projected_th = datetime.fromtimestamp(
        projected_epoch,
        tz=timezone.utc,
    ).astimezone(THAILAND_TZ)

    displayed_minutes = clock_text_to_minutes(clock_text)
    projected_minutes = projected_th.hour * 60 + projected_th.minute

    if displayed_minutes is not None:
        clock_diff = minute_distance(displayed_minutes, projected_minutes)
        clock_consistent = clock_diff <= ROW_CLOCK_TOLERANCE_MINUTES

        display_hour, display_minute = divmod(displayed_minutes, 60)
        slot_clock = f"{display_hour:02d}:{display_minute:02d}"
        slot_date = projected_th.strftime("%Y-%m-%d")
        slot_id = f"{kind}|{slot_date}|{slot_clock}"
        quality = "NAME_CLOCK_COUNTDOWN" if clock_consistent else "CLOCK_MISMATCH"
    else:
        clock_diff = None
        clock_consistent = False
        # Fallback keeps the scanner alive if GAG2 temporarily omits the
        # absolute clock, but this is lower confidence and is logged clearly.
        fallback_minute = int((projected_epoch + 30) // 60)
        slot_id = f"{kind}|fallback|{fallback_minute}"
        quality = "COUNTDOWN_ONLY"

    return {
        "slot_id": slot_id,
        "projected_epoch": projected_epoch,
        "clock_consistent": clock_consistent,
        "clock_diff_minutes": clock_diff,
        "quality": quality,
    }


def parse_weather_page(text, observed_epoch=None):
    observed_epoch = int(observed_epoch or utc_now().timestamp())
    lines = [norm(x) for x in (text or "").splitlines() if norm(x)]

    upcoming_idx = next(
        (i for i, line in enumerate(lines) if key(line) == "upcoming moons"),
        None,
    )

    active = None
    if upcoming_idx is not None:
        top = lines[:upcoming_idx]
        if not any("no active weather" in key(x) for x in top):
            for line in reversed(top[-15:]):
                kind = canonical_moon_name(line)
                if kind:
                    active = kind
                    break

    upcoming = []
    parse_errors = []

    if upcoming_idx is None:
        return {
            "active": active,
            "upcoming": upcoming,
            "parse_errors": ["Upcoming moons section not found"],
            "game_cycle": {
                "verified": False,
                "reason": "Upcoming moons section not found",
            },
        }

    recent_idx = next(
        (
            i for i in range(upcoming_idx + 1, len(lines))
            if key(lines[i]) == "recently seen"
        ),
        len(lines),
    )

    section = lines[upcoming_idx + 1:recent_idx]

    all_moon_positions = [
        i for i, line in enumerate(section)
        if is_any_moon_row_name(line)
    ]

    # First pass: collect absolute clock samples from every Moon row, including
    # Bloodmoon and other non-target moons. All Moon starts should lie on the
    # same 10-minute Night-start grid.
    cycle_clock_samples = []
    row_bounds = []

    for position_index, i in enumerate(all_moon_positions):
        next_i = (
            all_moon_positions[position_index + 1]
            if position_index + 1 < len(all_moon_positions)
            else len(section)
        )
        row_lines = section[i + 1:next_i]
        clock_text = next(
            (
                parse_clock_text(candidate)
                for candidate in row_lines
                if parse_clock_text(candidate)
            ),
            None,
        )
        clock_minutes = clock_text_to_minutes(clock_text) if clock_text else None

        row_bounds.append(
            {
                "index": i,
                "next_index": next_i,
                "row_name": section[i],
                "row_lines": row_lines,
                "clock_text": clock_text,
                "clock_minutes": clock_minutes,
            }
        )

        if clock_minutes is not None:
            cycle_clock_samples.append(
                {
                    "row_name": section[i],
                    "clock_text": clock_text,
                    "clock_minutes": clock_minutes,
                }
            )

    game_cycle = infer_game_cycle_phase(cycle_clock_samples)

    for row in row_bounds:
        line = row["row_name"]
        kind = canonical_moon_name(line)

        # Non-target moons are used as cycle anchors/row boundaries only.
        if not kind:
            continue

        row_lines = row["row_lines"]
        clock_text = row["clock_text"]
        displayed_minutes = row["clock_minutes"]

        countdown_text = None
        countdown = None
        for candidate in row_lines:
            value = parse_duration_seconds(candidate)
            if value is not None:
                countdown_text = candidate
                countdown = value
                break

        if countdown is None:
            parse_errors.append(f"{kind}: countdown missing near row '{line}'")
            continue

        identity = build_slot_identity(
            kind,
            clock_text,
            countdown,
            observed_epoch,
        )

        # Signal 1+2+3: name + displayed clock + countdown must agree.
        if clock_text and not identity["clock_consistent"]:
            parse_errors.append(
                f"{kind}: clock/countdown mismatch "
                f"clock={clock_text} countdown={countdown_text} "
                f"diff={identity['clock_diff_minutes']}m"
            )
            continue

        # Game-cycle validation: Night/Moon starts repeat every 10 minutes.
        cycle_verified = False
        cycle_reason = game_cycle.get("reason", "cycle verification unavailable")
        event_phase = cycle_phase_minute(displayed_minutes)

        if game_cycle.get("verified") and displayed_minutes is not None:
            expected_phase = game_cycle.get("phase")
            cycle_verified = event_phase == expected_phase

            if not cycle_verified:
                parse_errors.append(
                    f"{kind}: off 10-minute Night/Moon grid "
                    f"clock={clock_text} phase={event_phase} "
                    f"expected_phase={expected_phase}"
                )
                # A row that contradicts the verified game cycle is rejected,
                # rather than allowed to trigger a Discord alert.
                continue

            cycle_reason = (
                f"clock phase {event_phase} matches verified "
                f"10-minute Night/Moon grid"
            )
        elif displayed_minutes is None:
            cycle_reason = "no absolute clock; game-cycle check unavailable"

        upcoming.append(
            {
                "kind": kind,
                "remaining": int(countdown),
                "countdown_text": countdown_text,
                "countdown_precise": countdown_is_precise(countdown_text),
                "clock_text": clock_text,
                "event_epoch": int(identity["projected_epoch"]),
                "event_key": identity["slot_id"],
                "slot_id": identity["slot_id"],
                "row_quality": identity["quality"],
                "clock_diff_minutes": identity["clock_diff_minutes"],
                "observed_epoch": observed_epoch,
                "game_cycle_verified": bool(cycle_verified),
                "game_cycle_phase": event_phase,
                "game_cycle_reason": cycle_reason,
            }
        )

    upcoming.sort(key=lambda x: x["remaining"])
    return {
        "active": active,
        "upcoming": upcoming,
        "parse_errors": parse_errors,
        "game_cycle": game_cycle,
        "game_cycle_samples": cycle_clock_samples,
    }

def verify_snapshots(first, second, elapsed_seconds):
    """
    Match by exact slot_id (name + date + displayed clock), then verify that
    the countdown moves in a physically plausible direction between reads.
    """
    first_map = {e.get("slot_id"): e for e in first.get("upcoming", [])}
    verified = []
    unverified = []

    for current in second.get("upcoming", []):
        slot_id = current.get("slot_id")
        previous = first_map.get(slot_id)

        if previous is None:
            candidate = dict(current)
            candidate["snapshot_verified"] = False
            candidate["snapshot_reason"] = "slot only appeared in second snapshot"
            unverified.append(candidate)
            continue

        before = int(previous.get("remaining", 0))
        after = int(current.get("remaining", 0))
        drop = before - after

        precise = bool(previous.get("countdown_precise") or current.get("countdown_precise"))
        if precise:
            plausible = -2 <= drop <= int(elapsed_seconds) + 8
        else:
            # Long countdowns may be rounded to whole minutes.
            plausible = -5 <= drop <= 65

        candidate = dict(current)
        candidate["snapshot_drop_seconds"] = drop
        candidate["snapshot_verified"] = bool(plausible)
        candidate["snapshot_reason"] = (
            "name+clock+countdown stable across 2 snapshots"
            if plausible
            else f"countdown changed implausibly: {before}->{after}"
        )

        if plausible:
            verified.append(candidate)
        else:
            unverified.append(candidate)

    verified.sort(key=lambda x: x["remaining"])
    unverified.sort(key=lambda x: x["remaining"])

    return {
        "active": second.get("active"),
        "upcoming": verified,
        "unverified": unverified,
        "parse_errors": list(first.get("parse_errors", [])) + list(second.get("parse_errors", [])),
        "snapshot_verified_count": len(verified),
        "snapshot_unverified_count": len(unverified),
        "game_cycle": second.get("game_cycle", {}),
        "game_cycle_samples": second.get("game_cycle_samples", []),
    }


def load_state():
    if not STATE_PATH.exists():
        return {
            "version": 3,
            "logic_version": ANCHOR_LOGIC_VERSION,
            "notification_mode": "final-only",
            "events": {},
            "round_ledger": {},
            "metrics": {},
            "health": {"last_warning_epoch": 0},
        }

    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}

    if not isinstance(raw, dict):
        raw = {}

    raw["version"] = 3
    raw["notification_mode"] = "final-only"
    raw.setdefault("events", {})
    raw.setdefault("round_ledger", {})
    raw.setdefault("metrics", {})
    raw.setdefault("health", {"last_warning_epoch": 0})

    if not isinstance(raw["events"], dict):
        raw["events"] = {}
    if not isinstance(raw["round_ledger"], dict):
        raw["round_ledger"] = {}

    # This key belonged to the removed "Event started" fallback. Keeping it
    # would be misleading in FINAL-only mode, so discard it during migration.
    raw.pop("last_final_by_kind", None)

    # One-time migration from the older moving-countdown logic.
    # Clear only Moon event-cycle flags; keep the file itself and other state.
    if raw.get("logic_version") != ANCHOR_LOGIC_VERSION:
        raw["events"] = {}
        raw["logic_version"] = ANCHOR_LOGIC_VERSION
        raw["migrated_at"] = iso_now()

    return raw


def save_state(state):
    state["updated_at"] = iso_now()
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def prune_state(state):
    now = int(utc_now().timestamp())
    events = state.setdefault("events", {})

    for event_key in list(events):
        info = events.get(event_key) or {}
        event_epoch = int(info.get("anchor_epoch", info.get("event_epoch", 0)) or 0)

        # Keep only a small useful window around events.
        if event_epoch and (
            event_epoch < now - 6 * 3600
            or event_epoch > now + 2 * 86400
        ):
            events.pop(event_key, None)

    ledger = state.setdefault("round_ledger", {})
    for round_id in list(ledger):
        record = ledger.get(round_id) or {}
        anchor_epoch = int(record.get("anchor_epoch", 0) or 0)
        if anchor_epoch and anchor_epoch < now - ROUND_LEDGER_RETENTION_SECONDS:
            ledger.pop(round_id, None)

    if len(ledger) > ROUND_LEDGER_MAX_ENTRIES:
        oldest_first = sorted(
            ledger,
            key=lambda rid: int((ledger.get(rid) or {}).get("anchor_epoch", 0) or 0),
        )
        for round_id in oldest_first[:-ROUND_LEDGER_MAX_ENTRIES]:
            ledger.pop(round_id, None)

    rebuild_round_metrics(state)


def iso_at(epoch):
    return datetime.fromtimestamp(
        int(epoch),
        tz=timezone.utc,
    ).isoformat()


def build_round_id(kind, anchor_epoch):
    local = datetime.fromtimestamp(
        int(anchor_epoch),
        tz=timezone.utc,
    ).astimezone(THAILAND_TZ)
    return f"MOON-{str(kind).upper()}-{local.strftime('%Y%m%d-%H%M%S')}"


def verification_details(event):
    signals = ["Name", "Time", "Countdown"]
    if event.get("snapshot_verified"):
        signals.append("2 Snapshots")
    if event.get("game_cycle_verified"):
        signals.append("10m Cycle")

    return {
        "score": len(signals),
        "maximum": 5,
        "signals": signals,
        "text": " + ".join(signals),
    }


def append_ledger_timeline(record, action, epoch, **details):
    timeline = record.setdefault("timeline", [])
    if any(item.get("action") == action for item in timeline):
        return

    entry = {
        "action": action,
        "epoch": int(epoch),
        "at": iso_at(epoch),
    }
    entry.update(details)
    timeline.append(entry)


def ensure_round_ledger(state, event_state, event, anchor_epoch, seen_epoch):
    round_id = event_state.get("round_id") or build_round_id(
        event.get("kind"),
        anchor_epoch,
    )
    event_state["round_id"] = round_id

    ledger = state.setdefault("round_ledger", {})
    record = ledger.setdefault(
        round_id,
        {
            "round_id": round_id,
            "kind": event.get("kind"),
            "label": TARGET_MOONS[event["kind"]]["label"],
            "status": "tracking",
            "slot_id": event.get("slot_id"),
            "clock_text": event.get("clock_text"),
            "anchor_epoch": int(anchor_epoch),
            "event_time_th": event_time_th(anchor_epoch),
            "first_seen_epoch": int(seen_epoch),
            "first_seen_at": iso_at(seen_epoch),
            "detection_lead_seconds": max(0, int(anchor_epoch) - int(seen_epoch)),
            "final_target_epoch": int(anchor_epoch) - FINAL_TARGET_SECONDS,
            "final_target_at": iso_at(int(anchor_epoch) - FINAL_TARGET_SECONDS),
            "verification": verification_details(event),
            "timeline": [],
        },
    )

    append_ledger_timeline(
        record,
        "detected",
        seen_epoch,
        lead_seconds=max(0, int(anchor_epoch) - int(seen_epoch)),
    )
    return round_id, record


def mark_round_missed(state, event_state, reason, missed_epoch):
    round_id = event_state.get("round_id")
    record = (state.setdefault("round_ledger", {})).get(round_id)
    if not record:
        return

    record["status"] = "final_missed"
    record["miss_reason"] = reason
    record["missed_epoch"] = int(missed_epoch)
    record["missed_at"] = iso_at(missed_epoch)
    append_ledger_timeline(record, "final_missed", missed_epoch, reason=reason)


def rebuild_round_metrics(state):
    records = list((state.get("round_ledger") or {}).values())
    sent = [r for r in records if r.get("status") == "final_sent"]
    missed = [r for r in records if r.get("status") == "final_missed"]
    offsets = [
        int(r["final_target_delay_seconds"])
        for r in sent
        if r.get("final_target_delay_seconds") is not None
    ]

    state["metrics"] = {
        "tracked_rounds": len(records),
        "final_sent": len(sent),
        "final_missed": len(missed),
        "average_final_target_delay_seconds": (
            round(sum(offsets) / len(offsets), 2) if offsets else None
        ),
        "maximum_final_target_delay_seconds": max(offsets) if offsets else None,
        "updated_at": iso_now(),
    }


def send_discord(content, embeds=None):
    if not WEBHOOK:
        raise RuntimeError("Missing GitHub Actions secret: DISCORD_WEBHOOK")

    payload = {
        "content": content,
        "allowed_mentions": {"parse": []},
    }
    if embeds:
        payload["embeds"] = embeds

    request_epoch = int(utc_now().timestamp())
    request_started = time.monotonic()
    response = requests.post(WEBHOOK, json=payload, timeout=20)
    delivery_ms = int((time.monotonic() - request_started) * 1000)
    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord webhook failed {response.status_code}: "
            f"{response.text[:300]}"
        )

    return {
        "request_epoch": request_epoch,
        "completed_epoch": int(utc_now().timestamp()),
        "delivery_ms": delivery_ms,
    }


def format_seconds(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} วินาที"

    mins, sec = divmod(seconds, 60)
    if mins < 60:
        if sec:
            return f"{mins} นาที {sec} วินาที"
        return f"{mins} นาที"

    hours, mins = divmod(mins, 60)
    return f"{hours} ชม. {mins} นาที"


def event_time_th(event_epoch):
    dt = datetime.fromtimestamp(event_epoch, tz=timezone.utc).astimezone(
        THAILAND_TZ
    )
    return dt.strftime("%H:%M:%S")


def event_embed(event, level="final", remaining=None):
    if level != "final":
        raise ValueError("Moon notification mode is FINAL-only")

    meta = TARGET_MOONS[event["kind"]]
    event_epoch = int(event["event_epoch"])
    sent_epoch = int(event.get("final_sent_epoch") or utc_now().timestamp())
    first_seen_epoch = int(event.get("first_seen_epoch") or sent_epoch)
    round_id = event.get("round_id") or build_round_id(
        event["kind"],
        event_epoch,
    )

    if remaining is None:
        remaining = max(0, event_epoch - sent_epoch)

    verification = event.get("verification") or verification_details(event)
    target_epoch = event_epoch - FINAL_TARGET_SECONDS
    target_delay = sent_epoch - target_epoch
    if target_delay > 0:
        target_text = f"ช้ากว่าเป้า **{format_seconds(target_delay)}**"
    elif target_delay < 0:
        target_text = f"เร็วกว่าเป้า **{format_seconds(abs(target_delay))}**"
    else:
        target_text = "ตรงเป้า **45 วินาที**"

    title = f"⚠️ {meta['emoji']} {meta['label']} — ใกล้เริ่มแล้ว"
    description = (
        f"{meta['seed_emoji']} **{meta['seed']}** กำลังจะมีโอกาสเกิด\n"
        f"เหลือประมาณ **{format_seconds(remaining)}** ก่อน Night / Moon เริ่ม\n\n"
        "ระบบตั้งเป็น **FINAL เท่านั้น** — รอบนี้จะไม่มีข้อความเตือนแรกและไม่มีข้อความเริ่มแล้ว"
    )

    slot_clock = event.get("clock_text") or "ไม่แสดง"

    return {
        "author": {"name": MOON_SYSTEM_BADGE},
        "title": title,
        "description": description,
        "color": meta["color"],
        "fields": [
            {
                "name": "🌙 Moon เริ่ม",
                "value": (
                    f"**{event_time_th(event_epoch)} น.** เวลาไทย\n"
                    f"<t:{event_epoch}:R>"
                ),
                "inline": True,
            },
            {
                "name": "📨 ส่ง FINAL",
                "value": (
                    f"**{event_time_th(sent_epoch)} น.**\n"
                    f"ก่อนเริ่ม **{format_seconds(remaining)}**"
                ),
                "inline": True,
            },
            {
                "name": "🎯 ความตรงเวลา",
                "value": target_text,
                "inline": True,
            },
            {
                "name": "🔎 ตรวจพบครั้งแรก",
                "value": (
                    f"**{event_time_th(first_seen_epoch)} น.**\n"
                    f"ล่วงหน้า **{format_seconds(event_epoch - first_seen_epoch)}**"
                ),
                "inline": True,
            },
            {
                "name": "🧭 GAG2 Slot",
                "value": f"**{slot_clock}**",
                "inline": True,
            },
            {
                "name": "🆔 Round ID",
                "value": f"`{round_id}`",
                "inline": True,
            },
            {
                "name": "✅ หลักฐานยืนยัน",
                "value": (
                    f"**{verification['score']}/{verification['maximum']} signals** · "
                    f"{verification['text']}"
                ),
                "inline": False,
            },
        ],
        "footer": {
            "text": (
                f"GAG2 Moon Alert {BOT_DISPLAY_VERSION} · FINAL ONLY · "
                "Frozen Anchor + Round Ledger"
            )
        },
        "timestamp": iso_at(sent_epoch),
    }


def send_level(event, level, remaining=None):
    if level != "final":
        raise ValueError("Moon notification mode is FINAL-only")

    sent_epoch = int(utc_now().timestamp())
    outgoing = dict(event)
    outgoing["final_sent_epoch"] = sent_epoch
    result = send_discord("", [event_embed(outgoing, "final", remaining)])
    result["sent_epoch"] = sent_epoch
    return result


def read_weather():
    last_error = None

    for attempt in range(1, MAX_READ_ATTEMPTS + 1):
        driver = None
        try:
            driver = make_driver()

            first_epoch = int(utc_now().timestamp())
            first_text = rendered_weather_text(driver)
            first = parse_weather_page(first_text, first_epoch)

            time.sleep(SNAPSHOT_VERIFY_DELAY_SECONDS)

            second_epoch = int(utc_now().timestamp())
            second_text = driver.find_element("tag name", "body").text
            assert_not_blocked(second_text)
            second = parse_weather_page(second_text, second_epoch)

            parsed = verify_snapshots(
                first,
                second,
                max(1, second_epoch - first_epoch),
            )
            parsed["attempt"] = attempt

            # Parsing errors indicate at least one target row was visible but
            # could not be verified. Retry first. On the last attempt we may
            # still use other fully verified rows rather than throwing away
            # good Moon data because one distant row rendered badly.
            if parsed.get("parse_errors") and attempt < MAX_READ_ATTEMPTS:
                raise RuntimeError(
                    "Weather row verification errors: "
                    + " | ".join(parsed["parse_errors"][:4])
                )

            if parsed.get("parse_errors") and attempt == MAX_READ_ATTEMPTS:
                parsed["degraded"] = True

            # If a full row has all three signals but only one snapshot caught
            # it, accept it as a last-resort candidate after retries. During
            # normal runs, verified rows are always preferred.
            if not parsed.get("upcoming") and parsed.get("unverified"):
                strong = [
                    e for e in parsed["unverified"]
                    if e.get("clock_text")
                    and e.get("row_quality") == "NAME_CLOCK_COUNTDOWN"
                ]
                if strong and attempt == MAX_READ_ATTEMPTS:
                    for event in strong:
                        event["snapshot_verified"] = False
                        event["snapshot_reason"] = "single-snapshot fallback after retries"
                    parsed["upcoming"] = sorted(strong, key=lambda x: x["remaining"])
                    parsed["single_snapshot_fallback"] = True

            if (
                attempt == MAX_READ_ATTEMPTS
                and parsed.get("parse_errors")
                and not parsed.get("upcoming")
            ):
                raise RuntimeError(
                    "No verified target Moon rows after retries: "
                    + " | ".join(parsed["parse_errors"][:4])
                )

            return parsed

        except Exception as exc:
            last_error = exc
            print(
                f"Weather read attempt {attempt}/{MAX_READ_ATTEMPTS} failed: "
                f"{type(exc).__name__}: {str(exc)[:260]}"
            )
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

        if attempt < MAX_READ_ATTEMPTS:
            time.sleep(RETRY_WAIT_SECONDS)

    raise RuntimeError(
        f"Weather read failed after {MAX_READ_ATTEMPTS} attempts: {last_error}"
    )


def find_matching_event(parsed, original_event):
    candidates = [
        e for e in parsed.get("upcoming", [])
        if e.get("kind") == original_event.get("kind")
    ]
    if not candidates:
        return None

    original_epoch = int(original_event.get("event_epoch", 0) or 0)

    candidates.sort(
        key=lambda e: abs(int(e.get("event_epoch", 0)) - original_epoch)
    )

    best = candidates[0]

    # Don't treat a completely different later moon as the same event.
    if abs(int(best["event_epoch"]) - original_epoch) > 5 * 60:
        return None

    return best


def resolve_anchor_state(events_state, event):
    """
    Primary key is the exact GAG2 slot_id:
      moon kind + projected Thai date + displayed clock text.

    Only if GAG2 changes the displayed clock by a few minutes do we alias it
    to an already-frozen same-kind event. Adjacent slots ~10 minutes apart
    remain separate.
    """
    kind = event.get("kind")
    slot_id = event.get("slot_id") or event.get("event_key")
    predicted_epoch = int(event.get("event_epoch", 0) or 0)

    if slot_id in events_state:
        return slot_id, events_state[slot_id]

    best_key = None
    best_state = None
    best_diff = None

    for state_key, info in events_state.items():
        if not isinstance(info, dict):
            continue
        if info.get("kind") != kind:
            continue

        anchor_epoch = int(
            info.get("anchor_epoch", info.get("event_epoch", 0)) or 0
        )
        if not anchor_epoch:
            continue

        diff = abs(anchor_epoch - predicted_epoch)
        if diff <= ANCHOR_MATCH_TOLERANCE_SECONDS:
            if best_diff is None or diff < best_diff:
                best_key = state_key
                best_state = info
                best_diff = diff

    if best_state is not None:
        aliases = best_state.setdefault("slot_aliases", [])
        if slot_id and slot_id not in aliases:
            aliases.append(slot_id)
        return best_key, best_state

    state_key = slot_id
    info = events_state.setdefault(
        state_key,
        {
            "kind": kind,
            "slot_id": slot_id,
            "clock_text": event.get("clock_text"),
            "event_epoch": predicted_epoch,
            "anchor_epoch": 0,
            "final": False,
        },
    )
    return state_key, info


def frozen_event_for_embed(event, anchor_epoch):
    copied = dict(event)
    copied["event_epoch"] = int(anchor_epoch)
    copied["remaining"] = max(
        0,
        int(anchor_epoch) - int(utc_now().timestamp()),
    )
    return copied


def process_upcoming(state, parsed):
    now_epoch = int(utc_now().timestamp())
    events_state = state.setdefault("events", {})

    candidates = []

    for event in parsed.get("upcoming", []):
        remaining = int(event["remaining"])

        # Track only the nearest five-minute window. Tracking is silent;
        # Discord receives no READY/PREPARE message.
        if remaining > FINAL_TRACK_THRESHOLD_SECONDS:
            continue

        state_key, es = resolve_anchor_state(events_state, event)

        # Freeze the FIRST reliable prediction. Do not overwrite this later.
        anchor_epoch = int(es.get("anchor_epoch", 0) or 0)
        if not anchor_epoch:
            anchor_epoch = now_epoch + remaining
            es["anchor_epoch"] = anchor_epoch
            es["event_epoch"] = anchor_epoch
            es["first_seen_remaining"] = remaining
            es["first_seen_epoch"] = now_epoch
            es["first_seen_at"] = iso_at(now_epoch)
            es["slot_time_th"] = event_time_th(anchor_epoch)
            es["slot_id"] = event.get("slot_id")
            es["clock_text"] = event.get("clock_text")
            es["row_quality"] = event.get("row_quality")
            es["snapshot_verified"] = bool(event.get("snapshot_verified"))
            es["game_cycle_verified"] = bool(event.get("game_cycle_verified"))
            es["game_cycle_phase"] = event.get("game_cycle_phase")

            print(
                f"ANCHOR frozen: {event['kind']} "
                f"remaining={remaining}s anchor_epoch={anchor_epoch}"
            )

        first_seen_epoch = int(es.get("first_seen_epoch", now_epoch) or now_epoch)
        round_id, ledger_record = ensure_round_ledger(
            state,
            es,
            event,
            anchor_epoch,
            first_seen_epoch,
        )
        es["round_id"] = round_id
        es["final_target_epoch"] = anchor_epoch - FINAL_TARGET_SECONDS

        anchor_remaining = anchor_epoch - int(utc_now().timestamp())

        if not es.get("final"):
            candidates.append(
                {
                    "state_key": state_key,
                    "event": event,
                    "anchor_epoch": anchor_epoch,
                    "anchor_remaining": anchor_remaining,
                    "round_id": round_id,
                    "ledger_record": ledger_record,
                }
            )

    if not candidates:
        return

    # Nearest anchored event wins. This same workflow run is responsible
    # for the final alert, so we don't depend on the next 2-minute cron run.
    candidates.sort(key=lambda x: x["anchor_remaining"])
    candidate = candidates[0]

    state_key = candidate["state_key"]
    event = candidate["event"]
    anchor_epoch = int(candidate["anchor_epoch"])
    es = events_state[state_key]

    anchor_remaining = anchor_epoch - int(utc_now().timestamp())

    # If the frozen time has already passed, close the ledger as missed. FINAL
    # only means there is deliberately no "started" fallback message.
    if anchor_remaining <= 0:
        missed_epoch = int(utc_now().timestamp())
        es["final"] = True
        es["final_missed"] = True
        mark_round_missed(state, es, "frozen-anchor-passed", missed_epoch)
        rebuild_round_metrics(state)
        print(
            f"FROZEN anchor already passed for {event['kind']}; "
            "FINAL closed as missed (no active fallback)"
        )
        return

    wait_seconds = max(0, anchor_remaining - FINAL_TARGET_SECONDS)

    if wait_seconds:
        print(
            f"FROZEN FINAL armed: {event['kind']} "
            f"sleep={wait_seconds}s target=~{FINAL_TARGET_SECONDS}s"
        )
        time.sleep(wait_seconds)

    # IMPORTANT: Do NOT replace the frozen anchor with a later GAG2
    # countdown. This was the cause of the late-final-alert problem.
    actual_by_anchor = max(
        0,
        anchor_epoch - int(utc_now().timestamp()),
    )

    if actual_by_anchor <= 0:
        missed_epoch = int(utc_now().timestamp())
        es["final"] = True
        es["final_missed"] = True
        mark_round_missed(state, es, "final-wait-finished-after-anchor", missed_epoch)
        rebuild_round_metrics(state)
        print(
            f"FINAL missed frozen start for {event['kind']}; "
            "no active fallback in FINAL-only mode"
        )
        return

    # A system clock jump or interrupted wait must not generate an early FINAL.
    # Leave it armed so the next run can try again using the same frozen anchor.
    if actual_by_anchor > FINAL_MAX_ACCEPTABLE_SECONDS:
        print(
            f"FINAL postponed: {event['kind']} still has "
            f"{actual_by_anchor}s (> {FINAL_MAX_ACCEPTABLE_SECONDS}s)"
        )
        return

    final_event = frozen_event_for_embed(event, anchor_epoch)
    final_event["round_id"] = es["round_id"]
    final_event["first_seen_epoch"] = int(es.get("first_seen_epoch", now_epoch))
    final_event["verification"] = candidate["ledger_record"].get(
        "verification",
        verification_details(event),
    )
    send_result = send_level(final_event, "final", actual_by_anchor) or {}

    sent_epoch = int(send_result.get("sent_epoch") or utc_now().timestamp())
    final_lead_seconds = max(0, anchor_epoch - sent_epoch)
    target_delay_seconds = sent_epoch - (
        anchor_epoch - FINAL_TARGET_SECONDS
    )

    es["final"] = True
    es["final_sent_epoch"] = sent_epoch
    es["final_sent_at"] = iso_at(sent_epoch)
    es["final_remaining_by_anchor"] = final_lead_seconds
    es["final_target_delay_seconds"] = target_delay_seconds
    es["discord_delivery_ms"] = send_result.get("delivery_ms")
    es["event_epoch"] = anchor_epoch

    ledger_record = candidate["ledger_record"]
    ledger_record["status"] = "final_sent"
    ledger_record["final_sent_epoch"] = sent_epoch
    ledger_record["final_sent_at"] = iso_at(sent_epoch)
    ledger_record["final_lead_seconds"] = final_lead_seconds
    ledger_record["final_target_delay_seconds"] = target_delay_seconds
    ledger_record["discord_delivery_ms"] = send_result.get("delivery_ms")
    append_ledger_timeline(
        ledger_record,
        "final_sent",
        sent_epoch,
        lead_seconds=final_lead_seconds,
        target_delay_seconds=target_delay_seconds,
        discord_delivery_ms=send_result.get("delivery_ms"),
    )
    rebuild_round_metrics(state)

    print(
        f"FROZEN FINAL sent: {event['kind']} "
        f"round_id={es['round_id']} remaining={final_lead_seconds}s "
        f"target_delay={target_delay_seconds:+d}s "
        f"discord={send_result.get('delivery_ms')}ms"
    )


def maybe_send_scanner_warning(state, error_text):
    health = state.setdefault("health", {"last_warning_epoch": 0})
    now_epoch = int(utc_now().timestamp())
    last_warning = int(health.get("last_warning_epoch", 0) or 0)

    if now_epoch - last_warning < HEALTH_WARNING_COOLDOWN_SECONDS:
        return

    send_discord(
        "⚠️ **GAG2 Moon Scanner Warning**\n"
        "ระบบตรวจ **ชื่อ Moon + เวลาในแถว + Countdown + 2 Snapshots + รอบเกม 10 นาที** "
        "ไม่สามารถยืนยันข้อมูลได้ในรอบนี้ จึงไม่เดาเวลาแจ้งเตือน\n"
        f"`{str(error_text)[:350]}`\n"
        "ระบบจะลองใหม่อัตโนมัติรอบถัดไป"
    )
    health["last_warning_epoch"] = now_epoch
    health["last_warning_at"] = iso_now()



def main():
    if not WEBHOOK:
        raise RuntimeError("Missing GitHub Actions secret: DISCORD_WEBHOOK")

    state = load_state()
    prune_state(state)

    try:
        parsed = read_weather()
    except Exception as exc:
        print(f"Moon scanner verification failed: {type(exc).__name__}: {exc}")
        maybe_send_scanner_warning(state, exc)
        save_state(state)
        return

    state.setdefault("health", {})["last_success_at"] = iso_now()

    if parsed.get("degraded"):
        maybe_send_scanner_warning(
            state,
            "บาง Moon row ยืนยันไม่ผ่าน แต่ระบบยังใช้เฉพาะ row ที่ยืนยันครบ: "
            + " | ".join(parsed.get("parse_errors", [])[:3]),
        )

    cycle_info = parsed.get("game_cycle") or {}
    print(
        "Weather parsed: "
        f"active={parsed.get('active')} "
        f"upcoming_targets={len(parsed.get('upcoming', []))} "
        f"verified={parsed.get('snapshot_verified_count', 0)} "
        f"cycle_verified={cycle_info.get('verified')} "
        f"cycle_phase={cycle_info.get('phase')} "
        f"cycle_samples={cycle_info.get('consensus_count', 0)}/"
        f"{cycle_info.get('sample_count', 0)} "
        f"attempt={parsed.get('attempt')} logic={ANCHOR_LOGIC_VERSION} "
        "notification_mode=FINAL_ONLY"
    )

    for event in parsed.get("upcoming", []):
        print(
            "ROW VERIFIED "
            f"kind={event.get('kind')} "
            f"slot={event.get('slot_id')} "
            f"clock={event.get('clock_text')} "
            f"countdown={event.get('countdown_text')} "
            f"snapshot={event.get('snapshot_verified')} "
            f"cycle={event.get('game_cycle_verified')} "
            f"cycle_phase={event.get('game_cycle_phase')}"
        )

    process_upcoming(state, parsed)

    save_state(state)


if __name__ == "__main__":
    main()
