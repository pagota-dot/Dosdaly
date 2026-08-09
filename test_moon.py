import ast
import hashlib
import sys
import types
import importlib.util
from pathlib import Path
from datetime import datetime, timedelta, timezone

BOT_FILENAME = "moon_bot.py"

def install_optional_dependency_stubs():
    """
    Offline logic tests do not open Chrome/Selenium.
    Stub Requests/Selenium only when missing, so moon_bot.py can be imported
    on a clean machine for pure parsing tests without network access.
    """
    try:
        import requests  # noqa: F401
    except Exception:
        requests = types.ModuleType("requests")

        def no_network_post(*args, **kwargs):
            raise AssertionError("Offline test attempted a real HTTP request")

        requests.post = no_network_post
        sys.modules["requests"] = requests

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
print("GAG2 Moon Alert v7.0.2 FINAL-only - OFFLINE LOGIC TEST")
print("ไม่เปิดเว็บ / ไม่ยิง Discord / ไม่แก้ moon_state.json จริง")
print("=" * 72)

# ---------------------------------------------------------------------
# UI-only release guard: Anchor/FINAL behavior must not migrate.
# ---------------------------------------------------------------------
ok(
    "Display release is v7.0.2",
    bot.BOT_DISPLAY_VERSION == "v7.0.2",
    bot.BOT_DISPLAY_VERSION,
)
ok(
    "Anchor logic version remains unchanged",
    bot.ANCHOR_LOGIC_VERSION == "v7.0-final-only-round-ledger",
    bot.ANCHOR_LOGIC_VERSION,
)

LOCKED_CORE_HASHES = {
    "parse_weather_page": "0972b4aa5403892445e102a498c39f6fe12ba9337732c39805a27a5b6d78e0ad",
    "verify_snapshots": "878dcf8cbd1efb57e98e3b53a31cbd26c6cab379c896dae0cd0366513abdd84f",
    "ensure_round_ledger": "cdd4e8ef7713af172488cbaceaebce398d00315685c6a4faa5f8dd64bd481e1e",
    "mark_round_missed": "a0b22053e50189653a392ec547adf2a6a1a3cfc8b2778c05c1507516737e088c",
    "find_matching_event": "ba168d6a2a2ac2f16e55d257447f8202472fa21375992548e6080c87634d283b",
    "resolve_anchor_state": "97322ea45f25b1382d8e96c23aa1988b8de6d89a4bc3ce58e11b1c2faaa4f089",
    "frozen_event_for_embed": "ac985c38d258e38ea7e48d162a81c462f315d805a0e659077a1e4acb36ebebea",
    "process_upcoming": "21afcf1f0c1fe562aa5dfe2e2d61fd9600b011471eeb47ef70c4a96b3dc26885",
}
source_text = (Path(__file__).resolve().parent / BOT_FILENAME).read_text(
    encoding="utf-8"
)
actual_core_hashes = {}
for node in ast.parse(source_text).body:
    if isinstance(node, ast.FunctionDef) and node.name in LOCKED_CORE_HASHES:
        function_source = ast.get_source_segment(source_text, node)
        actual_core_hashes[node.name] = hashlib.sha256(
            function_source.encode("utf-8")
        ).hexdigest()

ok(
    "Moon parsing/FINAL core is byte-for-byte unchanged",
    actual_core_hashes == LOCKED_CORE_HASHES,
    str(actual_core_hashes),
)

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
# TEST 11: FINAL-only guard ต้องปฏิเสธทุกระดับแจ้งเตือนอื่น
# ---------------------------------------------------------------------
guard_event = {
    "kind": "gold",
    "event_epoch": t0 + 45,
}

embed_rejected = []
send_rejected = []
for stale_level in ("prepare", "ready", "active"):
    try:
        bot.event_embed(guard_event, stale_level, 45)
    except ValueError:
        embed_rejected.append(stale_level)

    try:
        bot.send_level(guard_event, stale_level, 45)
    except ValueError:
        send_rejected.append(stale_level)

ok(
    "Embed accepts FINAL only",
    embed_rejected == ["prepare", "ready", "active"],
    str(embed_rejected),
)
ok(
    "Sender accepts FINAL only",
    send_rejected == ["prepare", "ready", "active"],
    str(send_rejected),
)

