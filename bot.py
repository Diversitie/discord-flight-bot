import os
import csv
import asyncio
import sqlite3
from datetime import datetime, date, timedelta
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
    raise RuntimeError("Missing required environment variables")

FA_BASE = "https://aeroapi.flightaware.com/aeroapi"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
STATUS_UPDATE_SECONDS = 120
UA = "Mozilla/5.0"

# =============================
# Airport database (GLOBAL)
# =============================
def load_airports(path="airports.dat"):
    airports = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 5:
                continue
            name = row[1].strip()
            iata = row[4].strip().upper()
            if iata and iata != "\\N":
                airports[iata] = name
    return airports

AIRPORTS = load_airports()

def format_airport(code):
    if not code:
        return "—"
    code = code.upper()
    name = AIRPORTS.get(code)
    return f"{name} ({code})" if name else code

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
    status_channel INTEGER,
    status_message INTEGER
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

def countdown(dt):
    if not dt:
        return "—"
    delta = dt.astimezone(LOCAL_TZ) - datetime.now(tz=LOCAL_TZ)
    mins = max(0, int(delta.total_seconds() // 60))
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"

# =============================
# FR24 Parsing
# =============================
def parse_fr24(html):
    soup = BeautifulSoup(html, "html.parser")
    flights = []

    for tr in soup.select("tr"):
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if len(tds) < 10:
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
# Ordering Logic (DATE + TIME)
# =============================
def get_next_two_flights(flights):
    def flight_dt(f):
        try:
            return datetime.fromisoformat(f"{f['date']} {f['sched_dep']}")
        except Exception:
            return datetime.max

    today = date.today().isoformat()
    future = [f for f in flights if f["date"] >= today]

    if not future:
        return None, None

    future.sort(key=flight_dt)
    return future[0], (future[1] if len(future) > 1 else None)

# =============================
# FlightAware (STRICT DATE MATCH)
# =============================
def get_fa_instance(flight_no, fr24_date):
    target = datetime.fromisoformat(fr24_date).date()
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

    candidates = []
    for f in r.json().get("flights", []):
        if f.get("actual_on"):
            continue

        off = parse_iso(f.get("scheduled_off")) or parse_iso(f.get("estimated_off"))
        if not off:
            continue

        off_local = off.astimezone(LOCAL_TZ)
        if off_local.date() != target:
            continue

        candidates.append((off_local, f))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]

# =============================
# Embed
# =============================
def status_embed(fr, fa, after=None):
    e = discord.Embed(title="🧭 Hana’s Next Flight", color=discord.Color.blurple())

    if not fr:
        e.description = "No upcoming flights detected."
        return e

    e.description = (
        f"**{fr['flight']} — "
        f"{format_airport(fr['origin'])} → "
        f"{format_airport(fr['dest'])}**"
    )

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
        e.add_field(
            name="Status",
            value="📅 **Flight confirmed**\n⏳ Timing pending from FlightAware",
            inline=False
        )

    if after:
        e.add_field(
            name="🗓️ After that",
            value=(
                f"**{after['flight']} — "
                f"{format_airport(after['origin'])} → "
                f"{format_airport(after['dest'])}**\n"
                f"{after['date']} · {after['sched_dep']} → {after['sched_arr']}"
            ),
            inline=False
        )

    e.set_footer(text="This message updates automatically.")
    return e

# =============================
# Commands
# =============================
@bot.tree.command(name="set_status_message")
async def set_status_message(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    msg = await interaction.channel.send("Updating…")

    cur.execute("""
        INSERT INTO guild_settings (guild_id, status_channel, status_message)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
        status_channel=excluded.status_channel,
        status_message=excluded.status_message
    """, (interaction.guild_id, interaction.channel_id, msg.id))
    db.commit()

    try:
        await msg.pin()
    except:
        pass

    await refresh_status_internal(interaction.guild_id)
    await interaction.followup.send("✅ Status message created and refreshed.", ephemeral=True)

@bot.tree.command(name="refresh_status")
async def refresh_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await refresh_status_internal(interaction.guild_id)
    await interaction.followup.send("🔄 Status refreshed.", ephemeral=True)

# =============================
# Core Refresh
# =============================
async def refresh_status_internal(guild_id):
    cur.execute("SELECT status_channel, status_message FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    if not row:
        return

    ch_id, msg_id = row
    guild = bot.get_guild(guild_id)
    if not guild:
        return

    channel = guild.get_channel(ch_id)
    if not channel:
        return

    msg = await channel.fetch_message(msg_id)

    r = requests.get(FR24_URL, headers={"User-Agent": UA}, timeout=20)
    flights = parse_fr24(r.text) if r.status_code == 200 else []

    next_flight, after = get_next_two_flights(flights)
    fa = get_fa_instance(next_flight["flight"], next_flight["date"]) if next_flight else None

    await msg.edit(embed=status_embed(next_flight, fa, after))

# =============================
# Background Loop
# =============================
async def status_loop():
    while True:
        cur.execute("SELECT guild_id FROM guild_settings")
        for (gid,) in cur.fetchall():
            try:
                await refresh_status_internal(gid)
            except:
                pass
        await asyncio.sleep(STATUS_UPDATE_SECONDS)

# =============================
# Ready
# =============================
@bot.event
async def on_ready():
    await bot.tree.sync()
    bot.loop.create_task(status_loop())
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_TOKEN)
