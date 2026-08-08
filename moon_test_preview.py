import os
import requests

WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()

TEST_CASES = [
    {
        "title": "🧪 🔔 🌕 Gold Moon — เตรียมตัว",
        "description": (
            "⏳ จำลองเหลือประมาณ **12 นาที**\n"
            "🌟 มีโอกาสเกิด **Golden Seed**\n\n"
            "**TEST ONLY — ไม่ใช่ Event จริง**"
        ),
    },
    {
        "title": "🧪 ⚠️ 🌕 Gold Moon — ใกล้เริ่มแล้ว",
        "description": (
            "⏳ จำลองเหลือประมาณ **5 นาที**\n"
            "🌟 เตรียมหา **Golden Seed**\n"
            "แนะนำเปิดเกมและเตรียมเข้าเซิร์ฟเวอร์\n\n"
            "**TEST ONLY — ไม่ใช่ Event จริง**"
        ),
    },
    {
        "title": "🧪 🚨 🌕 Gold Moon — เข้าเกมตอนนี้!",
        "description": (
            "⏳ จำลองเหลือประมาณ **45 วินาที**\n"
            "🌟 **Golden Seed** กำลังจะมีโอกาสเกิด\n\n"
            "**TEST ONLY — ไม่ใช่ Event จริง**"
        ),
    },
    {
        "title": "🧪 🔔 🌈 Rainbow Moon — เตรียมตัว",
        "description": (
            "⏳ จำลองเหลือประมาณ **12 นาที**\n"
            "🌈 มีโอกาสเกิด **Rainbow Seed**\n\n"
            "**TEST ONLY — ไม่ใช่ Event จริง**"
        ),
    },
    {
        "title": "🧪 ⚠️ 🌈 Rainbow Moon — ใกล้เริ่มแล้ว",
        "description": (
            "⏳ จำลองเหลือประมาณ **5 นาที**\n"
            "🌈 เตรียมหา **Rainbow Seed**\n"
            "แนะนำเปิดเกมและเตรียมเข้าเซิร์ฟเวอร์\n\n"
            "**TEST ONLY — ไม่ใช่ Event จริง**"
        ),
    },
    {
        "title": "🧪 🚨 🌈 Rainbow Moon — เข้าเกมตอนนี้!",
        "description": (
            "⏳ จำลองเหลือประมาณ **45 วินาที**\n"
            "🌈 **Rainbow Seed** กำลังจะมีโอกาสเกิด\n\n"
            "**TEST ONLY — ไม่ใช่ Event จริง**"
        ),
    },
    {
        "title": "🧪 🔔 🌙 Mega Moon — เตรียมตัว",
        "description": (
            "⏳ จำลองเหลือประมาณ **12 นาที**\n"
            "💠 มีโอกาสเกิด **Mega Seed**\n\n"
            "**TEST ONLY — ไม่ใช่ Event จริง**"
        ),
    },
    {
        "title": "🧪 ⚠️ 🌙 Mega Moon — ใกล้เริ่มแล้ว",
        "description": (
            "⏳ จำลองเหลือประมาณ **5 นาที**\n"
            "💠 เตรียมหา **Mega Seed**\n"
            "แนะนำเปิดเกมและเตรียมเข้าเซิร์ฟเวอร์\n\n"
            "**TEST ONLY — ไม่ใช่ Event จริง**"
        ),
    },
    {
        "title": "🧪 🚨 🌙 Mega Moon — เข้าเกมตอนนี้!",
        "description": (
            "⏳ จำลองเหลือประมาณ **45 วินาที**\n"
            "💠 **Mega Seed** กำลังจะมีโอกาสเกิด\n\n"
            "**TEST ONLY — ไม่ใช่ Event จริง**"
        ),
    },
]


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


def main():
    embeds = []

    for case in TEST_CASES:
        embeds.append(
            {
                "title": case["title"],
                "description": case["description"],
                "footer": {
                    "text": "GAG2 Moon Test Preview · READ-ONLY"
                },
            }
        )

    send_discord(
        "🧪 **GAG2 Moon Test Preview**\n"
        "ทดสอบหน้าตาแจ้งเตือนเท่านั้น — **ไม่แตะระบบ Moon จริง**\n"
        "ครอบคลุม **Gold Moon / Rainbow Moon / Mega Moon**",
        embeds,
    )

    print(f"Moon Test Preview sent: {len(embeds)} embeds")
    print("moon_state.json untouched")
    print("No GAG2 weather read performed")


if __name__ == "__main__":
    main()