# ---------------------------------------------------------------------
# TEST 12: รอบ 45 วินาทีต้องส่ง FINAL ครั้งเดียวและเขียน Round Ledger
# ---------------------------------------------------------------------
fixed_dt_final = datetime.fromtimestamp(t0, tz=timezone.utc)
original_utc_now = bot.utc_now
original_send_level = bot.send_level
original_sleep = bot.time.sleep
final_calls = []

try:
    bot.utc_now = lambda: fixed_dt_final

    def fake_final_send(event, level, remaining=None):
        final_calls.append((event, level, remaining))
        return {
            "sent_epoch": t0,
            "delivery_ms": 123,
        }

    bot.send_level = fake_final_send
    bot.time.sleep = lambda *args, **kwargs: None

    final_state = {
        "events": {},
        "round_ledger": {},
        "metrics": {},
        "health": {"last_warning_epoch": 0},
    }
    final_event = {
        "kind": "gold",
        "remaining": 45,
        "countdown_text": "45s",
        "countdown_precise": True,
        "clock_text": "5:25 AM",
        "event_epoch": t0 + 45,
        "event_key": "gold|2026-08-09|05:25",
        "slot_id": "gold|2026-08-09|05:25",
        "row_quality": "NAME_CLOCK_COUNTDOWN",
        "snapshot_verified": True,
        "game_cycle_verified": True,
        "game_cycle_phase": 5,
    }

    bot.process_upcoming(final_state, {"upcoming": [final_event]})
    bot.process_upcoming(final_state, {"upcoming": [final_event]})

    final_es = final_state["events"]["gold|2026-08-09|05:25"]
    final_round_id = final_es["round_id"]
    final_record = final_state["round_ledger"][final_round_id]

    ok(
        "Exactly one FINAL is sent",
        len(final_calls) == 1 and final_calls[0][1] == "final",
        str(final_calls),
    )
    ok(
        "Same round cannot send FINAL twice",
        len(final_calls) == 1 and final_es.get("final") is True,
        str(final_es),
    )
    ok(
        "Round Ledger stores final_sent status",
        final_record.get("status") == "final_sent",
        str(final_record),
    )
    ok(
        "FINAL target delay measured at zero",
        final_record.get("final_target_delay_seconds") == 0
        and final_record.get("final_lead_seconds") == 45,
        str(final_record),
    )
    ok(
        "Discord delivery time stored",
        final_record.get("discord_delivery_ms") == 123,
        str(final_record),
    )
    ok(
        "Metrics count one sent round",
        final_state["metrics"].get("final_sent") == 1
        and final_state["metrics"].get("final_missed") == 0,
        str(final_state["metrics"]),
    )
finally:
    bot.utc_now = original_utc_now
    bot.send_level = original_send_level
    bot.time.sleep = original_sleep

# ---------------------------------------------------------------------
# TEST 13: Embed ใหม่ต้องมี Round ID, เวลา, หลักฐาน และ FINAL ONLY
# ---------------------------------------------------------------------
embed_event = dict(final_event)
embed_event.update(
    {
        "round_id": "MOON-GOLD-20260809-052555",
        "first_seen_epoch": t0 - 200,
        "final_sent_epoch": t0,
    }
)
embed = bot.event_embed(embed_event, "final", 45)
field_names = {field["name"] for field in embed.get("fields", [])}

ok(
    "Discord Embed contains audit fields",
    {
        "🌙 Moon เริ่ม",
        "📨 ส่ง FINAL",
        "🎯 ความตรงเวลา",
        "🔎 ตรวจพบครั้งแรก",
        "🧭 GAG2 Slot",
        "🆔 Round ID",
        "✅ หลักฐานยืนยัน",
    }.issubset(field_names),
    str(field_names),
)
ok(
    "45-second Embed says ใกล้เริ่มแล้ว",
    embed["title"] == "⚠️ 🌕 Gold Moon — ใกล้เริ่มแล้ว"
    and "เข้าเกมตอนนี้" not in embed["title"]
    and "FINAL ONLY" in embed["footer"]["text"]
    and embed.get("color") == bot.MOON_SYSTEM_COLOR,
    str(embed),
)
ok(
    "Moon has its own border and category badge",
    bot.MOON_SYSTEM_COLOR not in bot.NON_MOON_RESERVED_COLORS
    and embed.get("author", {}).get("name") == "🌙 MOON FINAL ALERT"
    and {
        meta.get("color") for meta in bot.TARGET_MOONS.values()
    } == {bot.MOON_SYSTEM_COLOR},
    str(
        {
            "moon_color": bot.MOON_SYSTEM_COLOR,
            "reserved": sorted(bot.NON_MOON_RESERVED_COLORS),
            "author": embed.get("author"),
        }
    ),
)

