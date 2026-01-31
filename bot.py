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
# OpenFlights databases
# =============================

def load_airports(path="airports.dat"):
    """
    OpenFlights airports.dat CSV:
    [0] Airport ID
    [1] Name
    [2] City
    [3] Country
    [4] IATA
    [5] ICAO
    ...
    """
    airports = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 6:
                continue
            name = row[1].strip()
            city = row[2].strip()
            country = row[3].strip()
            iata = row[4].strip().upper()
            if iata and iata != "\\N":
                airports[iata] = {"name": name, "city": city, "country": country}
    return airports

def load_airlines(path="airlines.dat"):
    """
    OpenFlights airlines.dat CSV:
    [0] Airline ID
    [1] Name
    [2] Alias
    [3] IATA
    [4] ICAO
    [5] Callsign
    [6] Country
    [7] Active
    """
    airlines_by_iata = {}
    airlines_by_icao = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 7:
                continue
            name = row[1].strip()
            iata = row[3].strip().upper() if row[3].strip() != "\\N" else ""
            icao = row[4].strip().upper() if row[4].strip() != "\\N" else ""
            country = row[6].strip()
            data = {"name": name, "country": country, "iata": iata, "icao": icao}
            if iata:
                airlines_by_iata[iata] = data
            if icao:
                airlines_by_icao[icao] = data
    return airlines_by_iata, airlines_by_icao

def load_planes(path="planes.dat"):
    """
    OpenFlights planes.dat CSV:
    [0] Name
    [1] IATA
    [2] ICAO
    """
    planes = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 3:
                continue
            name = row[0].strip()
            iata = row[1].strip().upper() if row[1].strip() != "\\N" else ""
            icao = row[2].strip().upper() if row[2].strip() != "\\N" else ""
            if icao:
                planes[icao] = name
            if iata:
                planes[iata] = name
    return planes

# Load global data (must exist as files next to bot.py)
AIRPORTS = load_airports("airports.dat")
AIRLINES_IATA, AIRLINES_ICAO = load_airlines("airlines.dat")
PLANES = load_planes("planes.dat")

def format_airport(code: str) -> str:
    if not code:
        return "—"
    code = code.upper()
    a = AIRPORTS.get(code)
    if not a:
        return code
    # "San Diego International Airport, San Diego, United States (SAN)"
    parts = [a["name"]]
    if a.get("city"):
        parts.append(a["city"])
    if a.get("country"):
        parts.append(a["country"])
    return f"{', '.join(parts)} ({code})"

def format_airline(code: str) -> str:
    """
    FR24 often shows ICAO airline codes (DAL, UAL, etc.).
    We try ICAO first, then IATA.
    """
    if not code:
        return "—"
    code = code.upper()

    a = AIRLINES_ICAO.get(code) or AIRLINES_IATA.get(code)
    if not a:
        return code

    # Prefer displaying the code you provided (DAL), but include country
    country = a.get("country") or ""
    if country:
        return f"{a['name']}, {country} ({code})"
    return f"{a['name']} ({code})"

def format_aircraft(code: str) -> str:
    if not code:
        return "—"
    code = code.upper()
    name = PLANES.get(code)
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
# FR24 Parsing (ALL COLUMNS)
# =============================
def parse_fr24(html):
    """
    Based on your FR24 table:
    DATE | FLIGHT | REG | FROM | TO | DIST | DEP | ARR | AIRLINE | AIRCRAFT | SEAT | ...
    """
    soup = BeautifulSoup(html, "html.parser")
    flights = []

    for tr in soup.select("tr"):
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if len(tds) < 11:
            continue

        flights.append({
            "date": tds[0],
            "flight": tds[1],
            "reg": tds[2] or None,
            "origin": tds[3],
            "dest": tds[4],
            "dist": tds[5] or None,
            "sched_dep": tds[6],
            "sched_arr": tds[7],
            "airline": tds[8],
            "aircraft": tds[9],
            "seat": tds[10] or None,
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
# FlightAware Match (STRICT FR24 DATE)
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
# Embed (EVERY BIT OF INFO)
# =============================
def status_embed(fr, fa, after=None):
    e = discord.Embed(title="🧭 Hana’s Next Flight", color=discord.Color.blurple())

    if not fr:
        e.description = "No upcoming flights detected."
        return e

    # Title line includes full airports
    e.description = (
        f"**{fr['flight']}**\n"
        f"{format_airport(fr['origin'])} → {format_airport(fr['dest'])}"
    )

    # Scheduled
    e.add_field(
        name="Scheduled",
        value=f"{fr['date']} · {fr['sched_dep']} → {fr['sched_arr']}",
        inline=False
    )

    # Everything from FR24
    e.add_field(name="Airline", value=format_airline(fr["airline"]), inline=True)
    e.add_field(name="Aircraft", value=format_aircraft(fr["aircraft"]), inline=True)

    if fr.get("reg"):
        e.add_field(name="Registration", value=fr["reg"], inline=True)
    if fr.get("seat"):
        e.add_field(name="Seat", value=fr["seat"], inline=True)
    if fr.get("dist"):
        e.add_field(name="Distance", value=f"{fr['dist']}", inline=True)

    # FlightAware enrichment
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

    # After that (full info too)
    if after:
        after_lines = [
            f"**{after['flight']}**",
            f"{format_airport(after['origin'])} → {format_airport(after['dest'])}",
            f"{after['date']} · {after['sched_dep']} → {after['sched_arr']}",
            f"Airline: {format_airline(after['airline'])}",
            f"Aircraft: {format_aircraft(after['aircraft'])}",
        ]
        if after.get("reg"):
            after_lines.append(f"Registration: {after['reg']}")
        if after.get("seat"):
            after_lines.append(f"Seat: {after['seat']}")
        if after.get("dist"):
            after_lines.append(f"Distance: {after['dist']}")

        e.add_field(name="🗓️ After that", value="\n".join(after_lines), inline=False)

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
