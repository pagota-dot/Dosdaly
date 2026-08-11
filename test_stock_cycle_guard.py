
import ast
import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import time
import types
from pathlib import Path

BOT_FILE = Path("bot.py")

# Stub requests so this test suite is fully offline and cannot send a webhook.
requests_module = types.ModuleType("requests")

def blocked_network_call(*args, **kwargs):
    raise AssertionError("offline logic test attempted a network call")

requests_module.post = blocked_network_call
sys.modules.setdefault("requests", requests_module)

# Stub Selenium only for offline import.
selenium = types.ModuleType("selenium")
webdriver_module = types.ModuleType("selenium.webdriver")
chrome_module = types.ModuleType("selenium.webdriver.chrome")
options_module = types.ModuleType("selenium.webdriver.chrome.options")
support_module = types.ModuleType("selenium.webdriver.support")
ui_module = types.ModuleType("selenium.webdriver.support.ui")

class DummyOptions:
    pass

class DummyWait:
    pass

webdriver_module.Chrome = lambda *a, **k: None
options_module.Options = DummyOptions
ui_module.WebDriverWait = DummyWait
selenium.webdriver = webdriver_module

sys.modules.setdefault("selenium", selenium)
sys.modules.setdefault("selenium.webdriver", webdriver_module)
sys.modules.setdefault("selenium.webdriver.chrome", chrome_module)
sys.modules.setdefault("selenium.webdriver.chrome.options", options_module)
sys.modules.setdefault("selenium.webdriver.support", support_module)
sys.modules.setdefault("selenium.webdriver.support.ui", ui_module)

spec = importlib.util.spec_from_file_location("bot_under_test", BOT_FILE)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

TARGET = "super syrup watering can"
BASE_ITEMS = [
    {
        "name": "Super Syrup Watering Can",
        "qty": 2,
        "rarity": "COMMON",
        "type": "gear",
    }
]

PASS = 0
FAIL = 0


def result(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"✅ PASS {name}")
    else:
        FAIL += 1
        print(f"❌ FAIL {name}")
        if detail:
            print("   ", detail)


def make_old(cycle_key="timer:100000", source="gag2-timer", present=True, items=None):
    return {
        "targets": {
            "stock": {
                TARGET: {
                    "present": present,
                    "items": list(items if items is not None else BASE_ITEMS),
                }
            }
        },
        "shop_cycles": {
            "gear": {
                "id": 10,
                "key": cycle_key,
                "source": source,
            }
        },
    }


def make_cycle(cycle_key, source="gag2-timer", changed=True):
    return {
        "gear": {
            "id": 11 if changed else 10,
            "key": cycle_key,
            "source": source,
            "remaining_seconds": 180,
            "changed": changed,
        }
    }


def event(items=None):
    return {
        "kind": "stock",
        "target_key": TARGET,
        "label": "Super Syrup Watering Can",
        "emoji": "🪣",
        "items": list(items if items is not None else BASE_ITEMS),
        "reason": "รอบร้านใหม่",
    }


print("GAG2 Stock Cycle Guard - OFFLINE TEST")
print("Bot file:", BOT_FILE)
print("=" * 64)

# Existing bot rule test.
old_rules = bot.alert_rule_self_test()
result(
    "Existing alert rules remain 7/7",
    old_rules.get("ok")
    and old_rules.get("passed_classes") == 7
    and old_rules.get("total_classes") == 7,
    str(old_rules),
)

# Same exact cycle.
accepted, diag = bot.filter_exact_stock_cycle_duplicates(
    [event()],
    make_old(),
    make_cycle("timer:100000", changed=False),
)
result(
    "Same cycle + same item => SUPPRESS",
    accepted == [] and diag[-1]["reason"] == "same-cycle",
    str(diag),
)

# Clear short source-key jitter.
for delta in (30, 60, 90, 120):
    accepted, diag = bot.filter_exact_stock_cycle_duplicates(
        [event()],
        make_old(),
        make_cycle(f"timer:{100000 + delta}"),
    )
    result(
        f"Cycle-key jitter +{delta}s => SUPPRESS",
        accepted == [] and diag[-1]["reason"] == "clear-jitter",
        str(diag),
    )

# Ambiguous range intentionally fail-open.
for delta in (121, 150, 180, 210, 239):
    accepted, diag = bot.filter_exact_stock_cycle_duplicates(
        [event()],
        make_old(),
        make_cycle(f"timer:{100000 + delta}"),
    )
    result(
        f"Ambiguous +{delta}s => FAIL-OPEN / ALLOW",
        len(accepted) == 1 and diag[-1]["reason"] == "unknown",
        str(diag),
    )

# Real next 5-minute cycle.
for delta in (240, 270, 300, 330, 360):
    accepted, diag = bot.filter_exact_stock_cycle_duplicates(
        [event()],
        make_old(),
        make_cycle(f"timer:{100000 + delta}"),
    )
    result(
        f"Real next-cycle evidence +{delta}s => ALLOW",
        len(accepted) == 1 and diag[-1]["reason"] == "plausible-new-cycle",
        str(diag),
    )

# Explicit timer rollover.
accepted, diag = bot.filter_exact_stock_cycle_duplicates(
    [event()],
    make_old(),
    make_cycle("timer:100120"),
    source_sync={
        "rollover_details": [
            {
                "shop": "gear",
                "before_seconds": 8,
                "after_seconds": 286,
            }
        ]
    },
)
result(
    "Explicit GAG2 rollover => ALLOW immediately",
    len(accepted) == 1 and diag[-1]["reason"] == "explicit-rollover",
    str(diag),
)

