import ast
import importlib.util
import re
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STOCK_FILE = ROOT / "bot.py"
MOON_FILE = ROOT / "moon_bot.py"


def install_offline_stubs():
    requests = types.ModuleType("requests")

    def blocked_network(*args, **kwargs):
        raise AssertionError("Cross-Bot UI Test attempted network access")

    requests.post = blocked_network
    sys.modules["requests"] = requests

    selenium = types.ModuleType("selenium")
    webdriver = types.ModuleType("selenium.webdriver")
    chrome = types.ModuleType("selenium.webdriver.chrome")
    options = types.ModuleType("selenium.webdriver.chrome.options")
    support = types.ModuleType("selenium.webdriver.support")
    ui = types.ModuleType("selenium.webdriver.support.ui")

    class DummyOptions:
        def add_argument(self, *args, **kwargs):
            pass

    class DummyWait:
        def __init__(self, *args, **kwargs):
            pass

    webdriver.Chrome = blocked_network
    options.Options = DummyOptions
    ui.WebDriverWait = DummyWait
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


def load_module(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def names_used_by_functions(source_text, function_names):
    used = set()
    for node in ast.parse(source_text).body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            used.update(
                child.id
                for child in ast.walk(node)
                if isinstance(child, ast.Name)
            )
    return used


PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"✅ PASS  {name}")
    else:
        FAIL += 1
        print(f"❌ FAIL  {name}")
        if detail:
            print("        " + detail)


print("=" * 72)
print("GAG2 Stock + Moon — CROSS-BOT UI / SAFETY TEST")
print("ไม่เปิด GAG2 / ไม่ส่ง Discord / ไม่อ่านหรือแก้ State จริง")
print("=" * 72)

required_paths = [
    ROOT / "bot.py",
    ROOT / "moon_bot.py",
    ROOT / "test_stock_cycle_guard.py",
    ROOT / "test_moon.py",
    ROOT / "test_cross_bot_ui.py",
    ROOT / ".github/workflows/stock_logic_test.yml",
    ROOT / ".github/workflows/gag2-moon.yml",
    ROOT / ".github/workflows/moon_logic_test.yml",
    ROOT / ".github/workflows/cross_bot_ui_test.yml",
]
check(
    "Combined package contains every canonical bot/test/workflow file",
    all(path.is_file() for path in required_paths),
    str([str(path.relative_to(ROOT)) for path in required_paths if not path.is_file()]),
)

canonical_moon_files = [
    ROOT / "moon_bot.py",
    ROOT / "test_moon.py",
    ROOT / ".github/workflows/gag2-moon.yml",
    ROOT / ".github/workflows/moon_logic_test.yml",
    ROOT / ".github/workflows/cross_bot_ui_test.yml",
]
stale_references = []
for path in canonical_moon_files:
    if not path.is_file():
        continue
    if re.search(r"moon_bot\([0-9]+\)\.py", path.read_text(encoding="utf-8")):
        stale_references.append(str(path.relative_to(ROOT)))
check(
    "No numbered legacy Moon filename is referenced",
    stale_references == [],
    str(stale_references),
)

install_offline_stubs()
stock = load_module("cross_stock_bot", STOCK_FILE)
moon = load_module("cross_moon_bot", MOON_FILE)

check(
    "Display versions are the combined safety release",
    stock.BOT_DISPLAY_VERSION == "6.5.11"
    and moon.BOT_DISPLAY_VERSION == "v7.0.4",
    f"Stock={stock.BOT_DISPLAY_VERSION} Moon={moon.BOT_DISPLAY_VERSION}",
)
check(
    "Alert and Frozen-Anchor logic versions remain unchanged",
    stock.ALERT_LOGIC_VERSION == "6.4.4-image-alert-v1"
    and moon.ANCHOR_LOGIC_VERSION == "v7.0-final-only-round-ledger",
    f"Stock={stock.ALERT_LOGIC_VERSION} Moon={moon.ANCHOR_LOGIC_VERSION}",
)

stock_colors = {
    style["color"] for style in stock.RARITY_UI_STYLES.values()
}
stock_colors.add(stock.UNKNOWN_RARITY_UI_STYLE["color"])
sell_colors = {
    style["color"] for style in stock.SELL_MULTIPLIER_UI_STYLES.values()
}
moon_colors = {moon.MOON_SYSTEM_COLOR}

check(
    "Stock rarity, Sell multiplier, and Moon palettes are pairwise disjoint",
    stock_colors.isdisjoint(sell_colors)
    and stock_colors.isdisjoint(moon_colors)
    and sell_colors.isdisjoint(moon_colors),
    f"Stock={stock_colors} Sell={sell_colors} Moon={moon_colors}",
)
check(
    "Every Gold/Rainbow/Mega alert uses the Moonlight category border",
    {meta.get("color") for meta in moon.TARGET_MOONS.values()}
    == {moon.MOON_SYSTEM_COLOR}
    and moon.MOON_SYSTEM_COLOR not in moon.NON_MOON_RESERVED_COLORS,
)

