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
PREPARE_THRESHOLD_SECONDS = 12 * 60
READY_THRESHOLD_SECONDS = 5 * 60
FINAL_ARM_THRESHOLD_SECONDS = 3 * 60

# Aim to notify this long before the predicted event start.
FINAL_TARGET_SECONDS = 45

# If a refreshed countdown after waiting is still above this,
# the schedule likely shifted; do not fire too early.
FINAL_MAX_ACCEPTABLE_SECONDS = 80

# Active-event fallback suppression window.
ACTIVE_RECENT_FINAL_SECONDS = 5 * 60

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
            "version": 1,
            "events": {},
            "last_final_by_kind": {},
        }

    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}

    if not isinstance(raw, dict):
        raw = {}

    raw.setdefault("version", 1)
    raw.setdefault("events", {})
    raw.setdefault("last_final_by_kind", {})
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
        event_epoch = int(info.get("event_epoch", 0) or 0)

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
            "text": "GAG2 Moon Alert · Gold/Rainbow only"
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


def process_upcoming(state, parsed):
    now_epoch = int(utc_now().timestamp())
    events_state = state.setdefault("events", {})

    final_candidates = []

    for event in parsed.get("upcoming", []):
        remaining = int(event["remaining"])

        # Ignore events too far away; we'll see them again on later 2-minute runs.
        if remaining > PREPARE_THRESHOLD_SECONDS:
            continue

        es = events_state.setdefault(
            event["event_key"],
            {
                "kind": event["kind"],
                "event_epoch": event["event_epoch"],
                "prepare": False,
                "ready": False,
                "final": False,
            },
        )

        # Keep predicted epoch fresh as countdown gets more precise.
        es["event_epoch"] = event["event_epoch"]

        if remaining <= FINAL_ARM_THRESHOLD_SECONDS:
            if not es.get("final"):
                final_candidates.append(event)
            continue

        if remaining <= READY_THRESHOLD_SECONDS:
            if not es.get("ready"):
                send_level(event, "ready", remaining)
                es["ready"] = True
                mark_earlier_levels_skipped(es, "ready")
                print(
                    f"READY alert sent: {event['kind']} remaining={remaining}s"
                )
            continue

        if not es.get("prepare"):
            send_level(event, "prepare", remaining)
            es["prepare"] = True
            print(
                f"PREPARE alert sent: {event['kind']} remaining={remaining}s"
            )

    # Only one final wait at a time: nearest target moon wins.
    if final_candidates:
        final_candidates.sort(key=lambda e: e["remaining"])
        event = final_candidates[0]
        es = events_state[event["event_key"]]

        remaining = int(event["remaining"])
        wait_seconds = max(0, remaining - FINAL_TARGET_SECONDS)

        if wait_seconds:
            print(
                f"Armed FINAL alert for {event['kind']}: "
                f"sleep {wait_seconds}s to target ~{FINAL_TARGET_SECONDS}s before start"
            )
            time.sleep(wait_seconds)

        # Refresh after the wait so we don't fire early if the source schedule changed.
        refreshed = read_weather()
        match = find_matching_event(refreshed, event)

        if match is not None:
            actual_remaining = int(match["remaining"])

            if actual_remaining <= FINAL_MAX_ACCEPTABLE_SECONDS:
                send_level(match, "final", actual_remaining)
                es["final"] = True
                es["final_sent_at"] = iso_now()
                es["event_epoch"] = match["event_epoch"]
                mark_earlier_levels_skipped(es, "final")
                state.setdefault("last_final_by_kind", {})[
                    event["kind"]
                ] = {
                    "sent_epoch": int(utc_now().timestamp()),
                    "event_epoch": match["event_epoch"],
                }
                print(
                    f"FINAL alert sent: {event['kind']} "
                    f"remaining={actual_remaining}s"
                )
            else:
                print(
                    f"FINAL postponed: refreshed remaining={actual_remaining}s "
                    f"> {FINAL_MAX_ACCEPTABLE_SECONDS}s"
                )
        else:
            # If the target disappeared from upcoming, it may already be active.
            active = refreshed.get("active")
            if active == event["kind"]:
                send_level(event, "active", 0)
                es["final"] = True
                es["active_fallback"] = True
                state.setdefault("last_final_by_kind", {})[
                    event["kind"]
                ] = {
                    "sent_epoch": int(utc_now().timestamp()),
                    "event_epoch": event["event_epoch"],
                }
                print(
                    f"ACTIVE fallback sent for {event['kind']} after final wait"
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
        f"attempt={parsed.get('attempt')}"
    )

    process_upcoming(state, parsed)

    # Re-read active state only if the initial page already shows a target active.
    # The final-wait path performs its own refreshed check.
    process_active_fallback(state, parsed)

    save_state(state)


if __name__ == "__main__":
    main()