# Qty changed.
qty3 = [{**BASE_ITEMS[0], "qty": 3}]
accepted, diag = bot.filter_exact_stock_cycle_duplicates(
    [event(qty3)],
    make_old(),
    make_cycle("timer:100030"),
)
result(
    "Quantity x2 -> x3 => ALLOW immediately",
    len(accepted) == 1 and diag[-1]["reason"] == "target-value-changed",
    str(diag),
)

# Rarity changed.
super_item = [{**BASE_ITEMS[0], "rarity": "SUPER"}]
accepted, diag = bot.filter_exact_stock_cycle_duplicates(
    [event(super_item)],
    make_old(),
    make_cycle("timer:100030"),
)
result(
    "Rarity COMMON -> SUPER => ALLOW immediately",
    len(accepted) == 1 and diag[-1]["reason"] == "target-value-changed",
    str(diag),
)

# Absent -> present.
accepted, diag = bot.filter_exact_stock_cycle_duplicates(
    [event()],
    make_old(present=False),
    make_cycle("timer:100030"),
)
result(
    "Absent -> present => ALLOW immediately",
    len(accepted) == 1 and diag[-1]["reason"] == "absent-to-present",
    str(diag),
)

# Untrusted timer -> fail-open.
accepted, diag = bot.filter_exact_stock_cycle_duplicates(
    [event()],
    make_old("fp:old", "fingerprint-fallback"),
    make_cycle("fp:new", "fingerprint-fallback"),
)
result(
    "No trusted GAG2 timer => FAIL-OPEN / ALLOW",
    len(accepted) == 1 and diag[-1]["reason"] == "unknown",
    str(diag),
)

# Sell untouched.
sell_event = {
    "kind": "sell",
    "target_key": "maple mushroom",
    "multi": 2.0,
}
accepted, _ = bot.filter_exact_stock_cycle_duplicates(
    [sell_event],
    make_old(),
    make_cycle("timer:100030"),
)
result("Sell event untouched", accepted == [sell_event])

# Magic Mail untouched.
magic_event = {
    "kind": "stock",
    "target_key": "legendary magic mail|legendary",
    "label": "Legendary Magic Mail",
    "items": [
        {
            "name": "Legendary Magic Mail",
            "qty": 1,
            "rarity": "LEGENDARY",
            "type": "gear",
        }
    ],
}
accepted, _ = bot.filter_exact_stock_cycle_duplicates(
    [magic_event],
    make_old(),
    make_cycle("timer:100030"),
)
result("Magic Mail untouched", accepted == [magic_event])

# -------------------------------------------------------------------------
# v6.5.10 Wiki-authoritative rarity UI + State Integrity regression tests.
# These run AFTER the old guard suite and do not open GAG2 or Discord.
# -------------------------------------------------------------------------

result(
    "Alert logic version remains frozen (no migration alert)",
    bot.ALERT_LOGIC_VERSION == "6.4.4-image-alert-v1",
    bot.ALERT_LOGIC_VERSION,
)
result(
    "Display release is v6.5.12",
    bot.BOT_DISPLAY_VERSION == "6.5.12",
    bot.BOT_DISPLAY_VERSION,
)

# Source hashes prove that all alert-producing and source-cycle functions are
# byte-for-byte identical to the approved pre-UI-upgrade release.
LOCKED_CORE_HASHES = {
    "derive_shop_cycle_keys": "e32a47fffedecb0e6495a4a18e02c81886f390672a8fb1c2ec40a465059e4398",
    "update_shop_cycles": "3e841d22e1373e8bd1b2c53882028a6333190dc13836e58273812e0e82bfda6b",
    "filter_exact_stock_cycle_duplicates": "20e3fd9b97767e04d9ae51f9915ded009ea2ee0a4f5406a41c69fd04ed292888",
    "read_source_synced_stock": "4b101076781cc59d14d147913ab78d9f3d9d6ef59d8f3e600d337629ed729fb1",
    "target_snapshot": "4e555147d4fa71865a203edede1f924921721b736eafb4b167fe253f09498ae2",
    "current_active_events": "87471d375d8067c8ceac4d4148eea92c067ad28fa79b3d7cbe7173a3aa56eecc",
    "compare_target_events": "3a5a2ec1d8be9afed93a17decbb01959580397b7d335417fba4dd576d3ccacbb",
    "collect_live_data": "9427d01aa5ba44265264aebe85b59596e7383bf4417be37454e4e9069e507fe0",
}

source_text = BOT_FILE.read_text(encoding="utf-8")
source_tree = ast.parse(source_text)
actual_core_hashes = {}
for node in source_tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in LOCKED_CORE_HASHES:
        function_source = ast.get_source_segment(source_text, node)
        actual_core_hashes[node.name] = hashlib.sha256(
            function_source.encode("utf-8")
        ).hexdigest()

result(
    "Locked alert/source core is byte-for-byte unchanged",
    actual_core_hashes == LOCKED_CORE_HASHES,
    str(actual_core_hashes),
)

