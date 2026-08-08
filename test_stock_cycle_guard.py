
import importlib.util
import sys
import types
from pathlib import Path

BOT_FILE = Path("bot.py")

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

print("=" * 64)
print(f"RESULT: {PASS}/{PASS + FAIL} PASSED")

if FAIL:
    raise SystemExit(1)

print("✅ Guard behavior matches the intended safety policy.")
print("✅ This test does NOT open GAG2 and does NOT send Discord messages.")