# ---------------------------------------------------------------------
# TEST 14: ถ้าพบหลัง Frozen Anchor ต้องบันทึก missed และไม่แจ้งเริ่มแล้ว
# ---------------------------------------------------------------------
original_utc_now = bot.utc_now
original_send_level = bot.send_level
miss_calls = []

try:
    bot.utc_now = lambda: fixed_dt_final
    bot.send_level = lambda *args, **kwargs: miss_calls.append((args, kwargs))

    miss_state = {
        "events": {},
        "round_ledger": {},
        "metrics": {},
        "health": {"last_warning_epoch": 0},
    }
    missed_event = dict(final_event)
    missed_event.update(
        {
            "remaining": -5,
            "event_epoch": t0 - 5,
            "event_key": "gold|2026-08-09|05:24",
            "slot_id": "gold|2026-08-09|05:24",
        }
    )
    bot.process_upcoming(miss_state, {"upcoming": [missed_event]})
    miss_es = miss_state["events"]["gold|2026-08-09|05:24"]
    miss_record = miss_state["round_ledger"][miss_es["round_id"]]

    ok(
        "Missed FINAL sends no active fallback",
        miss_calls == [],
        str(miss_calls),
    )
    ok(
        "Missed FINAL is recorded in ledger",
        miss_record.get("status") == "final_missed"
        and miss_state["metrics"].get("final_missed") == 1,
        str(miss_record),
    )
finally:
    bot.utc_now = original_utc_now
    bot.send_level = original_send_level

# ---------------------------------------------------------------------
# TEST 15: active-only input ต้องเงียบ และ Ledger เก่าต้องถูก prune
# ---------------------------------------------------------------------
silent_state = {
    "events": {},
    "round_ledger": {},
    "metrics": {},
}
bot.process_upcoming(silent_state, {"active": "gold", "upcoming": []})
ok(
    "Active-only page produces no Moon alert",
    silent_state["events"] == {} and silent_state["round_ledger"] == {},
    str(silent_state),
)

original_utc_now = bot.utc_now
try:
    bot.utc_now = lambda: fixed_dt_final
    old_epoch = t0 - bot.ROUND_LEDGER_RETENTION_SECONDS - 1
    prune_state = {
        "events": {},
        "round_ledger": {
            "OLD": {
                "round_id": "OLD",
                "anchor_epoch": old_epoch,
                "status": "final_sent",
                "final_target_delay_seconds": 0,
            }
        },
    }
    bot.prune_state(prune_state)
    ok(
        "Expired Round Ledger entry is pruned",
        prune_state["round_ledger"] == {},
        str(prune_state),
    )
finally:
    bot.utc_now = original_utc_now

ok(
    "Round ID is deterministic",
    bot.build_round_id("gold", t0 + 290) == "MOON-GOLD-20260809-053000",
    bot.build_round_id("gold", t0 + 290),
)

print("\n" + "=" * 72)
TOTAL = PASS + FAIL
print(f"RESULT: {PASS}/{TOTAL} PASSED")
if FAIL == 0:
    print("✅ Logic tests ผ่านทั้งหมด")
    print("   ระบบอ่านชื่อ/เวลา/Countdown, แยก Moon row, แยกหลาย slot,")
    print("   ตรวจ 2 snapshots, Frozen Anchor, FINAL-only, Round Ledger,")
    print("   Latency metrics และ Discord Embed ผ่านชุดทดสอบนี้")
else:
    print(f"❌ มี {FAIL} test(s) ไม่ผ่าน - อย่าเพิ่งถือว่าระบบพร้อม")
print("=" * 72)
sys.exit(1 if FAIL else 0)