ui_event = event()
ui_event["image_url"] = "https://example.com/watering-can.png"
event_before_context = copy.deepcopy(ui_event)
ui_context = bot.build_event_observability_context(
    ui_event,
    current_shop_cycles={
        "gear": {
            "id": 12,
            "key": "timer:100300",
            "source": "gag2-timer",
        }
    },
    source_sync={
        "captured_at_epoch": 100040,
        "samples": 2,
        "multi_snapshot_stable": True,
        "cycle_confidence": "MID_CYCLE_STABLE",
        "total_timer_wait_seconds": 0,
    },
    current_shop_fp={"gear": "gear-fp"},
)
result(
    "Observability context does not mutate alert event",
    ui_event == event_before_context,
)
result(
    "Round context derives trusted cycle timing",
    ui_context.get("shop") == "gear"
    and ui_context.get("cycle_id") == 12
    and ui_context.get("cycle_reset_epoch") == 100000
    and ui_context.get("round_id") == "STOCK-GEAR-C12",
    str(ui_context),
)

ui_context["alert_sent_epoch"] = 100045
stock_embed = bot.build_event_embed(
    ui_event,
    attempts=2,
    context=ui_context,
)
field_names = [x.get("name") for x in stock_embed.get("fields", [])]
status_value = next(
    x["value"] for x in stock_embed["fields"]
    if x["name"] == "📌 สถานะ"
)
timing_value = next(
    x["value"] for x in stock_embed["fields"]
    if x["name"] == "⏱️ เวลาและความล่าช้า"
)
result(
    "Live-like COMMON parse is displayed as Wiki-authoritative SUPER",
    stock_embed.get("color") == bot.RARITY_UI_STYLES["super"]["color"]
    and "🌈 SUPER" in stock_embed.get("title", "")
    and "COMMON" not in stock_embed.get("title", "")
    and "SUPER" in status_value
    and "COMMON" not in status_value
    and "เข้า Stock" in stock_embed.get("title", "")
    and "📌 สถานะ" in field_names
    and "🔄 รอบร้าน" in field_names
    and "45s" in timing_value,
    str(stock_embed),
)
result(
    "Wiki rarity presentation does not mutate raw event/source evidence",
    ui_event == event_before_context
    and ui_event["items"][0]["rarity"] == "COMMON",
    str(ui_event),
)

legacy_conflict_text = bot.format_single_event_message(
    ui_event,
    attempts=2,
)
result(
    "Legacy fallback also shows SUPER and never leaks conflicting COMMON",
    "SUPER" in legacy_conflict_text and "COMMON" not in legacy_conflict_text,
    legacy_conflict_text,
)

magic_ui_event = {
    **magic_event,
    "emoji": "✨",
    "reason": "Magic Mail ที่ต้องการอยู่ใน Stock",
}
magic_embed = bot.build_event_embed(magic_ui_event, attempts=1)
result(
    "Legendary Magic Mail uses Legendary gold border",
    magic_embed.get("color") == bot.RARITY_UI_STYLES["legendary"]["color"]
    and "LEGENDARY" in magic_embed.get("title", ""),
    str(magic_embed.get("color")),
)

super_ui_event = {
    "kind": "stock",
    "target_key": "super syrup sprinkler",
    "label": "Super Syrup Sprinkler",
    "emoji": "💦",
    "items": [
        {
            "name": "Super Syrup Sprinkler",
            "qty": 1,
            "rarity": "SUPER",
            "type": "gear",
        }
    ],
    "reason": "รอบร้านใหม่",
}
super_embed = bot.build_event_embed(super_ui_event, attempts=1)
result(
    "Super Stock uses rainbow badge and Super border",
    super_embed.get("color") == bot.RARITY_UI_STYLES["super"]["color"]
    and "🌈 SUPER" in super_embed.get("title", "")
    and "🟥🟧🟨🟩🟦🟪" in super_embed.get("description", ""),
    str(super_embed),
)

sell_ui_event = {
    "kind": "sell",
    "target_key": "maple mushroom",
    "label": "Maple Mushroom",
    "emoji": "🍄",
    "multi": 2.0,
    "reason": "Sell เปลี่ยนเป็น ×2",
}
sell_embed = bot.build_event_embed(sell_ui_event, attempts=3)
result(
    "SELL ×2 uses dedicated neon teal border",
    sell_embed.get("color") == bot.SELL_MULTIPLIER_UI_STYLES[2]["color"]
    and "SELL BOOST ×2" in sell_embed.get("title", ""),
    str(sell_embed),
)

sell_x4_event = {**sell_ui_event, "multi": 4.0, "reason": "Sell เปลี่ยนเป็น ×4"}
sell_x4_embed = bot.build_event_embed(sell_x4_event, attempts=3)
result(
    "SELL ×4 uses dedicated neon orange border",
    sell_x4_embed.get("color") == bot.SELL_MULTIPLIER_UI_STYLES[4]["color"]
    and "SELL BOOST ×4" in sell_x4_embed.get("title", ""),
    str(sell_x4_embed),
)

rarity_colors = {
    style["color"] for style in bot.RARITY_UI_STYLES.values()
}
sell_colors = {
    style["color"] for style in bot.SELL_MULTIPLIER_UI_STYLES.values()
}
result(
    "Sell border palette is separate from every Stock rarity color",
    rarity_colors.isdisjoint(sell_colors),
    f"rarity={rarity_colors} sell={sell_colors}",
)

