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
    response = requests.post(WEBHOOK, json=payload, timeout=20)
    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord webhook failed {response.status_code}: "
            f"{response.text[:300]}"
        )

def main():
    embeds = []

    for moon_emoji, moon_name, seed_emoji, seed_name in MOONS:
        embeds.extend([
            {
                "title": f"🧪 ⚠️ {moon_emoji} {moon_name} — ใกล้เริ่มแล้ว",
                "description": (
                    "⏳ จำลองเหลือประมาณ **5 นาที**\n"
                    f"{seed_emoji} เตรียมหา **{seed_name}**\n"
                    "แนะนำเปิดเกมและเตรียมเข้าเซิร์ฟเวอร์ได้เลย\n\n"
                    "**TEST ONLY — ไม่ใช่ Event จริง**"
                ),
                "footer": {
                    "text": "GAG2 Moon Test Preview · 5m / ~45s · READ-ONLY"
                },
            },
            {
                "title": f"🧪 🚨 {moon_emoji} {moon_name} — เข้าเกมตอนนี้!",
                "description": (
                    "⏳ จำลองเหลือประมาณ **40–45 วินาที**\n"
                    f"{seed_emoji} **{seed_name}** กำลังจะมีโอกาสเกิด\n"
                    "นี่คือการเตือนครั้งสุดท้ายก่อน Event\n\n"
                    "**TEST ONLY — ไม่ใช่ Event จริง**"
                ),
                "footer": {
                    "text": "GAG2 Moon Test Preview · 5m / ~45s · READ-ONLY"
                },
            },
        ])

    send_discord(
        "🧪 **GAG2 Moon Test Preview — ระบบเดิม / เหลือ 2 แจ้งเตือน**\n"
        "ทดสอบ 5 นาที + ~45 วินาทีเท่านั้น",
        embeds,
    )

    print(f"Moon Test Preview sent: {len(embeds)} embeds")
    print("moon_state.json untouched")
    print("No GAG2 weather read performed")

if __name__ == "__main__":
    main()
