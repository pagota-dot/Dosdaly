import sys
import os
import types
import importlib.util
from pathlib import Path
from datetime import datetime, timedelta, timezone

BOT_FILENAME = (os.environ.get("MOON_BOT_FILE") or "moon_bot.py").strip()

def install_optional_dependency_stubs():
    """
    Offline logic tests do not open Chrome/Selenium.
    Stub Selenium only when it is not installed, so moon_bot.py can be
    imported on a clean Windows/Python machine for pure parsing tests.
    """
    try:
        import selenium  # noqa: F401
        return
    except Exception:
        pass

    selenium = types.ModuleType("selenium")
    webdriver = types.ModuleType("selenium.webdriver")
    chrome = types.ModuleType("selenium.webdriver.chrome")
    options = types.ModuleType("selenium.webdriver.chrome.options")
    support = types.ModuleType("selenium.webdriver.support")
    ui = types.ModuleType("selenium.webdriver.support.ui")

    class DummyOptions:
        def add_argument(self, *args, **kwargs):
            pass

    class DummyWebDriverWait:
        def __init__(self, *args, **kwargs):
            pass

    options.Options = DummyOptions
    ui.WebDriverWait = DummyWebDriverWait
    webdriver.Chrome = None

    selenium.webdriver = webdriver
    webdriver.chrome = chrome
    chrome.options = options
    webdriver.support = support
    support.ui = ui

    sys.modules["selenium"] = selenium
    sys.modules["selenium.webdriver"] = webdriver
    sys.modules["selenium.webdriver.chrome"] = chrome
    sys.modules["selenium.webdriver.chrome.options"] = options
    sys.modules["selenium.webdriver.support"] = support
    sys.modules["selenium.webdriver.support.ui"] = ui