catalog_ok = bot.KNOWN_TARGET_RARITIES == {
    "atlantic giant pumpkin": "LEGENDARY",
    "super syrup watering can": "SUPER",
    "super syrup sprinkler": "SUPER",
    "amber cranberry": "SUPER",
    "maple mushroom": "EPIC",
}
catalog_sources_ok = (
    set(bot.KNOWN_TARGET_RARITY_SOURCES) == set(bot.KNOWN_TARGET_RARITIES)
    and all(
        url.startswith("https://growagarden2.fandom.com/wiki/")
        for url in bot.KNOWN_TARGET_RARITY_SOURCES.values()
    )
    and bot.RARITY_CATALOG_VERIFIED_DATE == "2026-08-09"
)
preview_amber = next(
    event for event in bot.build_test_preview_events()
    if event.get("target_key") == "amber cranberry"
)
result(
    "Fandom Wiki target rarity catalog and Amber preview are current",
    catalog_ok
    and catalog_sources_ok
    and bot.event_rarity(preview_amber) == "super",
    str(bot.KNOWN_TARGET_RARITIES),
)

conflicting_catalog_cases = {
    "atlantic giant pumpkin": ("COMMON", "legendary"),
    "super syrup watering can": ("COMMON", "super"),
    "super syrup sprinkler": ("RARE", "super"),
    "amber cranberry": ("LEGENDARY", "super"),
    "maple mushroom": ("COMMON", "epic"),
}
catalog_conflicts_resolved = True
for target_key, (wrong_page_rarity, expected_rarity) in conflicting_catalog_cases.items():
    label = next(
        (
            meta["label"]
            for key_name, meta in {
                **bot.EXACT_STOCK_TARGETS,
                **bot.SELL_TARGETS,
            }.items()
            if key_name == target_key
        ),
        target_key.title(),
    )
    conflict_event = {
        "kind": "stock",
        "target_key": target_key,
        "label": label,
        "items": [
            {
                "name": label,
                "qty": 1,
                "rarity": wrong_page_rarity,
                "type": "gear",
            }
        ],
    }
    if (
        bot.event_rarity(conflict_event) != expected_rarity
        or bot.display_item_rarity(conflict_event, conflict_event["items"][0])
        != expected_rarity.upper()
    ):
        catalog_conflicts_resolved = False

result(
    "Every watched Wiki name overrides a conflicting page rarity for UI only",
    catalog_conflicts_resolved,
    str(conflicting_catalog_cases),
)

# Real alert path: exactly one request and no repeated plain content.
captured_sends = []
original_send_discord = bot.send_discord

def fake_send_discord(content="", embeds=None):
    now = time.time()
    captured_sends.append({"content": content, "embeds": embeds})
    return {
        "request_epoch": now,
        "completed_epoch": now + 0.123,
        "delivery_ms": 123,
        "status_code": 204,
    }

bot.send_discord = fake_send_discord
event_before_send = copy.deepcopy(ui_event)
deliveries = bot.send_event_alerts(
    [ui_event],
    attempts=2,
    alert_contexts=[ui_context],
)
result(
    "Exactly one FINAL Embed is sent (no duplicate plain block)",
    len(captured_sends) == 1
    and captured_sends[0]["content"] == ""
    and len(captured_sends[0]["embeds"] or []) == 1
    and deliveries[0]["ui_mode"] == "compact-embed",
    str(captured_sends),
)
result(
    "UI send path does not mutate the approved event",
    ui_event == event_before_send,
)

# If formatting breaks, use the legacy text before making any request.
original_build_event_embed = bot.build_event_embed

def broken_embed(*args, **kwargs):
    raise ValueError("forced UI failure")

captured_sends.clear()
bot.build_event_embed = broken_embed
fallback_deliveries = bot.send_event_alerts([ui_event], attempts=2)
result(
    "Embed build failure falls back to one legacy plain alert",
    len(captured_sends) == 1
    and "Super Syrup Watering Can" in captured_sends[0]["content"]
    and not captured_sends[0]["embeds"]
    and fallback_deliveries[0]["ui_mode"] == "legacy-plain-fallback",
    str(captured_sends),
)
bot.build_event_embed = original_build_event_embed

# A network error must not cause a blind fallback resend and duplicate alert.
network_attempts = []

def failing_send_discord(content="", embeds=None):
    network_attempts.append((content, embeds))
    raise RuntimeError("forced Discord failure")

bot.send_discord = failing_send_discord
network_error_raised = False
try:
    bot.send_event_alerts([ui_event], attempts=2)
except RuntimeError:
    network_error_raised = True

result(
    "Discord failure raises after one request (no duplicate retry)",
    network_error_raised and len(network_attempts) == 1,
    str(network_attempts),
)
bot.send_discord = original_send_discord

ledger_state = {"round_ledger": bot.empty_round_ledger()}
added = bot.record_alert_deliveries_safe(ledger_state, deliveries)
ledger_entries = ledger_state["round_ledger"]["entries"]
result(
    "Round Ledger stores sent status and Discord latency",
    added == 1
    and len(ledger_entries) == 1
    and ledger_entries[0]["status"] == "alert_sent"
    and ledger_entries[0]["timeline"]["discord_delivery_ms"] == 123,
    str(ledger_entries),
)

