import os
import requests

WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()

MOONS = [
    ("🌕", "Gold Moon", "🌟", "Golden Seed"),
    ("🌈", "Rainbow Moon", "🌈", "Rainbow Seed"),
    ("🌙", "Mega Moon", "💠", "Mega Seed"),
]

def send_discord(content, embeds=None):
    if not WEBHOOK:
        raise RuntimeError("Missing GitHub Actions secret: DISCORD_WEBHOOK")
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    if embeds:
        payload["embeds"] = embeds
    r = requests.post(WEBHOOK, json=payload, timeout=20)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Discord webhook failed {r.status_code}: {r.text[:300]}")

def main():
    embeds = []
    for moon_emoji, moon_name, seed_emoji, seed_name in MOONS:
        embeds.extend([
            {
                "title": f"🧪 ⚠️ {moon_emoji} {moon_name} — เตรียมเข้าเกม",
                "description": (
                    "⏳ จำลองเหลือประมาณ **2 นาที**\n"
                    f"{seed_emoji} เตรียมหา **{seed_name}**\n"
                    "แนะนำเปิดเกมและเตรียมเข้าเซิร์ฟเวอร์\n\n"
                    "**TEST ONLY — ไม่ใช่ Event จริง**"
                ),
                "footer": {"text": "GAG2 Moon Test Preview · READ-ONLY"},
            },
            {
                "title": f"🧪 🚨 {moon_emoji} {moon_name} — เข้าเกมตอนนี้!",
                "description": (
                    "⏳ จำลองเหลือประมาณ **40 วินาที**\n"
                    f"{seed_emoji} **{seed_name}** กำลังจะมีโอกาสเกิด\n"
                    "อีกไม่กี่วินาที Event จะเริ่ม\n\n"
                    "**TEST ONLY — ไม่ใช่ Event จริง**"
                ),
                "footer": {"text": "GAG2 Moon Test Preview · READ-ONLY"},
            },
            {
                "title": f"🧪 🚨 {moon_emoji} {moon_name} — เริ่มแล้ว!",
                "description": (
                    "⏳ เหลือประมาณ **0 วินาที**\n"
                    f"{seed_emoji} **{seed_name}** สามารถมีโอกาสเกิดได้ตอนนี้\n"
                    "เข้าเกมและหา Seed ได้เลย!\n\n"
                    "**TEST ONLY — ไม่ใช่ Event จริง**"
                ),
                "footer": {"text": "GAG2 Moon Test Preview · READ-ONLY"},
            },
        ])

    send_discord(
        "🧪 **GAG2 Moon Test Preview — 2 นาที / 40 วินาที / เริ่มแล้ว**\n"
        "ไม่แตะระบบ Moon จริง",
        embeds,
    )
    print(f"Moon Test Preview sent: {len(embeds)} embeds")
    print("moon_state.json untouched")
    print("No GAG2 weather read performed")

if __name__ == "__main__":
    main()