stock_event = {
    "kind": "stock",
    "target_key": "atlantic giant pumpkin",
    "label": "Atlantic Giant Pumpkin",
    "emoji": "🎃",
    "items": [
        {
            "name": "Atlantic Giant Pumpkin",
            "qty": 1,
            "rarity": "LEGENDARY",
            "type": "seed",
        }
    ],
    "reason": "รอบร้านใหม่",
}
stock_embed = stock.build_event_embed(stock_event, attempts=1)
check(
    "Stock card keeps rarity title and Legendary border",
    "LEGENDARY" in stock_embed.get("title", "")
    and stock_embed.get("color") == stock.RARITY_UI_STYLES["legendary"]["color"],
    str(stock_embed),
)

stock_daily_stats = {
    "days": {
        stock.thailand_date_str(): {
            "stock_occurrences": {"atlantic giant pumpkin": 6},
            "stock_seen_cycles": {},
            "stock_pieces": {"atlantic giant pumpkin": 8},
            "stock_cycle_quantities": {},
            "magic_mail": {},
            "magic_seen_cycles": {},
            "sell": {},
            "sell_seen_rotations": {},
            "alerts_sent": 6,
        }
    }
}
stock_daily_embed = stock.build_event_embed(
    stock_event,
    attempts=1,
    daily_stats=stock_daily_stats,
)
stock_daily_value = next(
    field["value"]
    for field in stock_daily_embed.get("fields", [])
    if field.get("name") == "📊 สถิติวันนี้"
)
check(
    "Stock UI separates today's occurrence count from total pieces",
    "ครั้งที่ **6**" in stock_daily_value
    and "รวมวันนี้ **8 ชิ้น**" in stock_daily_value,
    stock_daily_value,
)

wiki_conflict_event = {
    "kind": "stock",
    "target_key": "super syrup watering can",
    "label": "Super Syrup Watering Can",
    "emoji": "🪣",
    "items": [
        {
            "name": "Super Syrup Watering Can",
            "qty": 2,
            "rarity": "COMMON",
            "type": "gear",
        }
    ],
    "reason": "กลับเข้า Stock",
}
wiki_conflict_before = dict(wiki_conflict_event["items"][0])
wiki_conflict_embed = stock.build_event_embed(
    wiki_conflict_event,
    attempts=3,
)
wiki_status = next(
    field["value"]
    for field in wiki_conflict_embed.get("fields", [])
    if field.get("name") == "📌 สถานะ"
)
check(
    "Wiki name forces Watering Can SUPER UI even when live page says COMMON",
    "🌈 SUPER" in wiki_conflict_embed.get("title", "")
    and "COMMON" not in wiki_conflict_embed.get("title", "")
    and "SUPER" in wiki_status
    and "COMMON" not in wiki_status
    and wiki_conflict_embed.get("color")
    == stock.RARITY_UI_STYLES["super"]["color"]
    and wiki_conflict_event["items"][0] == wiki_conflict_before,
    str(wiki_conflict_embed),
)

sell_embeds = []
for multi in (2.0, 4.0):
    sell_embeds.append(
        stock.build_event_embed(
            {
                "kind": "sell",
                "target_key": "maple mushroom",
                "label": "Maple Mushroom",
                "emoji": "🍄",
                "multi": multi,
                "reason": f"Sell เปลี่ยนเป็น ×{int(multi)}",
            },
            attempts=1,
        )
    )
check(
    "Sell ×2/×4 cards keep multiplier-only borders",
    sell_embeds[0].get("color")
    == stock.SELL_MULTIPLIER_UI_STYLES[2]["color"]
    and sell_embeds[1].get("color")
    == stock.SELL_MULTIPLIER_UI_STYLES[4]["color"]
    and "SELL BOOST ×2" in sell_embeds[0].get("title", "")
    and "SELL BOOST ×4" in sell_embeds[1].get("title", ""),
    str(sell_embeds),
)

moon_event = {
    "kind": "gold",
    "event_epoch": 1786227955,
    "final_sent_epoch": 1786227910,
    "first_seen_epoch": 1786227665,
    "clock_text": "5:25 AM",
    "round_id": "MOON-GOLD-20260809-052555",
    "snapshot_verified": True,
    "game_cycle_verified": True,
}
moon_embed = moon.event_embed(moon_event, "final", 45)
check(
    "Moon live card remains FINAL-only, 45-second, and visually separate",
    moon_embed.get("title") == "⚠️ 🌕 Gold Moon — ใกล้เริ่มแล้ว"
    and moon_embed.get("color") == moon.MOON_SYSTEM_COLOR
    and moon_embed.get("author", {}).get("name") == moon.MOON_SYSTEM_BADGE
    and "FINAL ONLY" in moon_embed.get("footer", {}).get("text", ""),
    str(moon_embed),
)