guard_ledger_state = {"round_ledger": bot.empty_round_ledger()}
guard_added = bot.record_guard_diagnostics_safe(
    guard_ledger_state,
    [
        {
            "action": "suppress",
            "target": TARGET,
            "shop": "gear",
            "reason": "same-cycle",
            "cycle_delta_seconds": 0,
        }
    ],
    current_shop_cycles={
        "gear": {"id": 12, "key": "timer:100300", "source": "gag2-timer"}
    },
    source_sync={
        "captured_at_epoch": time.time(),
        "samples": 2,
        "multi_snapshot_stable": True,
    },
)
result(
    "Round Ledger records duplicate suppression reason",
    guard_added == 1
    and guard_ledger_state["round_ledger"]["entries"][0]["status"]
    == "suppressed_duplicate",
    str(guard_ledger_state),
)

result(
    "Ledger failure is non-blocking",
    bot.record_alert_deliveries_safe(None, deliveries) == 0,
)

semantic = bot.semantic_state_view(
    {"round_ledger": {"entries": [{"entry_id": "x"}]}}
)
result(
    "Smart State view persists Round Ledger",
    semantic.get("round_ledger", {}).get("entries", [])[0].get("entry_id") == "x",
    str(semantic),
)

oversized_ledger = bot.empty_round_ledger()
now_epoch = time.time()
oversized_ledger["entries"] = [
    {
        "entry_id": f"entry-{i}",
        "recorded_epoch": now_epoch,
    }
    for i in range(bot.ROUND_LEDGER_MAX_ENTRIES + 20)
]
bot.prune_round_ledger(oversized_ledger, now_epoch=now_epoch)
result(
    "Round Ledger retention is bounded",
    len(oversized_ledger["entries"]) == bot.ROUND_LEDGER_MAX_ENTRIES,
    str(len(oversized_ledger["entries"])),
)

# -------------------------------------------------------------------------
# v6.5.12 Daily Stock/Magic Mail pieces: one maximum quantity per
# target/shop cycle.
# -------------------------------------------------------------------------
daily_quantity_names = set()
for node in source_tree.body:
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name
        in {
            "exact_stock_quantity",
            "update_stock_piece_total",
            "update_magic_mail_piece_total",
            "update_daily_occurrence_stats",
        }
    ):
        daily_quantity_names.update(
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name)
        )
result(
    "Daily piece counter adds no request, Discord send, or sleep",
    daily_quantity_names.isdisjoint(
        {
            "requests",
            "send_discord",
            "collect_live_data",
            "read_source_synced_stock",
            "sleep",
        }
    ),
    str(sorted(daily_quantity_names)),
)

daily_target = "atlantic giant pumpkin"
daily_label = "Atlantic Giant Pumpkin"
daily_stats = {"days": {}}
daily_key = bot.thailand_date_str()


def update_daily_stock_quantity(quantity, cycle_key):
    stock_items = [
        {
            "name": daily_label,
            "qty": quantity,
            "rarity": "LEGENDARY",
            "type": "seed",
        }
    ]
    snapshot = bot.target_snapshot(stock_items, [])
    bot.update_daily_occurrence_stats(
        daily_stats,
        stock_items,
        [],
        snapshot,
        {
            "seed": {
                "id": 1,
                "key": cycle_key,
                "source": "gag2-timer",
            }
        },
        {"seed": f"fp-{cycle_key}"},
    )


update_daily_stock_quantity(1, "timer:daily-1")
daily_day = daily_stats["days"][daily_key]
result(
    "First Stock cycle records one occurrence and its real quantity",
    daily_day["stock_occurrences"].get(daily_target) == 1
    and daily_day["stock_pieces"].get(daily_target) == 1
    and daily_day["stock_cycle_quantities"][daily_target].get(
        "seed|timer:daily-1"
    ) == 1,
    str(daily_day),
)

update_daily_stock_quantity(1, "timer:daily-1")
result(
    "Repeated snapshot in the same cycle adds neither round nor piece",
    daily_day["stock_occurrences"].get(daily_target) == 1
    and daily_day["stock_pieces"].get(daily_target) == 1,
    str(daily_day),
)

update_daily_stock_quantity(2, "timer:daily-1")
result(
    "Same-cycle quantity correction x1 to x2 adds only one piece",
    daily_day["stock_occurrences"].get(daily_target) == 1
    and daily_day["stock_pieces"].get(daily_target) == 2
    and daily_day["stock_cycle_quantities"][daily_target].get(
        "seed|timer:daily-1"
    ) == 2,
    str(daily_day),
)

update_daily_stock_quantity(1, "timer:daily-1")
result(
    "Same-cycle quantity decrease never subtracts or recounts pieces",
    daily_day["stock_occurrences"].get(daily_target) == 1
    and daily_day["stock_pieces"].get(daily_target) == 2,
    str(daily_day),
)

update_daily_stock_quantity(3, "timer:daily-2")
result(
    "New shop cycle adds its quantity to today's total",
    daily_day["stock_occurrences"].get(daily_target) == 2
    and daily_day["stock_pieces"].get(daily_target) == 5
    and daily_day["stock_cycle_quantities"][daily_target].get(
        "seed|timer:daily-2"
    ) == 3,
    str(daily_day),
)

result(
    "Duplicate parser variants use the highest quantity instead of a sum",
    bot.exact_stock_quantity(
        {
            "items": [
                {"qty": 2},
                {"qty": 4},
                {"qty": 4},
            ]
        }
    ) == 4,
)

