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

# Alert windows
PREPARE_THRESHOLD_SECONDS = 5 * 60
READY_THRESHOLD_SECONDS = 5 * 60
FINAL_ARM_THRESHOLD_SECONDS = 3 * 60

# Aim to notify this long before the predicted event start.
FINAL_TARGET_SECONDS = 45

# If a refreshed countdown after waiting is still above this,
# the schedule likely shifted; do not fire too early.
FINAL_MAX_ACCEPTABLE_SECONDS = 80

# Active-event fallback suppression window.
ACTIVE_RECENT_FINAL_SECONDS = 5 * 60

# Freeze the predicted start time from the FIRST reliable <=5m alert.
# Later GAG2 countdown changes must NOT move the final alert later.
ANCHOR_LOGIC_VERSION = "v5-frozen-first-countdown"

# If the first time we ever see the event is already very late,
# don't send two messages almost on top of each other.
FIRST_ALERT_MIN_REMAINING_SECONDS = 90

# Match the same event even if GAG2's later prediction drifts.
ANCHOR_MATCH_TOLERANCE_SECONDS = 10 * 60

TARGET_MOONS = {
    "gold": {
        "label": "Gold Moon",
        "emoji": "🌕",
        "seed": "Golden Seed",
        "seed_emoji": "🌟",
    },
    "rainbow": {
        "label": "Rainbow Moon",
        "emoji": "🌈",
        "seed": "Rainbow Seed",
        "seed_emoji": "🌈",
    },
    "mega": {
        "label": "Mega Moon",
        "emoji": "🌙",
        "seed": "Mega Seed",
        "seed_emoji": "💠",
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
    return webdriver.Chrome(options=opts)


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


def parse_weather_page(text):
    lines = [norm(x) for x in (text or "").splitlines() if norm(x)]

    # Active weather is above "Upcoming moons".
    upcoming_idx = next(
        (i for i, line in enumerate(lines) if key(line) == "upcoming moons"),
        None,
    )

    active = None
    if upcoming_idx is not None:
        top = lines[:upcoming_idx]
        if not any("no active weather" in key(x) for x in top):
            # Search the nearest target moon in the active section.
            for line in reversed(top[-15:]):
                kind = canonical_moon_name(line)
                if kind:
                    active = kind
                    break

    upcoming = []
    if upcoming_idx is None:
        return {"active": active, "upcoming": upcoming}

    recent_idx = next(
        (
            i for i in range(upcoming_idx + 1, len(lines))
            if key(lines[i]) == "recently seen"
        ),
        len(lines),
    )

    section = lines[upcoming_idx + 1:recent_idx]

    for i, line in enumerate(section):
        kind = canonical_moon_name(line)
        if not kind:
            continue

        countdown = None
        clock_text = None

        # GAG2 currently renders: Name -> absolute clock -> countdown.
        # Search a small window to tolerate layout changes.
        for candidate in section[i + 1:i + 6]:
            if clock_text is None:
                clock_text = parse_clock_text(candidate)

            if countdown is None:
                val = parse_duration_seconds(candidate)
                if val is not None:
                    countdown = val

        if countdown is None:
            continue

        event_epoch = int(utc_now().timestamp()) + int(countdown)

        # 5-minute bucket provides a stable identity even when long countdowns
        # are rendered only to whole minutes.
        event_bucket = int((event_epoch + 150) // 300)
        event_key = f"{kind}:{event_bucket}"

        upcoming.append(
            {
                "kind": kind,
                "remaining": int(countdown),
                "clock_text": clock_text,
                "event_epoch": event_epoch,
                "event_key": event_key,
            }
        )

    upcoming.sort(key=lambda x: x["remaining"])
    return {"active": active, "upcoming": upcoming}


def load_state():
    if not STATE_PATH.exists():
        return {
            "version": 2,
            "logic_version": ANCHOR_LOGIC_VERSION,
            "events": {},
            "last_final_by_kind": {},
        }

    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}

    if not isinstance(raw, dict):
        raw = {}

    raw.setdefault("version", 2)
    raw.setdefault("events", {})
    raw.setdefault("last_final_by_kind", {})

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


def send_discord(content, embeds=None):
    if not WEBHOOK:
        raise RuntimeError("Missing GitHub Actions secret: DISCORD_WEBHOOK")

    payload = {
        "content": content,
        "allowed_mentions": {"parse": []},
    }
    if embeds:
        payload["embeds"] = embeds

    response = requests.post(WEBHOOK, json=payload, timeout=20)
    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord webhook failed {response.status_code}: "
            f"{response.text[:300]}"
        )


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


def event_embed(event, level, remaining=None):
    meta = TARGET_MOONS[event["kind"]]
    event_epoch = int(event["event_epoch"])

    if remaining is None:
        remaining = max(0, event_epoch - int(utc_now().timestamp()))

    if level == "prepare":
        title = f"🔔 {meta['emoji']} {meta['label']} — เตรียมตัว"
        description = (
            f"⏳ เหลือประมาณ **{format_seconds(remaining)}**\n"
            f"🕐 คาดว่าเริ่มประมาณ **{event_time_th(event_epoch)} น.** เวลาไทย\n\n"
            f"{meta['seed_emoji']} มีโอกาสเกิด **{meta['seed']}**\n"
            "ยังไม่ต้องรีบ แต่เตรียมเข้าเกมไว้ได้เลย"
        )
    elif level == "ready":
        title = f"⚠️ {meta['emoji']} {meta['label']} — ใกล้เริ่มแล้ว"
        description = (
            f"⏳ เหลือประมาณ **{format_seconds(remaining)}**\n"
            f"🕐 คาดว่าเริ่มประมาณ **{event_time_th(event_epoch)} น.** เวลาไทย\n\n"
            f"{meta['seed_emoji']} เตรียมหา **{meta['seed']}**\n"
            "แนะนำเปิดเกมและเตรียมเข้าเซิร์ฟเวอร์ได้เลย"
        )
    elif level == "final":
        title = f"🚨 {meta['emoji']} {meta['label']} — เข้าเกมตอนนี้!"
        description = (
            f"⏳ เหลือประมาณ **{format_seconds(remaining)}**\n"
            f"🕐 คาดว่าเริ่มประมาณ **{event_time_th(event_epoch)} น.** เวลาไทย\n\n"
            f"{meta['seed_emoji']} **{meta['seed']}** กำลังจะมีโอกาสเกิด\n"
            "นี่คือการเตือนรอบสุดท้ายก่อน Event"
        )
    else:
        title = f"🚨 {meta['emoji']} {meta['label']} — เริ่มแล้ว!"
        description = (
            f"{meta['seed_emoji']} **{meta['seed']}** สามารถมีโอกาสเกิดได้ตอนนี้\n"
            "ระบบไม่ทันส่งเตือน ~45 วินาทีก่อนเริ่ม จึงแจ้งทันทีเมื่อพบว่า Event เริ่มแล้ว"
        )

    return {
        "title": title,
        "description": description,
        "footer": {
            "text": "GAG2 Moon Alert · Frozen 5m Anchor → ~45s · Gold/Rainbow/Mega"
        },
    }


def send_level(event, level, remaining=None):
    send_discord("", [event_embed(event, level, remaining)])


def read_weather():
    last_error = None

    for attempt in range(1, MAX_READ_ATTEMPTS + 1):
        driver = None
        try:
            driver = make_driver()
            text = rendered_weather_text(driver)
            parsed = parse_weather_page(text)
            parsed["attempt"] = attempt
            return parsed
        except Exception as exc:
            last_error = exc
            print(
                f"Weather read attempt {attempt}/{MAX_READ_ATTEMPTS} failed: "
                f"{type(exc).__name__}: {str(exc)[:180]}"
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


def mark_earlier_levels_skipped(event_state, level):
    if level == "ready":
        event_state.setdefault("prepare", True)
    elif level == "final":
        event_state.setdefault("prepare", True)
        event_state.setdefault("ready", True)



def resolve_anchor_state(events_state, event):
    """
    Reuse the same Moon state even when a later GAG2 read moves the
    predicted time / event_key. This prevents duplicate or delayed alerts.
    """
    kind = event.get("kind")
    predicted_epoch = int(event.get("event_epoch", 0) or 0)

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
        return best_key, best_state

    state_key = event["event_key"]
    info = events_state.setdefault(
        state_key,
        {
            "kind": kind,
            "event_epoch": predicted_epoch,
            "anchor_epoch": 0,
            "ready": False,
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

        # We care only about the nearest 5-minute window.
        if remaining > READY_THRESHOLD_SECONDS:
            continue

        state_key, es = resolve_anchor_state(events_state, event)

        # Freeze the FIRST reliable prediction. Do not overwrite this later.
        anchor_epoch = int(es.get("anchor_epoch", 0) or 0)
        if not anchor_epoch:
            anchor_epoch = now_epoch + remaining
            es["anchor_epoch"] = anchor_epoch
            es["event_epoch"] = anchor_epoch
            es["first_seen_remaining"] = remaining
            es["first_seen_at"] = iso_now()

            print(
                f"ANCHOR frozen: {event['kind']} "
                f"remaining={remaining}s anchor_epoch={anchor_epoch}"
            )

        anchor_remaining = anchor_epoch - int(utc_now().timestamp())

        # Alert #1: send once when there is enough useful lead time.
        # If first discovery is already <=90s, skip this alert so Discord
        # doesn't receive two messages back-to-back.
        if not es.get("ready"):
            if anchor_remaining > FIRST_ALERT_MIN_REMAINING_SECONDS:
                first_event = frozen_event_for_embed(event, anchor_epoch)
                send_level(first_event, "ready", anchor_remaining)
                es["ready"] = True
                es["prepare"] = True
                es["ready_sent_at"] = iso_now()

                print(
                    f"5-MIN alert sent from FROZEN anchor: "
                    f"{event['kind']} remaining={anchor_remaining}s"
                )
            else:
                es["ready"] = True
                es["prepare"] = True
                es["ready_skipped_late"] = True
                print(
                    f"5-MIN alert skipped (first seen late): "
                    f"{event['kind']} remaining={anchor_remaining}s"
                )

        if not es.get("final"):
            candidates.append(
                {
                    "state_key": state_key,
                    "event": event,
                    "anchor_epoch": anchor_epoch,
                    "anchor_remaining": anchor_remaining,
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

    # If the frozen time has already passed, don't invent a "45s" alert.
    # Active fallback below can still report a genuinely active event.
    if anchor_remaining <= 0:
        print(
            f"FROZEN anchor already passed for {event['kind']}; "
            "waiting for active fallback"
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
        print(
            f"FINAL missed frozen start for {event['kind']}; "
            "active fallback will handle it"
        )
        return

    final_event = frozen_event_for_embed(event, anchor_epoch)
    send_level(final_event, "final", actual_by_anchor)

    es["final"] = True
    es["final_sent_at"] = iso_now()
    es["final_remaining_by_anchor"] = actual_by_anchor
    es["event_epoch"] = anchor_epoch
    mark_earlier_levels_skipped(es, "final")

    state.setdefault("last_final_by_kind", {})[
        event["kind"]
    ] = {
        "sent_epoch": int(utc_now().timestamp()),
        "event_epoch": anchor_epoch,
    }

    print(
        f"FROZEN FINAL sent: {event['kind']} "
        f"remaining={actual_by_anchor}s"
    )


def process_active_fallback(state, parsed):
    active = parsed.get("active")
    if active not in TARGET_MOONS:
        return

    now_epoch = int(utc_now().timestamp())
    last = (state.get("last_final_by_kind") or {}).get(active) or {}
    last_sent = int(last.get("sent_epoch", 0) or 0)

    # If final alert was sent recently, don't send a redundant "started" message.
    if last_sent and now_epoch - last_sent <= ACTIVE_RECENT_FINAL_SECONDS:
        return

    pseudo_event = {
        "kind": active,
        "event_epoch": now_epoch,
        "event_key": f"active:{active}:{now_epoch // 300}",
        "remaining": 0,
    }

    send_level(pseudo_event, "active", 0)

    state.setdefault("last_final_by_kind", {})[active] = {
        "sent_epoch": now_epoch,
        "event_epoch": now_epoch,
    }

    print(f"ACTIVE fallback sent: {active}")


def main():
    if not WEBHOOK:
        raise RuntimeError("Missing GitHub Actions secret: DISCORD_WEBHOOK")

    state = load_state()
    prune_state(state)

    parsed = read_weather()

    print(
        "Weather parsed: "
        f"active={parsed.get('active')} "
        f"upcoming_targets={len(parsed.get('upcoming', []))} "
        f"attempt={parsed.get('attempt')} logic={ANCHOR_LOGIC_VERSION}"
    )

    process_upcoming(state, parsed)

    # Re-read active state only if the initial page already shows a target active.
    # The final-wait path performs its own refreshed check.
    process_active_fallback(state, parsed)

    save_state(state)


if __name__ == "__main__":
    main()
