
import ast
import copy
import hashlib
import importlib.util
import sys
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
# v6.5.7 observability/UI regression tests.
# These run AFTER the old guard suite and do not open GAG2 or Discord.
# -------------------------------------------------------------------------

result(
    "Alert logic version remains frozen (no migration alert)",
    bot.ALERT_LOGIC_VERSION == "6.4.4-image-alert-v1",
    bot.ALERT_LOGIC_VERSION,
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
timing_value = next(
    x["value"] for x in stock_embed["fields"]
    if x["name"] == "⏱️ เวลาและความล่าช้า"
)
result(
    "Compact Stock Embed has one-card fields and 45s delay",
    stock_embed.get("color") == 0x57F287
    and "เข้า Stock" in stock_embed.get("title", "")
    and "📌 สถานะ" in field_names
    and "🔄 รอบร้าน" in field_names
    and "45s" in timing_value,
    str(stock_embed),
)

magic_ui_event = {
    **magic_event,
    "emoji": "✨",
    "reason": "Magic Mail ที่ต้องการอยู่ใน Stock",
}
magic_embed = bot.build_event_embed(magic_ui_event, attempts=1)
result(
    "Magic Mail uses distinct purple Embed",
    magic_embed.get("color") == 0x9B59B6,
    str(magic_embed.get("color")),
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
    "Sell Embed uses gold compact title",
    sell_embed.get("color") == 0xFEE75C
    and "SELL ×2" in sell_embed.get("title", ""),
    str(sell_embed),
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

print("=" * 64)
print(f"RESULT: {PASS}/{PASS + FAIL} PASSED")

if FAIL:
    raise SystemExit(1)

print("✅ Guard behavior matches the intended safety policy.")
print("✅ This test does NOT open GAG2 and does NOT send Discord messages.")