daily_event = {
    "kind": "stock",
    "target_key": daily_target,
    "label": daily_label,
    "emoji": "🎃",
    "items": [
        {
            "name": daily_label,
            "qty": 3,
            "rarity": "LEGENDARY",
            "type": "seed",
        }
    ],
    "reason": "รอบร้านใหม่",
}
daily_counter = bot.event_daily_counter(daily_event, daily_stats)
daily_embed = bot.build_event_embed(
    daily_event,
    attempts=1,
    daily_stats=daily_stats,
)
daily_counter_field = next(
    field["value"]
    for field in daily_embed["fields"]
    if field["name"] == "📊 สถิติวันนี้"
)
result(
    "Real Stock card shows both today's round and piece totals",
    daily_counter == {"kind": "stock", "current": 2, "pieces": 5}
    and "ครั้งที่ **2**" in daily_counter_field
    and "รวมวันนี้ **5 ชิ้น**" in daily_counter_field,
    str(daily_counter_field),
)

preview_base = {"days": {}}
preview_before = copy.deepcopy(preview_base)
preview_event = next(
    item
    for item in bot.build_test_preview_events()
    if item.get("target_key") == "super syrup watering can"
)
preview_stats = bot.preview_daily_stats_for_event(
    preview_base,
    preview_event,
)
preview_day = preview_stats["days"][daily_key]
preview_embed = bot.build_test_preview_embed(preview_event, preview_base)
preview_counter_field = next(
    field["value"]
    for field in preview_embed["fields"]
    if field["name"] == "📊 สถิติวันนี้"
)
result(
    "Read-only Preview exercises x2 pieces without mutating real statistics",
    preview_event["items"][0]["qty"] == 2
    and preview_day["stock_occurrences"].get(TARGET) == 1
    and preview_day["stock_pieces"].get(TARGET) == 2
    and "รวมวันนี้ **2 ชิ้น**" in preview_counter_field
    and preview_base == preview_before,
    str(preview_counter_field),
)

magic_rarity = "legendary"
magic_daily_stats = {"days": {}}


def update_daily_magic_quantity(quantity, cycle_key, duplicate_quantities=None):
    quantities = (
        list(duplicate_quantities)
        if duplicate_quantities is not None
        else [quantity]
    )
    stock_items = [
        {
            "name": "Legendary Magic Mail",
            "qty": qty,
            "rarity": "LEGENDARY",
            "type": "gear",
        }
        for qty in quantities
    ]
    snapshot = bot.target_snapshot(stock_items, [])
    bot.update_daily_occurrence_stats(
        magic_daily_stats,
        stock_items,
        [],
        snapshot,
        {
            "gear": {
                "id": 1,
                "key": cycle_key,
                "source": "gag2-timer",
            }
        },
        {"gear": f"fp-{cycle_key}"},
    )


update_daily_magic_quantity(1, "timer:magic-1")
magic_day = magic_daily_stats["days"][daily_key]
result(
    "First Magic Mail cycle records one occurrence and its real quantity",
    magic_day["magic_mail"].get(magic_rarity) == 1
    and magic_day["magic_mail_pieces"].get(magic_rarity) == 1,
    str(magic_day),
)

update_daily_magic_quantity(1, "timer:magic-1")
result(
    "Repeated Magic Mail snapshot adds neither round nor piece",
    magic_day["magic_mail"].get(magic_rarity) == 1
    and magic_day["magic_mail_pieces"].get(magic_rarity) == 1,
    str(magic_day),
)

update_daily_magic_quantity(2, "timer:magic-1")
result(
    "Same-cycle Magic Mail correction x1 to x2 adds only one piece",
    magic_day["magic_mail"].get(magic_rarity) == 1
    and magic_day["magic_mail_pieces"].get(magic_rarity) == 2,
    str(magic_day),
)

update_daily_magic_quantity(1, "timer:magic-1")
result(
    "Same-cycle Magic Mail decrease never subtracts pieces",
    magic_day["magic_mail"].get(magic_rarity) == 1
    and magic_day["magic_mail_pieces"].get(magic_rarity) == 2,
    str(magic_day),
)

update_daily_magic_quantity(3, "timer:magic-2")
result(
    "New Magic Mail cycle adds its full quantity",
    magic_day["magic_mail"].get(magic_rarity) == 2
    and magic_day["magic_mail_pieces"].get(magic_rarity) == 5,
    str(magic_day),
)

duplicate_magic_stats_before = copy.deepcopy(magic_daily_stats)
update_daily_magic_quantity(
    4,
    "timer:magic-3",
    duplicate_quantities=[2, 4, 4],
)
result(
    "Duplicate Magic Mail parser variants use max quantity, not a sum",
    magic_day["magic_mail"].get(magic_rarity) == 3
    and magic_day["magic_mail_pieces"].get(magic_rarity) == 9
    and magic_daily_stats != duplicate_magic_stats_before,
    str(magic_day),
)

magic_daily_event = {
    "kind": "stock",
    "target_key": "legendary magic mail|legendary|gear",
    "label": "Legendary Magic Mail",
    "emoji": "✨",
    "items": [
        {
            "name": "Legendary Magic Mail",
            "qty": 4,
            "rarity": "LEGENDARY",
            "type": "gear",
        }
    ],
    "reason": "รอบร้านใหม่",
}
magic_counter = bot.event_daily_counter(
    magic_daily_event,
    magic_daily_stats,
)
magic_embed = bot.build_event_embed(
    magic_daily_event,
    attempts=1,
    daily_stats=magic_daily_stats,
)
magic_counter_field = next(
    field["value"]
    for field in magic_embed["fields"]
    if field["name"] == "📊 สถิติวันนี้"
)
result(
    "Magic Mail card shows both today's round and piece totals",
    magic_counter
    == {
        "kind": "magic",
        "rarity": "legendary",
        "current": 3,
        "pieces": 9,
    }
    and "ครั้งที่ **3**" in magic_counter_field
    and "รวมวันนี้ **9 ชิ้น**" in magic_counter_field,
    str(magic_counter_field),
)