preview_events = moon.build_moon_test_preview_events(now_epoch=1786227910)
preview_embeds = [moon.build_moon_test_preview_embed(e) for e in preview_events]
check(
    "Moon Test Preview has three obvious read-only TEST cards",
    [event.get("kind") for event in preview_events] == ["gold", "rainbow", "mega"]
    and all(embed.get("title", "").startswith("🧪 TEST — ") for embed in preview_embeds)
    and all("ไม่แก้ moon_state.json" in embed.get("description", "") for embed in preview_embeds)
    and {embed.get("color") for embed in preview_embeds} == moon_colors,
    str(preview_embeds),
)

stock_source = STOCK_FILE.read_text(encoding="utf-8")
moon_source = MOON_FILE.read_text(encoding="utf-8")

stock_logic_names = names_used_by_functions(
    stock_source,
    {
        "derive_shop_cycle_keys",
        "update_shop_cycles",
        "filter_exact_stock_cycle_duplicates",
        "read_source_synced_stock",
        "target_snapshot",
        "current_active_events",
        "compare_target_events",
        "collect_live_data",
    },
)
moon_logic_names = names_used_by_functions(
    moon_source,
    {
        "parse_weather_page",
        "verify_snapshots",
        "ensure_round_ledger",
        "mark_round_missed",
        "find_matching_event",
        "resolve_anchor_state",
        "frozen_event_for_embed",
        "process_upcoming",
    },
)

check(
    "Stock alert/source core never reads presentation palette constants",
    stock_logic_names.isdisjoint(
        {
            "RARITY_UI_STYLES",
            "UNKNOWN_RARITY_UI_STYLE",
            "SELL_MULTIPLIER_UI_STYLES",
            "KNOWN_TARGET_RARITIES",
            "KNOWN_TARGET_RARITY_SOURCES",
            "BOT_DISPLAY_VERSION",
        }
    ),
    str(sorted(stock_logic_names)),
)
check(
    "Moon parsing/Frozen-Anchor core never reads presentation constants",
    moon_logic_names.isdisjoint(
        {"MOON_SYSTEM_COLOR", "MOON_SYSTEM_BADGE", "BOT_DISPLAY_VERSION"}
    ),
    str(sorted(moon_logic_names)),
)

check(
    "Both bots use distinct State integrity schemas and atomic replacement",
    stock.STATE_INTEGRITY_SCHEMA != moon.STATE_INTEGRITY_SCHEMA
    and "os.replace(" in stock_source
    and "os.replace(" in moon_source
    and "os.fsync(" in stock_source
    and "os.fsync(" in moon_source,
)

moon_workflow = (
    ROOT / ".github/workflows/moon_logic_test.yml"
).read_text(encoding="utf-8")
check(
    "Moon test workflow exposes a read-only test_preview",
    "test_preview" in moon_workflow
    and "TRIGGER_SOURCE: test_preview" in moon_workflow
    and "python moon_bot.py" in moon_workflow,
)

production_moon_workflow = (
    ROOT / ".github/workflows/gag2-moon.yml"
).read_text(encoding="utf-8")
check(
    "Production Moon workflow serializes and reads latest main before scan",
    "group: gag2-moon-alert" in production_moon_workflow
    and "cancel-in-progress: false" in production_moon_workflow
    and "ref: main" in production_moon_workflow
    and "git fetch origin main" in production_moon_workflow
    and "git reset --hard origin/main" in production_moon_workflow
    and production_moon_workflow.find("ref: main")
    < production_moon_workflow.find("run: python moon_bot.py"),
)

production_invokers = []
for workflow_path in sorted(
    list((ROOT / ".github/workflows").glob("*.yml"))
    + list((ROOT / ".github/workflows").glob("*.yaml"))
):
    workflow_text = workflow_path.read_text(encoding="utf-8")
    if (
        "python moon_bot.py" in workflow_text
        and "TRIGGER_SOURCE: test_preview" not in workflow_text
    ):
        production_invokers.append(workflow_path.name)
check(
    "Only canonical gag2-moon.yml can run Moon live mode",
    production_invokers == ["gag2-moon.yml"],
    str(production_invokers),
)

print("\n" + "=" * 72)
print(f"RESULT: {PASS}/{PASS + FAIL} PASSED")
if FAIL:
    print(f"❌ มี {FAIL} cross-bot test(s) ไม่ผ่าน")
    raise SystemExit(1)

print("✅ Stock / Sell / Moon UI ไม่ชนกัน และ Logic หลักไม่อ่านค่าหน้าตา")
print("✅ Moon production workflow prevents stale-State queued duplicates")
print("✅ Combined test is fully offline and read-only")
