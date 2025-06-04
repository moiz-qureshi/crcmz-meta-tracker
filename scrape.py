#!/usr/bin/env python3
import os
import json
import asyncio
import re
from io import BytesIO
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from PIL import Image
import pillow_avif
import discord

# === Load Environment ===
load_dotenv()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
IMGUR_CLIENT_ID = os.getenv("IMGUR_CLIENT_ID")
META_STORE = "last_meta.json"

# === Config ===
MODES = {
    "Resurgence": "https://wzstats.gg/warzone/meta/resurgence",
    "Verdansk": "https://wzstats.gg/"
}

RANGES = {
    "Long Range": None,  # Default tab
    "Close Range": "a.menu-item:has-text('Close range')",
    "Sniper": "a.menu-item:has-text('Sniper')"
}

TITLE_EMOJIS = {
    ("Resurgence", "Long Range"): "🎯",
    ("Resurgence", "Close Range"): "🔫",
    ("Resurgence", "Sniper"): "🎯",
    ("Verdansk", "Long Range"): "🏹",
    ("Verdansk", "Close Range"): "🪖",
    ("Verdansk", "Sniper"): "🏹"
}

EMBED_COLORS = {
    "Resurgence": 0x3498db,  # Blue
    "Verdansk": 0x2ecc71     # Green
}

def convert_avif_to_png(image_bytes):
    with Image.open(BytesIO(image_bytes)) as img:
        output = BytesIO()
        img.convert("RGB").save(output, format="PNG")
        output.seek(0)
        return output

def upload_image_to_imgur(image_url):
    try:
        response = requests.get(image_url)
        response.raise_for_status()
        image_bytes = response.content

        # Convert AVIF if necessary
        if image_url.endswith(".avif") or response.headers.get("Content-Type") == "image/avif":
            image_data = convert_avif_to_png(image_bytes)
        else:
            image_data = BytesIO(image_bytes)

        headers = {'Authorization': f'Client-ID {IMGUR_CLIENT_ID}'}
        files = {'image': image_data}
        res = requests.post("https://api.imgur.com/3/image", headers=headers, files=files)
        res.raise_for_status()
        return res.json()['data']['link']
    except Exception as e:
        print(f"⚠️ Imgur upload failed: {e}")
        return None

def scrape_top_gun(mode: str, url: str, range_label: str, selector: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url)
        page.wait_for_selector("app-weapon-loadouts")
        page.wait_for_timeout(3000)

        if selector:
            page.locator(selector).first.click(force=True)
            page.wait_for_timeout(2000)

        gun = page.query_selector("div.loadout-container")
        gun.scroll_into_view_if_needed()
        gun.click()
        page.wait_for_timeout(1000)

        name_el = gun.query_selector("h3.loadout-content-name")
        gun_name = name_el.inner_text().strip() if name_el else "Unknown Weapon"

        class_block = gun.query_selector("div.loadout-detail")
        raw_lines = class_block.inner_text().strip().splitlines() if class_block else []

        class_lines = [
            line for line in raw_lines
            if not any(x in line.upper() for x in ["LEVEL", "CREATED ON", "UPDATED ON", "LOADOUTS"])
        ]

        date_line = next((line for line in raw_lines if "Created on" in line or "Updated on" in line), None)
        if date_line:
            match = re.search(r"(Created|Updated) on[ -]+(.+)", date_line)
            clean_date = match.group(2).strip() if match else date_line.split("on")[-1].strip()
        else:
            fallback = class_lines[-1] if class_lines else ""
            clean_date = fallback if re.search(r"\d{4}", fallback) else "Unknown"

        formatted_lines = []
        i = 0
        while i < len(class_lines):
            name = class_lines[i]
            if i + 1 < len(class_lines):
                attachment_type = class_lines[i + 1]
                formatted_lines.append(f"• {name} — {attachment_type}")
                i += 2
            else:
                formatted_lines.append(f"• {name}")
                i += 1

        image_container = gun.query_selector("div.weapon-image-rank-container img")
        gun_image = image_container.get_attribute("src") if image_container else None
        gun_image = upload_image_to_imgur(gun_image) if gun_image else None

        browser.close()
        return {
            "mode": mode,
            "range": range_label,
            "gun": gun_name,
            "class": formatted_lines,
            "image": gun_image,
            "updated": clean_date,
        }

def load_last_meta():
    if os.path.exists(META_STORE):
        try:
            with open(META_STORE, "r") as f:
                content = f.read().strip()
                if content:
                    return json.loads(content)
        except Exception as e:
            print(f"⚠️ Could not load last_meta.json: {e}")
    return {}

def save_last_meta(data):
    with open(META_STORE, "w") as f:
        json.dump(data, f, indent=2)

async def send_all_metas(metas):
    intents = discord.Intents.default()
    intents.messages = True
    client = discord.Client(intents=intents)

    last_posted = load_last_meta()

    @client.event
    async def on_ready():
        print(f"✅ Logged in as {client.user}")
        channel = client.get_channel(CHANNEL_ID)

        new_posted = {}

        for meta in metas:
            key = f"{meta['mode']}_{meta['range']}"
            old = last_posted.get(key)

            current = {
                "gun": meta["gun"],
                "class": meta["class"],
                "mode": meta["mode"],
                "range": meta["range"]
            }

            if old and old["gun"] == current["gun"] and old["class"] == current["class"]:
                print(f"⏩ Skipping {key} — no change")
                new_posted[key] = old
                continue

            print(f"🔁 Posting update for {key}")

            emoji = TITLE_EMOJIS.get((meta["mode"], meta["range"]), "🛡️")
            expected_title = f"{emoji} {meta['mode']} {meta['range']} Meta Loadout\n**{meta['gun']}**"

            async for msg in channel.history(limit=50):
                if msg.author == client.user and msg.embeds:
                    embed = msg.embeds[0]
                    if embed.title and embed.title.strip() == expected_title.strip():
                        try:
                            await msg.delete()
                            print(f"🧹 Deleted old message for {key}")
                        except Exception as e:
                            print(f"⚠️ Failed to delete message: {e}")

            description = "Attachments:\n" + "\n".join(meta["class"])

            embed = discord.Embed(
                title=expected_title,
                description=description,
                color=EMBED_COLORS.get(meta["mode"], 0x7289DA)
            )

            if meta["image"]:
                embed.set_image(url=meta["image"])

            await channel.send(embed=embed)
            new_posted[key] = current

        save_last_meta({**last_posted, **new_posted})
        await client.close()

    async with client:
        await client.start(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    all_metas = []
    for mode, url in MODES.items():
        for range_label, selector in RANGES.items():
            print(f"🔍 Scraping {mode} [{range_label}]...")
            meta = scrape_top_gun(mode, url, range_label, selector)
            all_metas.append(meta)

    asyncio.run(send_all_metas(all_metas))