magic_preview_base = {"days": {}}
magic_preview_before = copy.deepcopy(magic_preview_base)
magic_preview_event = next(
    item
    for item in bot.build_test_preview_events()
    if "magic mail" in bot.key(item.get("label", ""))
)
magic_preview_embed = bot.build_test_preview_embed(
    magic_preview_event,
    magic_preview_base,
)
magic_preview_field = next(
    field["value"]
    for field in magic_preview_embed["fields"]
    if field["name"] == "📊 สถิติวันนี้"
)
result(
    "Magic Mail Preview shows pieces and stays read-only",
    "ครั้งที่ **1**" in magic_preview_field
    and "รวมวันนี้ **1 ชิ้น**" in magic_preview_field
    and magic_preview_base == magic_preview_before,
    str(magic_preview_field),
)

legacy_magic_day = {
    "stock_occurrences": {},
    "stock_seen_cycles": {},
    "stock_pieces": {},
    "stock_cycle_quantities": {},
    "magic_mail": {magic_rarity: 3},
    "magic_seen_cycles": {
        magic_rarity: ["gear|old-1", "gear|old-2", "gear|old-3"]
    },
    "sell": {},
    "sell_seen_rotations": {},
    "alerts_sent": 0,
}
legacy_magic_stats = {
    "days": {"legacy-magic": copy.deepcopy(legacy_magic_day)}
}
migrated_magic_day = bot.ensure_daily_day(
    legacy_magic_stats,
    "legacy-magic",
)
result(
    "v6.5.11 Magic Mail rounds migrate with one minimum piece each",
    migrated_magic_day["magic_mail_pieces"].get(magic_rarity) == 3
    and set(
        migrated_magic_day["magic_cycle_quantities"][magic_rarity].values()
    )
    == {1},
    str(migrated_magic_day),
)

isolated_daily_stats = {"days": {}}
isolated_stock_items = [
    {
        "name": "Atlantic Giant Pumpkin",
        "qty": 1,
        "rarity": "LEGENDARY",
        "type": "seed",
    },
    {
        "name": "Super Syrup Watering Can",
        "qty": 2,
        "rarity": "COMMON",
        "type": "gear",
    },
    {
        "name": "Legendary Magic Mail",
        "qty": 2,
        "rarity": "LEGENDARY",
        "type": "gear",
    },
    {
        "name": "Super Magic Mail",
        "qty": 3,
        "rarity": "SUPER",
        "type": "gear",
    },
]
isolated_snapshot = bot.target_snapshot(isolated_stock_items, [])
bot.update_daily_occurrence_stats(
    isolated_daily_stats,
    isolated_stock_items,
    [],
    isolated_snapshot,
    {
        "seed": {
            "id": 1,
            "key": "timer:isolation-seed",
            "source": "gag2-timer",
        },
        "gear": {
            "id": 1,
            "key": "timer:isolation-gear",
            "source": "gag2-timer",
        },
    },
    {"seed": "fp-isolation-seed", "gear": "fp-isolation-gear"},
)
isolated_day = isolated_daily_stats["days"][daily_key]
result(
    "Every Stock name and Magic Mail rarity owns an isolated daily counter",
    isolated_day["stock_occurrences"]
    == {
        "atlantic giant pumpkin": 1,
        "super syrup watering can": 1,
    }
    and isolated_day["stock_pieces"]
    == {
        "atlantic giant pumpkin": 1,
        "super syrup watering can": 2,
    }
    and isolated_day["magic_mail"]
    == {"legendary": 1, "super": 1}
    and isolated_day["magic_mail_pieces"]
    == {"legendary": 2, "super": 3},
    str(isolated_day),
)

legacy_day = {
    "stock_occurrences": {daily_target: 3},
    "stock_seen_cycles": {
        daily_target: ["seed|old-1", "seed|old-2", "seed|old-3"]
    },
    "magic_mail": {},
    "magic_seen_cycles": {},
    "sell": {},
    "sell_seen_rotations": {},
    "alerts_sent": 0,
}
legacy_stats = {"days": {"legacy": copy.deepcopy(legacy_day)}}
migrated_day = bot.ensure_daily_day(legacy_stats, "legacy")
result(
    "v6.5.10 day migrates safely with one minimum piece per old occurrence",
    migrated_day["stock_pieces"].get(daily_target) == 3
    and set(migrated_day["stock_cycle_quantities"][daily_target].values())
    == {1},
    str(migrated_day),
)

legacy_today_stats = {"days": {daily_key: copy.deepcopy(legacy_day)}}
legacy_today_before = copy.deepcopy(legacy_today_stats)
manual_statistics = bot.format_today_statistics_message(legacy_today_stats)
result(
    "Manual Daily Statistics migrates for display only and remains read-only",
    "Atlantic Giant Pumpkin: **3 รอบ** · รวม **3 ชิ้น**"
    in manual_statistics
    and legacy_today_stats == legacy_today_before,
    manual_statistics,
)

