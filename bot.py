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
AUTO_DELETE_SECONDS = 1800

UA = "Mozilla/5.0"

# =============================
# Discord
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
    origin TEXT,
    dest TEXT,
    sched_dep TEXT,
    sched_arr TEXT,
    airline TEXT,
    aircraft TEXT,
    seat TEXT,
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
# FR24 Parsing (FULL DATA)
# =============================
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

def parse_fr24(html):
    soup = BeautifulSoup(html, "html.parser")
    flights = []

    for tr in soup.select("tr"):
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if len(tds) < 10 or not DATE_RE.match(tds[0]):
            continue

        flights.append({
            "date": tds[0],
            "flight": tds[1],
            "origin": tds[3],
            "dest": tds[4],
            "sched_dep": tds[6],
            "sched_arr": tds[7],
            "airline": tds[8],
            "aircraft": tds[9],
            "seat": tds[10] if len(tds) > 10 else None,
        })

    return flights

# =============================
# FlightAware (STRICT DATE MATCH)
# =============================
def get_fa_instance(flight_no, fr24_date):
    target_date = datetime.fromisoformat(fr24_date).date()
    now = datetime.now(tz=LOCAL_TZ)

    d = datetime.fromisoformat(fr24_date).replace(tzinfo=LOCAL_TZ)
    start = (d - timedelta(hours=12)).astimezone(ZoneInfo("UTC")).isoformat()
    end = (d + timedelta(hours=36)).astimezone(ZoneInfo("UTC")).isoformat()

    r = requests.get(
        f"{FA_BASE}/flights/{flight_no}",
        headers=fa_headers(),
        params={"start": start, "end": end, "max_pages": 1},
        timeout=20
    )
    if r.status_code != 200:
        return None

    flights = r.json().get("flights", [])
    candidates = []

    for f in flights:
        if f.get("actual_on"):
            continue

        off = parse_iso(f.get("scheduled_off")) or parse_iso(f.get("estimated_off"))
        if not off:
            continue

        off_local = off.astimezone(LOCAL_TZ)

        if off_local.date() != target_date:
            continue

        if (off_local - now).total_seconds() < -3600:
            continue

        candidates.append((off_local, f))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]

# =============================
# Embeds
# =============================
def status_embed(fr, fa):
    e = discord.Embed(
        title="🧭 Hana’s Next Flight",
        color=discord.Color.blurple()
    )

    e.description = f"**{fr['flight']} — {fr['origin']} → {fr['dest']}**"

    e.add_field(
        name="Scheduled",
        value=f"{fr['date']} · {fr['sched_dep']} → {fr['sched_arr']}",
        inline=False
    )

    e.add_field(name="Aircraft", value=fr["aircraft"], inline=True)
    e.add_field(name="Airline", value=fr["airline"], inline=True)

    if fr.get("seat"):
        e.add_field(name="Seat", value=fr["seat"], inline=True)

    if fa:
        off = parse_iso(fa.get("scheduled_off")) or parse_iso(fa.get("estimated_off"))
        e.add_field(name="Departs in", value=countdown(off), inline=True)
        e.add_field(name="Status", value=fa.get("status", "Scheduled"), inline=True)
        if fa.get("fa_flight_id"):
            e.add_field(
                name="Live map",
                value=f"[Open on FlightAware](https://flightaware.com/live/flight/id/{fa['fa_flight_id']})",
                inline=False
            )
    else:
        e.add_field(name="Status", value="Waiting for FlightAware data", inline=False)

    e.set_footer(text="This message updates automatically.")
    return e

def takeoff_embed(fr, fa):
    e = discord.Embed(title="🛫 Takeoff", color=discord.Color.green())
    e.description = f"**{fr['flight']} — {fr['origin']} → {fr['dest']}**"
    e.add_field(name="Off", value=fmt(parse_iso(fa.get("actual_off"))), inline=True)
    return e

def landed_embed(fr, fa):
    e = discord.Embed(title="🛬 Landed", color=discord.Color.orange())
    e.description = f"**{fr['flight']} — {fr['origin']} → {fr['dest']}**"
    e.add_field(name="On", value=fmt(parse_iso(fa.get("actual_on"))), inline=True)
    return e

# =============================
# Slash Commands
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
    await interaction.response.defer(ephemeral=True)

    # Create placeholder message
    msg = await interaction.channel.send("Initializing…")

    # Save message location
    cur.execute("""
        INSERT INTO guild_settings (guild_id, status_channel, status_message)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
        status_channel=excluded.status_channel,
        status_message=excluded.status_message
    """, (interaction.guild_id, interaction.channel_id, msg.id))
    db.commit()

    # 🔥 IMMEDIATE REFRESH 🔥
    try:
        r = requests.get(FR24_URL, headers={"User-Agent": UA}, timeout=20)
        flights = parse_fr24(r.text) if r.status_code == 200 else []

        today = date.today().isoformat()
        next_fr = None

        for f in flights:
            if f["date"] >= today:
                next_fr = f
                break

        if next_fr:
            fa = get_fa_instance(next_fr["flight"], next_fr["date"])
            await msg.edit(embed=status_embed(next_fr, fa))
        else:
            await msg.edit(embed=status_embed(None, None))

        await interaction.followup.send("✅ Status message created and refreshed instantly.", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Status message created, but refresh failed:\n`{e}`",
            ephemeral=True
        )

# =============================
# Background Tasks
# =============================
async def fr24_import_loop():
    while True:
        r = requests.get(FR24_URL, headers={"User-Agent": UA})
        flights = parse_fr24(r.text) if r.status_code == 200 else []

        today = date.today().isoformat()

        cur.execute("SELECT guild_id, autopost_channel FROM guild_settings")
        for gid, channel_id in cur.fetchall():
            if not channel_id:
                continue
            for f in flights:
                if f["date"] < today:
                    continue
                cur.execute("""
                    INSERT OR IGNORE INTO tracked_flights
                    (guild_id, channel_id, flight_no, flight_date, origin, dest,
                     sched_dep, sched_arr, airline, aircraft, seat)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    gid, channel_id,
                    f["flight"], f["date"], f["origin"], f["dest"],
                    f["sched_dep"], f["sched_arr"],
                    f["airline"], f["aircraft"], f["seat"]
                ))
                db.commit()

        await asyncio.sleep(POLL_FR24_SECONDS)

async def status_update_loop():
    while True:
        cur.execute("SELECT * FROM tracked_flights ORDER BY flight_date ASC LIMIT 1")
        row = cur.fetchone()

        cur.execute("SELECT guild_id, status_channel, status_message FROM guild_settings")
        for gid, ch_id, msg_id in cur.fetchall():
            if not row or not ch_id or not msg_id:
                continue

            fr = {
                "flight": row[3],
                "date": row[4],
                "origin": row[5],
                "dest": row[6],
                "sched_dep": row[7],
                "sched_arr": row[8],
                "airline": row[9],
                "aircraft": row[10],
                "seat": row[11],
            }

            fa = get_fa_instance(fr["flight"], fr["date"])

            guild = bot.get_guild(gid)
            if not guild:
                continue
            channel = guild.get_channel(ch_id)
            if not channel:
                continue

            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=status_embed(fr, fa))
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
    bot.loop.create_task(status_update_loop())

bot.run(DISCORD_TOKEN)