def load_bot():
    here = Path(__file__).resolve().parent
    path = here / BOT_FILENAME
    if not path.exists():
        print(f"[ERROR] ไม่พบไฟล์ {BOT_FILENAME}")
        print("ให้นำ test_moon.py วางไว้โฟลเดอร์เดียวกับ moon_bot.py")
        input("\nกด Enter เพื่อปิด...")
        sys.exit(1)

    install_optional_dependency_stubs()
    spec = importlib.util.spec_from_file_location("moon_bot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

bot = load_bot()

PASS = 0
FAIL = 0

def ok(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"✅ PASS  {name}")
    else:
        FAIL += 1
        print(f"❌ FAIL  {name}")
        if detail:
            print("        " + detail)

def epoch_th(y, mo, d, h, mi, s):
    dt = datetime(y, mo, d, h, mi, s, tzinfo=bot.THAILAND_TZ)
    return int(dt.timestamp())

def parse(page, when_epoch):
    return bot.parse_weather_page(page, when_epoch)

print("=" * 72)
print("GAG2 Moon Alert v6.4 - OFFLINE LOGIC + HARD MAX-2 TEST")
print(f"Bot file under test: {BOT_FILENAME}")
print("ไม่เปิดเว็บ / ไม่ยิง Discord / ไม่แก้ moon_state.json จริง")
print("=" * 72)

# ---------------------------------------------------------------------
# TEST 1: เวลาปกติ
# ---------------------------------------------------------------------
t0 = epoch_th(2026, 8, 9, 5, 25, 10)
page = """
No active weather
Upcoming Moons
Gold Moon
5:30 AM
4m 50s
Rainbow Moon
5:40 AM
14m 50s
Recently Seen
"""
r = parse(page, t0)
gold = next((e for e in r["upcoming"] if e["kind"] == "gold"), None)
rainbow = next((e for e in r["upcoming"] if e["kind"] == "rainbow"), None)

ok(
    "Normal Gold time accepted",
    gold is not None and gold["slot_id"] == "gold|2026-08-09|05:30",
    f"result={gold}, errors={r['parse_errors']}"
)
ok(
    "Normal Rainbow time accepted",
    rainbow is not None and rainbow["slot_id"] == "rainbow|2026-08-09|05:40",
    f"result={rainbow}, errors={r['parse_errors']}"
)

# ---------------------------------------------------------------------
# TEST 2: เวลา/Countdown ไม่สัมพันธ์กัน ต้อง reject
# ---------------------------------------------------------------------
bad_page = """
No active weather
Upcoming Moons
Gold Moon
5:30 AM
9m 50s
Recently Seen
"""
r_bad = parse(bad_page, t0)
ok(
    "Wrong clock/countdown rejected",
    not any(e["kind"] == "gold" for e in r_bad["upcoming"])
    and any("clock/countdown mismatch" in x for x in r_bad["parse_errors"]),
    f"upcoming={r_bad['upcoming']}, errors={r_bad['parse_errors']}"
)

# ---------------------------------------------------------------------
# TEST 3: Moon row isolation - ห้าม Gold ไปยืมข้อมูล Bloodmoon
# ---------------------------------------------------------------------
row_page = """
No active weather
Upcoming Moons
Gold Moon
5:30 AM
4m 50s
Bloodmoon
5:40 AM
14m 50s
Rainbow Moon
5:50 AM
24m 50s
Recently Seen
"""
r_row = parse(row_page, t0)
gold2 = next((e for e in r_row["upcoming"] if e["kind"] == "gold"), None)
rainbow2 = next((e for e in r_row["upcoming"] if e["kind"] == "rainbow"), None)

ok(
    "Gold row isolated from Bloodmoon",
    gold2 is not None and gold2["clock_text"] == "5:30 AM" and gold2["remaining"] == 290,
    f"gold={gold2}, errors={r_row['parse_errors']}"
)
ok(
    "Rainbow keeps its own row",
    rainbow2 is not None and rainbow2["clock_text"] == "5:50 AM" and rainbow2["remaining"] == 1490,
    f"rainbow={rainbow2}, errors={r_row['parse_errors']}"
)

# ---------------------------------------------------------------------
# TEST 4: Moon ชนิดเดียวกันหลายรอบ ต้องได้คนละ slot
# ---------------------------------------------------------------------
multi_page = """
No active weather
Upcoming Moons
Gold Moon
5:30 AM
4m 50s
Gold Moon
5:40 AM
14m 50s
Gold Moon
6:10 AM
44m 50s
Recently Seen
"""
r_multi = parse(multi_page, t0)
gold_slots = [e["slot_id"] for e in r_multi["upcoming"] if e["kind"] == "gold"]
ok(
    "Multiple Gold slots stay separate",
    gold_slots == [
        "gold|2026-08-09|05:30",
        "gold|2026-08-09|05:40",
        "gold|2026-08-09|06:10",
    ],
    f"slots={gold_slots}, errors={r_multi['parse_errors']}"
)

# ---------------------------------------------------------------------
# TEST 5: Snapshot ปกติ 4 วินาที
# ---------------------------------------------------------------------
first = parse("""
Upcoming Moons
Gold Moon
5:30 AM
4m 50s
Recently Seen
""", t0)

second = parse("""
Upcoming Moons
Gold Moon
5:30 AM
4m 46s
Recently Seen
""", t0 + 4)

v = bot.verify_snapshots(first, second, 4)
vg = next((e for e in v["upcoming"] if e["kind"] == "gold"), None)
ok(
    "2-snapshot countdown verifies normally",
    vg is not None and vg.get("snapshot_verified") is True
    and vg.get("snapshot_drop_seconds") == 4,
    f"verified={v}"
)

# ---------------------------------------------------------------------
# TEST 6: Countdown กระโดดผิดธรรมชาติ ต้อง unverified
# ---------------------------------------------------------------------
second_bad = parse("""
Upcoming Moons
Gold Moon
5:30 AM
3m 40s
Recently Seen
""", t0 + 4)

v_bad = bot.verify_snapshots(first, second_bad, 4)
ok(
    "Implausible snapshot jump rejected",
    not any(e["kind"] == "gold" for e in v_bad["upcoming"])
    and any(e["kind"] == "gold" for e in v_bad["unverified"]),
    f"result={v_bad}"
)

# ---------------------------------------------------------------------
# TEST 7: เวลาเลยเที่ยงคืน - slot date ต้องเป็นวันใหม่
# ---------------------------------------------------------------------
late = epoch_th(2026, 8, 9, 23, 58, 30)
midnight_page = """
Upcoming Moons
Mega Moon
12:02 AM
3m 30s
Recently Seen
"""
r_mid = parse(midnight_page, late)
mega = next((e for e in r_mid["upcoming"] if e["kind"] == "mega"), None)
ok(
    "Midnight date rollover correct",
    mega is not None and mega["slot_id"] == "mega|2026-08-10|00:02",
    f"mega={mega}, errors={r_mid['parse_errors']}"
)

# ---------------------------------------------------------------------
# TEST 8: process_upcoming ต้อง Freeze Anchor และห้าม prediction ใหม่เขียนทับ
# ---------------------------------------------------------------------
fixed_epoch = t0
fixed_dt = datetime.fromtimestamp(fixed_epoch, tz=timezone.utc)

original_utc_now = bot.utc_now
original_send_level = bot.send_level
original_sleep = bot.time.sleep

try:
    bot.utc_now = lambda: fixed_dt
    bot.send_level = lambda *args, **kwargs: None
    bot.time.sleep = lambda *args, **kwargs: None

    frozen_state = {
        "events": {},
        "last_final_by_kind": {},
        "health": {"last_warning_epoch": 0},
    }

    p1_event = {
        "kind": "gold",
        "remaining": 290,
        "countdown_text": "4m 50s",
        "countdown_precise": True,
        "clock_text": "5:30 AM",
        "event_epoch": fixed_epoch + 290,
        "event_key": "gold|2026-08-09|05:30",
        "slot_id": "gold|2026-08-09|05:30",
        "row_quality": "NAME_CLOCK_COUNTDOWN",
        "snapshot_verified": True,
    }
    bot.process_upcoming(frozen_state, {"upcoming": [p1_event]})

    es = frozen_state["events"]["gold|2026-08-09|05:30"]
    first_anchor = int(es["anchor_epoch"])

    # Same GAG2 slot, but later prediction shifts by +20 seconds.
    p2_event = dict(p1_event)
    p2_event["remaining"] = 310
    p2_event["event_epoch"] = fixed_epoch + 310
    bot.process_upcoming(frozen_state, {"upcoming": [p2_event]})

    second_anchor = int(
        frozen_state["events"]["gold|2026-08-09|05:30"]["anchor_epoch"]
    )

    ok(
        "process_upcoming freezes first anchor",
        first_anchor == fixed_epoch + 290,
        f"anchor={first_anchor}, expected={fixed_epoch + 290}"
    )
    ok(
        "Later prediction cannot move frozen anchor",
        second_anchor == first_anchor,
        f"first={first_anchor}, second={second_anchor}"
    )
finally:
    bot.utc_now = original_utc_now
    bot.send_level = original_send_level
    bot.time.sleep = original_sleep

# ---------------------------------------------------------------------
# TEST 9: Adjacent Gold ~10 นาที ต้องไม่ merge
# ---------------------------------------------------------------------
events_state2 = {}
e1 = {
    "kind": "gold",
    "slot_id": "gold|2026-08-09|05:30",
    "event_key": "gold|2026-08-09|05:30",
    "event_epoch": t0 + 290,
    "clock_text": "5:30 AM",
}
k1b, s1b = bot.resolve_anchor_state(events_state2, e1)
s1b["anchor_epoch"] = t0 + 290
s1b["event_epoch"] = t0 + 290

e2 = {
    "kind": "gold",
    "slot_id": "gold|2026-08-09|05:40",
    "event_key": "gold|2026-08-09|05:40",
    "event_epoch": t0 + 890,
    "clock_text": "5:40 AM",
}
k2b, s2b = bot.resolve_anchor_state(events_state2, e2)

ok(
    "Adjacent Gold slots do not merge",
    k1b != k2b,
    f"k1={k1b}, k2={k2b}, states={events_state2}"
)

# ---------------------------------------------------------------------
# TEST 10: AM/PM conversion
# ---------------------------------------------------------------------
ok(
    "12:00 AM -> 00:00",
    bot.clock_text_to_minutes("12:00 AM") == 0,
    str(bot.clock_text_to_minutes("12:00 AM"))
)
ok(
    "12:00 PM -> 12:00",
    bot.clock_text_to_minutes("12:00 PM") == 12 * 60,
    str(bot.clock_text_to_minutes("12:00 PM"))
)
ok(
    "11:59 PM -> 23:59",
    bot.clock_text_to_minutes("11:59 PM") == 23 * 60 + 59,
    str(bot.clock_text_to_minutes("11:59 PM"))
)


# ---------------------------------------------------------------------
# TEST 11: Game cycle constants = Day 7:30 + Sunset 0:30 + Night 2:00
# ---------------------------------------------------------------------
ok(
    "Game cycle durations total 10 minutes",
    bot.GAME_DAY_SECONDS == 450
    and bot.GAME_SUNSET_SECONDS == 30
    and bot.GAME_NIGHT_SECONDS == 120
    and bot.GAME_CYCLE_SECONDS == 600,
    (
        f"day={getattr(bot, 'GAME_DAY_SECONDS', None)} "
        f"sunset={getattr(bot, 'GAME_SUNSET_SECONDS', None)} "
        f"night={getattr(bot, 'GAME_NIGHT_SECONDS', None)} "
        f"cycle={getattr(bot, 'GAME_CYCLE_SECONDS', None)}"
    )
)

# ---------------------------------------------------------------------
# TEST 12: Dynamic 10-minute Moon grid - phase is learned, not hard-coded
# ---------------------------------------------------------------------
phase8_page = """
No active weather
Upcoming Moons
Gold Moon
5:28 AM
2m 50s
Bloodmoon
5:38 AM
12m 50s
Rainbow Moon
5:48 AM
22m 50s
Recently Seen
"""
# 05:25:10 + 2:50 = 05:28:00
r_phase8 = parse(phase8_page, t0)
cycle8 = r_phase8.get("game_cycle") or {}
gold8 = next((e for e in r_phase8["upcoming"] if e["kind"] == "gold"), None)
rainbow8 = next((e for e in r_phase8["upcoming"] if e["kind"] == "rainbow"), None)

ok(
    "10-minute Moon grid consensus detected dynamically",
    cycle8.get("verified") is True
    and cycle8.get("phase") == 8
    and cycle8.get("consensus_count") == 3,
    f"game_cycle={cycle8}"
)
ok(
    "Target Moon rows pass verified 10-minute game cycle",
    gold8 is not None
    and rainbow8 is not None
    and gold8.get("game_cycle_verified") is True
    and rainbow8.get("game_cycle_verified") is True,
    f"gold={gold8}, rainbow={rainbow8}, errors={r_phase8['parse_errors']}"
)

# ---------------------------------------------------------------------
# TEST 13: One off-grid Moon row must be rejected when consensus is strong
# ---------------------------------------------------------------------
offgrid_page = """
No active weather
Upcoming Moons
Gold Moon
5:30 AM
4m 50s
Bloodmoon
5:40 AM
14m 50s
Rainbow Moon
5:50 AM
24m 50s
Mega Moon
6:01 AM
35m 50s
Recently Seen
"""
r_off = parse(offgrid_page, t0)
off_cycle = r_off.get("game_cycle") or {}
mega_off = next((e for e in r_off["upcoming"] if e["kind"] == "mega"), None)

ok(
    "Strong game-cycle consensus survives one bad row",
    off_cycle.get("verified") is True
    and off_cycle.get("phase") == 0
    and off_cycle.get("consensus_count") == 3
    and off_cycle.get("sample_count") == 4,
    f"game_cycle={off_cycle}"
)
ok(
    "Off-grid Moon row rejected",
    mega_off is None
    and any("off 10-minute Night/Moon grid" in x for x in r_off["parse_errors"]),
    f"upcoming={r_off['upcoming']}, errors={r_off['parse_errors']}"
)

# ---------------------------------------------------------------------
# TEST 14: Discord embed identifies Night/Moon start and cycle verification
# ---------------------------------------------------------------------
embed_event = {
    "kind": "gold",
    "event_epoch": t0 + 290,
    "clock_text": "5:30 AM",
    "snapshot_verified": True,
    "game_cycle_verified": True,
}
embed = bot.event_embed(embed_event, "ready", 290)
footer_text = ((embed.get("footer") or {}).get("text") or "")
desc_text = embed.get("description") or ""

ok(
    "Discord copy references Night / Moon start",
    "Night / Moon" in desc_text,
    f"description={desc_text}"
)
ok(
    "Discord footer shows 10m Game Cycle verification",
    "+10m Game Cycle" in footer_text,
    f"footer={footer_text}"
)


# ---------------------------------------------------------------------
# TEST 15: Late discovery (<30s) must NOT send a late Final
# ---------------------------------------------------------------------
late_epoch = t0
late_dt = datetime.fromtimestamp(late_epoch, tz=timezone.utc)
late_sent = []
orig_utc_now = bot.utc_now
orig_send_level = bot.send_level
orig_sleep = bot.time.sleep
try:
    bot.utc_now = lambda: late_dt
    bot.send_level = lambda event, level, remaining=None: late_sent.append((level, remaining))
    bot.time.sleep = lambda *args, **kwargs: None

    late_state = {
        "events": {},
        "last_final_by_kind": {},
        "health": {"last_warning_epoch": 0},
    }
    late_event = {
        "kind": "gold",
        "remaining": 14,
        "countdown_text": "14s",
        "countdown_precise": True,
        "clock_text": "5:25 AM",
        "event_epoch": late_epoch + 14,
        "event_key": "gold|2026-08-09|05:25",
        "slot_id": "gold|2026-08-09|05:25",
        "row_quality": "NAME_CLOCK_COUNTDOWN",
        "snapshot_verified": True,
        "game_cycle_verified": True,
        "game_cycle_phase": 5,
    }
    bot.process_upcoming(late_state, {"upcoming": [late_event]})
    ok(
        "Late <30s discovery sends no Ready/Final",
        late_sent == [],
        f"sent={late_sent}, state={late_state}"
    )
finally:
    bot.utc_now = orig_utc_now
    bot.send_level = orig_send_level
    bot.time.sleep = orig_sleep

# ---------------------------------------------------------------------
# TEST 16: Active Moon must NEVER create a third started alert
# ---------------------------------------------------------------------
active_sent = []
orig_send_level = bot.send_level
try:
    bot.send_level = lambda event, level, remaining=None: active_sent.append((level, remaining))
    active_state = {
        "events": {},
        "last_final_by_kind": {},
        "health": {"last_warning_epoch": 0},
    }
    bot.process_active_fallback(active_state, {"active": "gold"})
    ok(
        "Active fallback suppressed (hard max 2 alerts)",
        active_sent == [] and getattr(bot, "ACTIVE_FALLBACK_ENABLED", None) is False,
        f"sent={active_sent}, enabled={getattr(bot, 'ACTIVE_FALLBACK_ENABLED', None)}"
    )
finally:
    bot.send_level = orig_send_level


print("\n" + "=" * 72)
TOTAL = PASS + FAIL
print(f"RESULT: {PASS}/{TOTAL} PASSED")
if FAIL == 0:
    print("✅ Logic tests ผ่านทั้งหมด")
    print("   ระบบอ่านชื่อ/เวลา/Countdown, แยก Moon row, แยกหลาย slot,")
    print("   ตรวจ 2 snapshots, Frozen Anchor, Game Cycle 10 นาที และ Hard Max-2 ผ่านชุดทดสอบนี้")
else:
    print(f"❌ มี {FAIL} test(s) ไม่ผ่าน - อย่าเพิ่งถือว่าระบบพร้อม")
print("=" * 72)

input("\nกด Enter เพื่อปิด...")
