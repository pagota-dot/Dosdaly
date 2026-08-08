import sys
import types
import importlib.util
from pathlib import Path
from datetime import datetime, timedelta, timezone

BOT_FILENAME = "moon_bot(6).py"

def install_optional_dependency_stubs():
    """
    Offline logic tests do not open Chrome/Selenium.
    Stub Selenium only when it is not installed, so moon_bot(6).py can be
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
        print("ให้นำ test_moon.py วางไว้โฟลเดอร์เดียวกับ moon_bot(6).py")
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
print("GAG2 Moon Alert v6.1 - OFFLINE LOGIC TEST")
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

print("\n" + "=" * 72)
TOTAL = PASS + FAIL
print(f"RESULT: {PASS}/{TOTAL} PASSED")
if FAIL == 0:
    print("✅ Logic tests ผ่านทั้งหมด")
    print("   ระบบอ่านชื่อ/เวลา/Countdown, แยก Moon row, แยกหลาย slot,")
    print("   ตรวจ 2 snapshots และ Frozen Anchor ผ่านชุดทดสอบนี้")
else:
    print(f"❌ มี {FAIL} test(s) ไม่ผ่าน - อย่าเพิ่งถือว่าระบบพร้อม")
print("=" * 72)

input("\nกด Enter เพื่อปิด...")
