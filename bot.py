import os
import sqlite3
import asyncio
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FA_API_KEY = os.getenv("FLIGHTAWARE_API_KEY")

FA_BASE = "https://aeroapi.flightaware.com/aeroapi"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Database ----------
db = sqlite3.connect("flights.db")
cur = db.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    channel_id INTEGER,
    flight_number TEXT,
    flight_date TEXT,
    notified_off INTEGER DEFAULT 0,
    notified_on INTEGER DEFAULT 0
)
""")
db.commit()

# ---------- Helpers ----------
def fa_headers():
    return {"x-apikey": FA_API_KEY}

def parse_time(ts):
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo("America/New_York"))

def nice_time(dt):
    return dt.strftime("%H:%M %Z")

# ---------- Discord ----------
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    bot.loop.create_task(poll_flights())

# ---------- Slash Commands ----------
@bot.tree.command(name="track", description="Track a flight by number and date")
@app_commands.describe(
    flight="Flight number (e.g. DL882)",
    date="Flight date (YYYY-MM-DD)"
)
async def track(interaction: discord.Interaction, flight: str, date: str):
    cur.execute(
        "INSERT INTO flights (guild_id, channel_id, flight_number, flight_date) VALUES (?, ?, ?, ?)",
        (interaction.guild_id, interaction.channel_id, flight.upper(), date)
    )
    db.commit()

    embed = discord.Embed(
        title="✈️ Flight Tracking Started",
        description=f"**{flight.upper()}** on **{date}**",
        color=discord.Color.blue()
    )
    embed.set_footer(text="Waiting for departure…")

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="cancel", description="Cancel flight tracking")
async def cancel(interaction: discord.Interaction):
    cur.execute(
        "DELETE FROM flights WHERE guild_id=? AND channel_id=?",
        (interaction.guild_id, interaction.channel_id)
    )
    db.commit()

    await interaction.response.send_message("🛑 Flight tracking cancelled.")

# ---------- Polling ----------
async def poll_flights():
    while True:
        cur.execute("SELECT * FROM flights")
        rows = cur.fetchall()

        for row in rows:
            (
                flight_id, guild_id, channel_id,
                flight_number, flight_date,
                notified_off, notified_on
            ) = row

            url = f"{FA_BASE}/flights/{flight_number}"
            params = {"start": flight_date, "max_pages": 1}
            r = requests.get(url, headers=fa_headers(), params=params)

            if r.status_code != 200:
                continue

            flights = r.json().get("flights", [])
            if not flights:
                continue

            f = flights[0]

            actual_off = f.get("actual_off")
            actual_on = f.get("actual_on")

            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            # Takeoff
            if actual_off and not notified_off:
                off_time = nice_time(parse_time(actual_off))
                embed = discord.Embed(
                    title="🛫 Takeoff",
                    description=f"**{flight_number}**",
                    color=discord.Color.green()
                )
                embed.add_field(name="Off Time", value=off_time, inline=False)
                embed.set_footer(text="Wheels up!")

                await channel.send(embed=embed)

                cur.execute(
                    "UPDATE flights SET notified_off=1 WHERE id=?",
                    (flight_id,)
                )
                db.commit()

            # Landing
            if actual_on and not notified_on:
                on_time = nice_time(parse_time(actual_on))
                embed = discord.Embed(
                    title="🛬 Landed",
                    description=f"**{flight_number}**",
                    color=discord.Color.orange()
                )
                embed.add_field(name="On Time", value=on_time, inline=False)
                embed.set_footer(text="Welcome back!")

                await channel.send(embed=embed)

                cur.execute("DELETE FROM flights WHERE id=?", (flight_id,))
                db.commit()

        await asyncio.sleep(180)

# ---------- Run ----------
bot.run(DISCORD_TOKEN)
