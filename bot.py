import os
import re
import sqlite3
import asyncio
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import requests
import discord
from discord.ext import commands
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# =============================
# Config
# =============================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FA_API_KEY = os.getenv("FLIGHTAWARE_API_KEY")
FR24_URL = os.getenv("FR24_PUBLIC_FLIGHTS_URL")

if not DISCORD_TOKEN or not FA_API_KEY or not FR24_URL:
    raise RuntimeError("Missing required environment variables.")

FA_BASE = "https://aeroapi.flightaware.com/aeroapi"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

POLL_FR24_SECONDS = 3600
POLL_FA_SECONDS = 180
STATUS_UPDATE_SECONDS = 120
AUTO_DELETE_SECONDS = 1800  # 30 minutes

UA = "Mozilla/5.0"

# =============================
# Discord setup
# =============================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# =============================
# Database
# =============================
db = sqlite3.connect("flights.db")
cur = db.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    autopost_channel INTEGER,
    status_channel INTEGER,
    status_message INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS tracked_flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    channel_id INTEGER,
    flight_no TEXT,
    flight_date TEXT,
    notified_off INTEGER DEFAULT 0,
    notified_on INTEGER DEFAULT 0
)
""")

db.commit()

# =============================
# Helpers
# =============================
def fa_headers():
    return {"x-apikey": FA_API_KEY, "User-Agent": UA}

def parse_iso(ts):
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)

def localize(dt):
    return dt.astimezone(LOCAL_TZ) if dt else None

def fmt(dt):
    return localize(dt).strftime("%Y-%m-%d %H:%M %Z") if dt else "—"

def countdown(dt):
    if not dt:
        return "—"
    delta = localize(dt) - datetime.now(tz=LOCAL_TZ)
    mins = max(0, int(delta.total_seconds() // 60))
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"

async def delete_after(msg):
    await asyncio.sleep(AUTO_DELETE_SECONDS)
    try:
        await msg.delete()
    except:
        pass

# =============================
# FR24 Parsing
# =============================
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
FLIGHT_RE = re.compile(r"[A-Z]{2,4}\d+")

def parse_fr24(html):
    soup = BeautifulSoup(html, "html.parser")
    flights = []
    for tr in soup.select("tr"):
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if len(tds) >= 2 and DATE_RE.match(tds[0]) and FLIGHT_RE.match(tds[1]):
            flights.append((tds[0], tds[1]))
    return flights

# =============================
# FlightAware lookup
# =============================
def get_fa_instance(flight_no, flight_date):
    d = datetime.fromisoformat(flight_date).replace(tzinfo=LOCAL_TZ)
    start = (d - timedelta(days=1)).astimezone(ZoneInfo("UTC")).isoformat()
    end = (d + timedelta(days=2)).astimezone(ZoneInfo("UTC")).isoformat()

    r = requests.get(
        f"{FA_BASE}/flights/{flight_no}",
        headers=fa_headers(),
        params={"start": start, "end": end, "max_pages": 1},
        timeout=20
    )
    if r.status_code != 200:
        return None

    flights = r.json().get("flights", [])
    for f in flights:
        off = parse_iso(f.get("scheduled_off")) or parse_iso(f.get("estimated_off"))
        if off and localize(off).date() == d.date():
            return f
    return flights[0] if flights else None

# =============================
# Embeds
# =============================
def status_embed(info):
    e = discord.Embed(title="🧭 Hana’s Next Flight", color=discord.Color.blurple())
    if not info:
        e.description = "No upcoming flights detected."
        return e

    e.description = f"**{info['flight']} — {info['origin']} → {info['dest']}**"
    e.add_field(name="Departs in", value=countdown(info["off"]), inline=True)
    e.add_field(name="Scheduled", value=fmt(info["off"]), inline=True)
    e.add_field(name="Status", value=info["status"], inline=False)
    if info.get("aircraft"):
        e.add_field(name="Aircraft", value=info["aircraft"], inline=True)
    return e

def takeoff_embed(info):
    e = discord.Embed(title="🛫 Takeoff", description=f"**{info['flight']}**", color=discord.Color.green())
    e.add_field(name="Route", value=f"{info['origin']} → {info['dest']}", inline=False)
    e.add_field(name="Off", value=fmt(info["off"]), inline=True)
    return e

def landed_embed(info):
    e = discord.Embed(title="🛬 Landed", description=f"**{info['flight']}**", color=discord.Color.orange())
    e.add_field(name="On", value=fmt(info["on"]), inline=True)
    return e

# =============================
# Slash commands
# =============================
@bot.tree.command(name="set_autopost")
async def set_autopost(interaction: discord.Interaction):
    cur.execute("""
        INSERT INTO guild_settings (guild_id, autopost_channel)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET autopost_channel=excluded.autopost_channel
    """, (interaction.guild_id, interaction.channel_id))
    db.commit()
    await interaction.response.send_message("✅ Auto-post channel set.", ephemeral=True)

@bot.tree.command(name="set_status_message")
async def set_status(interaction: discord.Interaction):
    msg = await interaction.channel.send(embed=status_embed(None))
    cur.execute("""
        INSERT INTO guild_settings (guild_id, status_channel, status_message)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
        status_channel=excluded.status_channel,
        status_message=excluded.status_message
    """, (interaction.guild_id, interaction.channel_id, msg.id))
    db.commit()
    await interaction.response.send_message("✅ Status message created.", ephemeral=True)

# =============================
# Background tasks
# =============================
async def fr24_import_loop():
    while True:
        r = requests.get(FR24_URL, headers={"User-Agent": UA})
        flights = parse_fr24(r.text) if r.status_code == 200 else []

        today = date.today().isoformat()
        future = [(d, f) for d, f in flights if d >= today]

        cur.execute("SELECT guild_id, autopost_channel FROM guild_settings")
        for guild_id, channel_id in cur.fetchall():
            if not channel_id:
                continue
            for d, f in future:
                cur.execute("""
                    INSERT OR IGNORE INTO tracked_flights
                    (guild_id, channel_id, flight_no, flight_date)
                    VALUES (?, ?, ?, ?)
                """, (guild_id, channel_id, f, d))
                db.commit()
        await asyncio.sleep(POLL_FR24_SECONDS)

async def flight_poll_loop():
    while True:
        cur.execute("SELECT * FROM tracked_flights")
        for row in cur.fetchall():
            row_id, guild_id, channel_id, flight, fdate, off_sent, on_sent = row
            guild = bot.get_guild(guild_id)
            if not guild:
                continue
            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            inst = get_fa_instance(flight, fdate)
            if not inst:
                continue

            off = parse_iso(inst.get("actual_off"))
            on = parse_iso(inst.get("actual_on"))

            info = {
                "flight": flight,
                "origin": inst["origin"]["code_iata"],
                "dest": inst["destination"]["code_iata"],
                "off": off,
                "on": on,
                "status": inst.get("status"),
                "aircraft": inst.get("aircraft_type")
            }

            if off and not off_sent:
                msg = await channel.send(embed=takeoff_embed(info))
                bot.loop.create_task(delete_after(msg))
                cur.execute("UPDATE tracked_flights SET notified_off=1 WHERE id=?", (row_id,))
                db.commit()

            if on and not on_sent:
                msg = await channel.send(embed=landed_embed(info))
                bot.loop.create_task(delete_after(msg))
                cur.execute("DELETE FROM tracked_flights WHERE id=?", (row_id,))
                db.commit()

        await asyncio.sleep(POLL_FA_SECONDS)

async def status_update_loop():
    while True:
        r = requests.get(FR24_URL, headers={"User-Agent": UA})
        flights = parse_fr24(r.text) if r.status_code == 200 else []

        cur.execute("SELECT guild_id, status_channel, status_message FROM guild_settings")
        for gid, ch_id, msg_id in cur.fetchall():
            if not ch_id or not msg_id:
                continue
            guild = bot.get_guild(gid)
            if not guild:
                continue
            channel = guild.get_channel(ch_id)
            if not channel:
                continue

            next_info = None
            for d, f in flights:
                inst = get_fa_instance(f, d)
                if inst:
                    next_info = {
                        "flight": f,
                        "origin": inst["origin"]["code_iata"],
                        "dest": inst["destination"]["code_iata"],
                        "off": parse_iso(inst.get("scheduled_off")),
                        "status": inst.get("status"),
                        "aircraft": inst.get("aircraft_type")
                    }
                    break

            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=status_embed(next_info))
            except:
                pass

        await asyncio.sleep(STATUS_UPDATE_SECONDS)

# =============================
# Ready
# =============================
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    bot.loop.create_task(fr24_import_loop())
    bot.loop.create_task(flight_poll_loop())
    bot.loop.create_task(status_update_loop())

bot.run(DISCORD_TOKEN)