reset_stats = {"days": {}}
old_day = bot.ensure_daily_day(reset_stats, "2099-01-01")
old_day["stock_pieces"][daily_target] = 9
new_day = bot.ensure_daily_day(reset_stats, "2099-01-02")
result(
    "Thailand date boundary starts a separate zeroed piece total",
    new_day["stock_pieces"] == {}
    and reset_stats["days"]["2099-01-01"]["stock_pieces"][daily_target]
    == 9,
    str(reset_stats),
)

# -------------------------------------------------------------------------
# State Integrity Guard + atomic save. These tests use a temporary directory;
# they never read or modify the repository's real state.json.
# -------------------------------------------------------------------------
original_state_path = bot.STATE_PATH
original_replace = bot.os.replace
original_collect_live_data = bot.collect_live_data
original_integrity_warning = bot.send_state_integrity_warning
original_webhook = bot.WEBHOOK

with tempfile.TemporaryDirectory() as temp_dir:
    test_state_path = Path(temp_dir) / "state.json"
    bot.STATE_PATH = test_state_path

    result(
        "Missing state is the only clean first-install path",
        bot.load_state() == {},
    )

    malformed_bytes = b'{"targets": '
    test_state_path.write_bytes(malformed_bytes)
    malformed_rejected = False
    try:
        bot.load_state()
    except bot.StateIntegrityError:
        malformed_rejected = True
    result(
        "Malformed existing Stock state is rejected unchanged",
        malformed_rejected and test_state_path.read_bytes() == malformed_bytes,
    )

    test_state_path.write_text("{}", encoding="utf-8")
    empty_rejected = False
    try:
        bot.load_state()
    except bot.StateIntegrityError:
        empty_rejected = True
    result(
        "Existing empty Stock state cannot become a fresh baseline",
        empty_rejected,
    )

    valid_state = {
        "version": "6.5.8",
        "alert_logic_version": bot.ALERT_LOGIC_VERSION,
        "shop_fingerprints": {"seed": "seed-fp", "gear": "gear-fp"},
        "shop_cycles": {},
        "targets": {"stock": {}, "magic_mail": {}, "sell": {}},
        "daily_stats": {"days": {}},
        "round_ledger": bot.empty_round_ledger(),
        "health": {"status": "ok"},
    }
    test_state_path.write_text(
        json.dumps(valid_state, ensure_ascii=False),
        encoding="utf-8",
    )
    result(
        "Compatible unsealed Stock state still loads normally",
        bot.load_state().get("alert_logic_version") == bot.ALERT_LOGIC_VERSION,
    )

    bot.save_state(valid_state)
    sealed_state = json.loads(test_state_path.read_text(encoding="utf-8"))
    result(
        "Atomic Stock save adds a valid SHA-256 integrity seal",
        sealed_state.get("_integrity", {}).get("schema")
        == bot.STATE_INTEGRITY_SCHEMA
        and bot.load_state().get("health", {}).get("status") == "ok",
        str(sealed_state.get("_integrity")),
    )

    tampered_state = copy.deepcopy(sealed_state)
    tampered_state["health"]["status"] = "tampered"
    test_state_path.write_text(
        json.dumps(tampered_state, ensure_ascii=False),
        encoding="utf-8",
    )
    tamper_rejected = False
    try:
        bot.load_state()
    except bot.StateIntegrityError:
        tamper_rejected = True
    result(
        "Tampered sealed Stock state is rejected",
        tamper_rejected,
    )

    bot.save_state(valid_state)
    original_bytes = test_state_path.read_bytes()

    def fail_atomic_replace(*args, **kwargs):
        raise OSError("forced atomic replace failure")

    bot.os.replace = fail_atomic_replace
    atomic_failure_raised = False
    try:
        changed_state = copy.deepcopy(valid_state)
        changed_state["health"]["status"] = "changed"
        bot.save_state(changed_state)
    except OSError:
        atomic_failure_raised = True
    finally:
        bot.os.replace = original_replace

    leftover_temp_files = list(Path(temp_dir).glob(".state.json.tmp-*"))
    result(
        "Failed atomic Stock save preserves original and cleans temp file",
        atomic_failure_raised
        and test_state_path.read_bytes() == original_bytes
        and leftover_temp_files == [],
        str(leftover_temp_files),
    )

    # A corrupt production state must stop before collect_live_data/GAG2.
    test_state_path.write_bytes(malformed_bytes)
    guard_calls = []
    bot.collect_live_data = lambda: (_ for _ in ()).throw(
        AssertionError("collect_live_data must not run")
    )
    bot.send_state_integrity_warning = lambda error: guard_calls.append(str(error))
    bot.WEBHOOK = "test-webhook-present"
    bot.main()
    result(
        "Stock integrity failure stops before GAG2 and emits SYSTEM warning",
        len(guard_calls) == 1 and test_state_path.read_bytes() == malformed_bytes,
        str(guard_calls),
    )

bot.STATE_PATH = original_state_path
bot.os.replace = original_replace
bot.collect_live_data = original_collect_live_data
bot.send_state_integrity_warning = original_integrity_warning
bot.WEBHOOK = original_webhook

print("=" * 64)
print(f"RESULT: {PASS}/{PASS + FAIL} PASSED")

if FAIL:
    raise SystemExit(1)

print("✅ Guard behavior matches the intended safety policy.")
print("✅ This test does NOT open GAG2 and does NOT send Discord messages.")
