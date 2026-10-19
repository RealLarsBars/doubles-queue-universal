"""Doubles Queue Bot: a simple matchmaking queue for any Discord server.

Players press a button to join a queue. The bot pings the server's queue role when someone joins, announces the
match when the queue fills, and removes anyone who doesn't answer the "still there?" check-in. Each player can set
quiet hours: while they're active, the bot takes the queue role away so queue pings don't reach them, and gives it
back afterwards.

Every server configures itself with /setup, so nothing in this file needs editing. See docs/SETUP.md.
"""

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import discord
from discord import app_commands
from discord.ext import tasks

# ---------------------------------------------------------------------------
# CONSTANTS (per-server settings live in /setup, not here)
# ---------------------------------------------------------------------------

STATE_FILE = Path(os.environ.get("QUEUE_STATE_FILE", "queue_state.json"))
SWEEP_INTERVAL_SECONDS = 30
MAX_MODES = 5            # one button row on the panel
MAX_MODE_NAME = 40
MAX_QUIET_RULES = 10
MATCH_LOG_SECONDS = 86400

DEFAULT_MODES = {"Doubles": 4}
DEFAULT_TIMEZONE = "UTC"
DEFAULT_JOIN_MINUTES = 35
DEFAULT_RENEW_MINUTES = 30
DEFAULT_WARNING_MINUTES = 5

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DAY_OPTIONS = [
    ("Every day", [0, 1, 2, 3, 4, 5, 6]),
    ("Weekdays (Mon-Fri)", [0, 1, 2, 3, 4]),
    ("Weekends (Sat-Sun)", [5, 6]),
    ("Mondays", [0]),
    ("Tuesdays", [1]),
    ("Wednesdays", [2]),
    ("Thursdays", [3]),
    ("Fridays", [4]),
    ("Saturdays", [5]),
    ("Sundays", [6]),
]

# Times are "HH:MM" in the player's own timezone. An end of "24:00" means midnight at the end of the day.
TIME_OPTIONS = [
    ("Work hours (9 AM - 5 PM)", "09:00", "17:00"),
    ("Daytime (8 AM - 6 PM)", "08:00", "18:00"),
    ("Evening (5 PM - 11 PM)", "17:00", "23:00"),
    ("Night (10 PM - 7 AM)", "22:00", "07:00"),
    ("Late night (12 AM - 8 AM)", "00:00", "08:00"),
    ("All day", "00:00", "24:00"),
]

# Offered when a player picks their timezone; anything else can be typed in.
COMMON_TIMEZONES = [
    ("Pacific (Los Angeles)", "America/Los_Angeles"),
    ("Mountain (Denver)", "America/Denver"),
    ("Arizona (Phoenix)", "America/Phoenix"),
    ("Central (Chicago)", "America/Chicago"),
    ("Eastern (New York)", "America/New_York"),
    ("Atlantic (Halifax)", "America/Halifax"),
    ("Alaska (Anchorage)", "America/Anchorage"),
    ("Hawaii (Honolulu)", "Pacific/Honolulu"),
    ("Mexico City", "America/Mexico_City"),
    ("Brazil (Sao Paulo)", "America/Sao_Paulo"),
    ("UK & Ireland (London)", "Europe/London"),
    ("Central Europe (Paris, Berlin)", "Europe/Paris"),
    ("Eastern Europe (Helsinki, Athens)", "Europe/Helsinki"),
    ("Moscow", "Europe/Moscow"),
    ("Gulf (Dubai)", "Asia/Dubai"),
    ("India (Kolkata)", "Asia/Kolkata"),
    ("Singapore, Manila, Perth", "Asia/Singapore"),
    ("Japan & Korea (Tokyo)", "Asia/Tokyo"),
    ("Eastern Australia (Sydney)", "Australia/Sydney"),
    ("New Zealand (Auckland)", "Pacific/Auckland"),
    ("UTC", "UTC"),
]

# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------


@dataclass
class QueueEntry:
    user_id: int
    display_name: str
    joined_at: float
    expires_at: float
    prompted: bool = False
    prompt_message_id: Optional[int] = None
    prompt_channel_id: Optional[int] = None


@dataclass
class GuildState:
    # Settings, changed with /setup
    ping_role_id: Optional[int] = None
    ping_channel_id: Optional[int] = None
    timezone: str = DEFAULT_TIMEZONE
    modes: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MODES))
    join_minutes: int = DEFAULT_JOIN_MINUTES
    renew_minutes: int = DEFAULT_RENEW_MINUTES
    warning_minutes: int = DEFAULT_WARNING_MINUTES

    # Live data
    queues: Dict[str, List[QueueEntry]] = field(default_factory=dict)
    match_log: List[float] = field(default_factory=list)
    panel_message: Optional[Dict[str, int]] = None
    quiet_board_message: Optional[Dict[str, int]] = None
    quiet_hours: Dict[int, List[dict]] = field(default_factory=dict)  # user id -> [{"days", "start", "end"}]
    user_timezones: Dict[int, str] = field(default_factory=dict)  # user id -> timezone their quiet hours use
    quiet_active: List[int] = field(default_factory=list)  # users whose quiet hours were active at the last check
    quiet_muted: List[int] = field(default_factory=list)   # users whose ping role the bot took away for quiet hours

    def queue(self, mode: str) -> List[QueueEntry]:
        return self.queues.setdefault(mode, [])

    def to_json(self) -> dict:
        data = asdict(self)
        data["quiet_hours"] = {str(k): v for k, v in self.quiet_hours.items()}
        data["user_timezones"] = {str(k): v for k, v in self.user_timezones.items()}
        return data

    @classmethod
    def from_json(cls, data: dict) -> "GuildState":
        g = cls()
        for name in ("ping_role_id", "ping_channel_id", "timezone", "modes", "join_minutes", "renew_minutes",
                     "warning_minutes", "match_log", "panel_message", "quiet_board_message",
                     "quiet_active", "quiet_muted"):
            if name in data:
                setattr(g, name, data[name])
        g.queues = {mode: [QueueEntry(**e) for e in entries] for mode, entries in data.get("queues", {}).items()}
        g.quiet_hours = {int(k): v for k, v in data.get("quiet_hours", {}).items()}
        g.user_timezones = {int(k): v for k, v in data.get("user_timezones", {}).items()}
        return g


guilds: Dict[int, GuildState] = {}


def gs(guild_id: int) -> GuildState:
    """The state for one server, created with default settings the first time it's needed."""
    if guild_id not in guilds:
        guilds[guild_id] = GuildState()
    return guilds[guild_id]


def write_json_atomic(path: Path, data) -> None:
    """Write to a temp file and swap it in, so a crash mid-save can never leave a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_state() -> Dict[int, GuildState]:
    """Load the saved state, falling back to the backup if the main file is damaged."""
    backup = STATE_FILE.with_name(STATE_FILE.name + ".bak")
    for path in (STATE_FILE, backup):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text())
            loaded = {int(gid): GuildState.from_json(g) for gid, g in raw.get("guilds", {}).items()}
        except Exception as e:
            print(f"Could not read {path}: {e}")
            continue
        if path is backup:
            print(f"Recovered state from {backup}.")
        return loaded
    if STATE_FILE.exists() or backup.exists():
        raise SystemExit(f"{STATE_FILE} exists but can't be read, and neither can its backup. "
                         "Refusing to start with empty state. Fix or move the file, then restart.")
    return {}


_last_backup = 0.0


def save_state() -> None:
    global _last_backup
    data = {"version": 1, "guilds": {str(gid): g.to_json() for gid, g in guilds.items()}}
    write_json_atomic(STATE_FILE, data)
    if time.time() - _last_backup > 600:  # keep a known-good copy, refreshed every 10 minutes
        _last_backup = time.time()
        try:
            write_json_atomic(STATE_FILE.with_name(STATE_FILE.name + ".bak"), data)
        except OSError as e:
            print(f"Could not write backup: {e}")


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def user_tz_name(g: GuildState, user_id: int) -> str:
    """The player's own timezone, or the server's if they haven't picked one."""
    return g.user_timezones.get(user_id, g.timezone)


def local_clock(tz_name: str) -> str:
    return datetime.now(zone(tz_name)).strftime("%I:%M %p").lstrip("0")


ALL_TIMEZONES = sorted(available_timezones())


def find_timezone(text: str) -> Optional[str]:
    """Match what someone typed ("America/Chicago", "chicago", "new york") to a timezone name."""
    wanted = "_".join(text.strip().split()).lower()
    if not wanted:
        return None
    for tz in ALL_TIMEZONES:
        if tz.lower() == wanted:
            return tz
    for tz in ALL_TIMEZONES:  # a city: the last part of the name
        if "/" in tz and tz.rsplit("/", 1)[1].lower() == wanted:
            return tz
    if wanted in ("utc", "gmt"):
        return "UTC"
    return None


def clean(name: str) -> str:
    """A display name that's safe to drop into a message (no markdown tricks, no mentions)."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(name))


def find_user_mode(g: GuildState, user_id: int) -> Optional[str]:
    for mode, entries in g.queues.items():
        if any(e.user_id == user_id for e in entries):
            return mode
    return None


def find_entry(g: GuildState, mode: str, user_id: int) -> Optional[QueueEntry]:
    return next((e for e in g.queues.get(mode, []) if e.user_id == user_id), None)


def remove_entry(g: GuildState, mode: str, user_id: int) -> None:
    g.queues[mode] = [e for e in g.queues.get(mode, []) if e.user_id != user_id]


def total_queued(g: GuildState) -> int:
    return sum(len(g.queues.get(mode, [])) for mode in g.modes)


def matches_last_24h(g: GuildState) -> int:
    cutoff = time.time() - MATCH_LOG_SECONDS
    return sum(1 for t in g.match_log if t >= cutoff)


CLOCK_RE = re.compile(r"^\s*(\d{1,2})(?::?(\d{2}))?\s*([ap])?\.?\s*m?\.?\s*$", re.IGNORECASE)


def parse_clock(text: str, allow_24: bool = False) -> Optional[str]:
    """Turn "22:00", "9pm", "9:30 am" or "0730" into "HH:MM". Returns None if it isn't a time."""
    m = CLOCK_RE.match(text or "")
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "p" else 0)
    if hour == 24 and minute == 0 and allow_24:
        return "24:00"
    if hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def format_clock(hm: str) -> str:
    if hm == "24:00":
        return "midnight"
    hour, minute = map(int, hm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def in_quiet_hours(rules: List[dict], now_local: datetime) -> bool:
    """True if any rule covers this moment. A rule that crosses midnight (10 PM - 7 AM) belongs to the day it
    starts on, so a Friday night rule also covers early Saturday morning."""
    weekday = now_local.weekday()
    yesterday = (weekday - 1) % 7
    now_hm = now_local.strftime("%H:%M")
    for rule in rules:
        start, end, days = rule["start"], rule["end"], rule["days"]
        if start < end:
            if weekday in days and start <= now_hm < end:
                return True
        else:
            if weekday in days and now_hm >= start:
                return True
            if yesterday in days and now_hm < end:
                return True
    return False


def user_in_quiet_hours(g: GuildState, user_id: int) -> bool:
    return in_quiet_hours(g.quiet_hours.get(user_id, []), datetime.now(zone(user_tz_name(g, user_id))))


def describe_rule(rule: dict) -> str:
    days = next((label for label, d in DAY_OPTIONS if sorted(d) == sorted(rule["days"]) and len(d) > 1), None)
    days = days or ", ".join(DAY_NAMES[d] for d in sorted(rule["days"]))
    return f"{days}, {format_clock(rule['start'])} to {format_clock(rule['end'])}"


def notify_channel(g: GuildState, fallback=None):
    """Where queue pings, check-ins and match announcements go: the ping channel, else the panel's channel."""
    channel = bot.get_channel(g.ping_channel_id) if g.ping_channel_id else None
    if channel is None and g.panel_message:
        channel = bot.get_channel(g.panel_message["channel_id"])
    return channel or fallback


async def safe_send(channel, content: str, **kwargs) -> Optional[discord.Message]:
    if channel is None:
        return None
    try:
        return await channel.send(content, **kwargs)
    except discord.HTTPException as e:
        print(f"Could not send to #{getattr(channel, 'name', channel)}: {e}")
        return None


async def delete_prompt(entry: QueueEntry) -> None:
    if entry.prompt_message_id and entry.prompt_channel_id:
        channel = bot.get_channel(entry.prompt_channel_id)
        if channel is not None:
            try:
                await channel.get_partial_message(entry.prompt_message_id).delete()
            except discord.HTTPException:
                pass
    entry.prompt_message_id = None
    entry.prompt_channel_id = None


# ---------------------------------------------------------------------------
# QUIET HOURS: TAKING THE PING ROLE AWAY AND GIVING IT BACK
# ---------------------------------------------------------------------------


async def sync_quiet_role(guild: discord.Guild, g: GuildState, member: discord.Member) -> None:
    """Take the ping role away while the member's quiet hours are on, and give it back when they end. The role
    only comes back to people the bot took it from, so someone who turned pings off stays off."""
    uid = member.id
    quiet = user_in_quiet_hours(g, uid)
    if quiet and uid not in g.quiet_active:
        g.quiet_active.append(uid)
    elif not quiet and uid in g.quiet_active:
        g.quiet_active.remove(uid)

    role = guild.get_role(g.ping_role_id) if g.ping_role_id else None
    if role is None:
        return
    try:
        if quiet and role in member.roles:
            await member.remove_roles(role, reason="Quiet hours started")
            if uid not in g.quiet_muted:
                g.quiet_muted.append(uid)
        elif not quiet and uid in g.quiet_muted:
            if role not in member.roles:
                await member.add_roles(role, reason="Quiet hours ended")
            g.quiet_muted.remove(uid)
    except discord.HTTPException as e:
        print(f"[{guild.name}] Could not change {role.name} for {member}: {e}")


# ---------------------------------------------------------------------------
# EMBEDS
# ---------------------------------------------------------------------------


def build_panel_embed(g: GuildState) -> discord.Embed:
    embed = discord.Embed(
        title="Matchmaking Queue",
        description=(
            "Press a button below to join a queue.\n"
            f"**{total_queued(g)}** player(s) in queue · **{matches_last_24h(g)}** match(es) in the last 24 hours"
        ),
        color=discord.Color.blurple(),
    )
    for mode, needed in g.modes.items():
        entries = g.queues.get(mode, [])
        names = ", ".join(clean(e.display_name) for e in entries)
        value = f"**{len(entries)}/{needed}** · {names}" if entries else f"0/{needed}"
        embed.add_field(name=mode, value=value, inline=False)
    embed.set_footer(text=(
        f"You stay in queue for {g.join_minutes} minutes. {g.warning_minutes} minutes before that runs out you'll "
        f"get a check-in, and if you don't answer it you're removed from the queue."
    ))
    return embed


def build_quiet_embed(g: GuildState, user_id: int, note: str = "") -> discord.Embed:
    rules = g.quiet_hours.get(user_id, [])
    current = "\n".join(f"**{i}.** {describe_rule(r)}" for i, r in enumerate(rules, 1)) or "None yet."
    status = "🌙 Quiet hours are **on** right now." if user_in_quiet_hours(g, user_id) else ""
    tz_name = user_tz_name(g, user_id)
    tz_line = (f"Your timezone: **{tz_name}** (it's {local_clock(tz_name)} there now)."
               if user_id in g.user_timezones else
               f"You haven't picked a timezone, so the server's is used: **{tz_name}** "
               f"(it's {local_clock(tz_name)} there now). Press **Timezone** to pick yours.")
    description = (
        f"{note}\n\n" if note else ""
    ) + (
        "While your quiet hours are on, you won't get queue pings.\n"
        f"{tz_line}\n\n"
        f"**Your quiet hours**\n{current}\n{status}"
    )
    return discord.Embed(title="🌙 Quiet Hours", description=description, color=discord.Color.dark_purple())


def build_quiet_board_embed(guild: Optional[discord.Guild], g: GuildState) -> discord.Embed:
    lines = []
    for uid, rules in g.quiet_hours.items():
        if not rules:
            continue
        member = guild.get_member(uid) if guild else None
        name = clean(member.display_name) if member else f"<@{uid}>"
        lines.append(f"• **{name}**: " + " | ".join(describe_rule(r) for r in rules) + f" ({user_tz_name(g, uid)})")
    return discord.Embed(
        title="🌙 Everyone's Quiet Hours",
        description="\n".join(lines) or "Nobody has set quiet hours yet.",
        color=discord.Color.dark_purple(),
    )


async def refresh_panel(guild_id: int) -> None:
    g = gs(guild_id)
    if not g.panel_message:
        return
    channel = bot.get_channel(g.panel_message["channel_id"])
    if channel is None:
        return
    try:
        msg = channel.get_partial_message(g.panel_message["message_id"])
        await msg.edit(embed=build_panel_embed(g), view=QueuePanelView(g))
    except discord.NotFound:
        g.panel_message = None
        save_state()
    except discord.HTTPException as e:
        print(f"Could not refresh the queue panel in {guild_id}: {e}")


async def refresh_quiet_board(guild: discord.Guild) -> None:
    g = gs(guild.id)
    if not g.quiet_board_message:
        return
    channel = bot.get_channel(g.quiet_board_message["channel_id"])
    if channel is None:
        return
    try:
        msg = channel.get_partial_message(g.quiet_board_message["message_id"])
        await msg.edit(embed=build_quiet_board_embed(guild, g))
    except discord.NotFound:
        g.quiet_board_message = None
        save_state()
    except discord.HTTPException as e:
        print(f"Could not refresh the quiet hours board in {guild.id}: {e}")


# ---------------------------------------------------------------------------
# QUEUE PANEL BUTTONS
# ---------------------------------------------------------------------------


class JoinQueueButton(discord.ui.DynamicItem[discord.ui.Button], template=r"queue:join:(?P<mode>.+)"):
    """One per mode. It's a dynamic item so every server's own mode names keep working after a restart."""

    def __init__(self, mode: str):
        super().__init__(discord.ui.Button(
            label=mode, style=discord.ButtonStyle.primary, custom_id=f"queue:join:{mode}", row=0))
        self.mode = mode

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["mode"])

    async def callback(self, interaction: discord.Interaction):
        g = gs(interaction.guild_id)
        mode, user = self.mode, interaction.user
        if mode not in g.modes:
            await interaction.response.send_message("That queue doesn't exist anymore.", ephemeral=True)
            return
        current = find_user_mode(g, user.id)
        if current == mode:
            await interaction.response.send_message("You're already in that queue.", ephemeral=True)
            return

        # Update the queue before anything is awaited, so two people joining at once can't both be "first".
        moved_from = find_entry(g, current, user.id) if current else None
        if current:
            remove_entry(g, current, user.id)
        now = time.time()
        queue = g.queue(mode)
        queue.append(QueueEntry(user.id, user.display_name, now, now + g.join_minutes * 60))
        count, needed = len(queue), g.modes[mode]
        matched: List[QueueEntry] = []
        if count >= needed:
            matched = queue[:needed]
            del queue[:needed]
            g.match_log = [t for t in g.match_log if t >= now - MATCH_LOG_SECONDS] + [now]
        save_state()

        await interaction.response.edit_message(embed=build_panel_embed(g), view=QueuePanelView(g))
        if moved_from:
            await delete_prompt(moved_from)

        channel = notify_channel(g, fallback=interaction.channel)
        panel_link = ""
        if g.panel_message and channel is not None and g.panel_message["channel_id"] != channel.id:
            panel_link = f" Join in <#{g.panel_message['channel_id']}>."
        role_ping = f"<@&{g.ping_role_id}> " if g.ping_role_id else ""
        name = clean(user.display_name)

        if matched:
            for e in matched:
                await delete_prompt(e)
            save_state()
            mentions = " ".join(f"<@{e.user_id}>" for e in matched)
            await safe_send(channel, f"🎮 Match found for **{mode}**! {mentions}")
        elif count == 1:
            await safe_send(channel, f"{role_ping}**{name}** joined the **{mode}** queue!{panel_link}")
        elif count == needed - 1:
            names = ", ".join(clean(e.display_name) for e in queue)
            await safe_send(channel, f"{role_ping}**{names}** are in the **{mode}** queue. Just need one more!"
                                     f"{panel_link}")
        else:
            await safe_send(channel, f"**{name}** joined the **{mode}** queue ({count}/{needed}).")


class LeaveQueueButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Leave Queue", style=discord.ButtonStyle.danger, custom_id="queue:leave", row=1)

    async def callback(self, interaction: discord.Interaction):
        g = gs(interaction.guild_id)
        current = find_user_mode(g, interaction.user.id)
        if current is None:
            await interaction.response.send_message("You're not in a queue.", ephemeral=True)
            return
        entry = find_entry(g, current, interaction.user.id)
        remove_entry(g, current, interaction.user.id)
        save_state()
        await interaction.response.edit_message(embed=build_panel_embed(g), view=QueuePanelView(g))
        if entry:
            await delete_prompt(entry)
            save_state()


class PingsButton(discord.ui.Button):
    """Turns the server's ping role on or off for whoever presses it."""

    def __init__(self):
        super().__init__(label="Pings On/Off", style=discord.ButtonStyle.secondary, emoji="🔔",
                         custom_id="queue:pings", row=1)

    async def callback(self, interaction: discord.Interaction):
        g = gs(interaction.guild_id)
        member = interaction.user
        role = interaction.guild.get_role(g.ping_role_id) if g.ping_role_id else None
        if role is None:
            await interaction.response.send_message(
                "This server hasn't picked a ping role yet. An admin can set one with `/setup ping-role`.",
                ephemeral=True)
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Turned queue pings off")
                reply = f"🔕 Pings are off. You won't get {role.mention} pings."
            elif member.id in g.quiet_muted:
                g.quiet_muted.remove(member.id)
                reply = "🔕 Pings are off. They won't come back when your quiet hours end."
            elif user_in_quiet_hours(g, member.id):
                g.quiet_muted.append(member.id)
                reply = f"🔔 Pings are on. You're in quiet hours right now, so {role.mention} pings start when they end."
            else:
                await member.add_roles(role, reason="Turned queue pings on")
                reply = f"🔔 Pings are on. You'll get {role.mention} pings when people join the queue."
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I can't hand out {role.mention}. An admin needs to give me Manage Roles and drag my role above it.",
                ephemeral=True)
            return
        save_state()
        await interaction.response.send_message(reply, ephemeral=True)


class QuietHoursButton(discord.ui.Button):
    def __init__(self, custom_id: str = "queue:quiet", label: str = "Quiet Hours", row: Optional[int] = 1):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, emoji="🌙", custom_id=custom_id, row=row)

    async def callback(self, interaction: discord.Interaction):
        g = gs(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_quiet_embed(g, interaction.user.id), view=QuietHoursMenu(g, interaction.user.id), ephemeral=True)


class QueuePanelView(discord.ui.View):
    """The queue panel. Built without a server's state, it holds only the fixed buttons, which is how they're
    registered at startup."""

    def __init__(self, g: Optional[GuildState] = None):
        super().__init__(timeout=None)
        if g is not None:
            for mode in g.modes:
                self.add_item(JoinQueueButton(mode))
        self.add_item(LeaveQueueButton())
        self.add_item(PingsButton())
        self.add_item(QuietHoursButton())


class QuietHoursPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(QuietHoursButton(custom_id="quiet:open", label="Set My Quiet Hours", row=None))


class CheckInView(discord.ui.View):
    """The "still there?" check-in. One copy is registered at startup; it finds the queue entry by the check-in's
    message id, so check-ins posted before a restart keep working."""

    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    def entry_for(interaction: discord.Interaction) -> Optional[tuple]:
        g = gs(interaction.guild_id)
        for mode, entries in g.queues.items():
            for e in entries:
                if e.prompt_message_id == interaction.message.id:
                    return g, mode, e
        return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        found = self.entry_for(interaction)
        if found is None:
            await interaction.response.edit_message(content="This player isn't in the queue anymore.", view=None)
            return False
        if interaction.user.id != found[2].user_id:
            await interaction.response.send_message("This check-in isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Stay in Queue", style=discord.ButtonStyle.success, custom_id="checkin:stay")
    async def stay(self, interaction: discord.Interaction, button: discord.ui.Button):
        g, mode, entry = self.entry_for(interaction)
        entry.expires_at = time.time() + g.renew_minutes * 60
        entry.prompted = False
        entry.prompt_message_id = entry.prompt_channel_id = None
        save_state()
        await interaction.response.edit_message(
            content=f"👍 {interaction.user.mention} is staying in the **{mode}** queue for another "
                    f"{g.renew_minutes} minutes.", view=None)

    @discord.ui.button(label="Leave Queue", style=discord.ButtonStyle.danger, custom_id="checkin:leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        g, mode, entry = self.entry_for(interaction)
        remove_entry(g, mode, entry.user_id)
        save_state()
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        await refresh_panel(interaction.guild_id)


CHECK_IN_VIEW: Optional[CheckInView] = None  # made in setup_hook, once the event loop is running


# ---------------------------------------------------------------------------
# QUIET HOURS MENU
# ---------------------------------------------------------------------------


async def add_quiet_rule(interaction: discord.Interaction, days: List[int], start: str, end: str) -> None:
    g = gs(interaction.guild_id)
    rules = g.quiet_hours.setdefault(interaction.user.id, [])
    if len(rules) >= MAX_QUIET_RULES:
        note = f"⚠️ You already have {MAX_QUIET_RULES} schedules. Remove one first."
    else:
        rules.append({"days": days, "start": start, "end": end})
        await sync_quiet_role(interaction.guild, g, interaction.user)
        save_state()
        await refresh_quiet_board(interaction.guild)
        note = "✅ Schedule added."
    await interaction.response.edit_message(
        embed=build_quiet_embed(g, interaction.user.id, note), view=QuietHoursMenu(g, interaction.user.id))


class QuietHoursMenu(discord.ui.View):
    def __init__(self, g: GuildState, user_id: int):
        super().__init__(timeout=900)
        rules = g.quiet_hours.get(user_id, [])
        if rules:
            self.add_item(RemoveRuleSelect(rules))

    @discord.ui.button(label="Add a Schedule", style=discord.ButtonStyle.primary, emoji="➕", row=1)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = gs(interaction.guild_id)
        if interaction.user.id not in g.user_timezones:  # ask once, before their first schedule
            await show_timezone_picker(interaction, then_add=True)
        else:
            await show_day_picker(interaction)

    @discord.ui.button(label="Timezone", style=discord.ButtonStyle.secondary, emoji="🌍", row=1)
    async def change_timezone(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_timezone_picker(interaction, then_add=False)

    @discord.ui.button(label="Clear All", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = gs(interaction.guild_id)
        g.quiet_hours.pop(interaction.user.id, None)
        await sync_quiet_role(interaction.guild, g, interaction.user)
        save_state()
        await refresh_quiet_board(interaction.guild)
        await interaction.response.edit_message(
            embed=build_quiet_embed(g, interaction.user.id, "🗑️ All quiet hours cleared. You'll get pings any time."),
            view=QuietHoursMenu(g, interaction.user.id))


class RemoveRuleSelect(discord.ui.Select):
    def __init__(self, rules: List[dict]):
        options = [discord.SelectOption(label=f"{i}. {describe_rule(r)}"[:100], value=str(i - 1))
                   for i, r in enumerate(rules, 1)]
        super().__init__(placeholder="Remove one schedule...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        g = gs(interaction.guild_id)
        rules = g.quiet_hours.get(interaction.user.id, [])
        index = int(self.values[0])
        if index < len(rules):
            rules.pop(index)
        if not rules:
            g.quiet_hours.pop(interaction.user.id, None)
        await sync_quiet_role(interaction.guild, g, interaction.user)
        save_state()
        await refresh_quiet_board(interaction.guild)
        await interaction.response.edit_message(
            embed=build_quiet_embed(g, interaction.user.id, "🗑️ Schedule removed."),
            view=QuietHoursMenu(g, interaction.user.id))


async def show_day_picker(interaction: discord.Interaction) -> None:
    view = discord.ui.View(timeout=900)
    view.add_item(DaySelect())
    await interaction.response.edit_message(
        embed=discord.Embed(title="🌙 Quiet Hours", description="**Step 1 of 2:** which days?",
                            color=discord.Color.dark_purple()), view=view)


async def show_timezone_picker(interaction: discord.Interaction, then_add: bool) -> None:
    g = gs(interaction.guild_id)
    view = discord.ui.View(timeout=900)
    view.add_item(TimezoneSelect(g, then_add))
    await interaction.response.edit_message(
        embed=discord.Embed(
            title="🌍 Your Timezone",
            description="Which timezone are you in? Your quiet hours follow your own clock.\n"
                        "Each option shows the time there right now. Not listed? Pick **Somewhere else** and type it.",
            color=discord.Color.dark_purple()),
        view=view)


async def set_user_timezone(interaction: discord.Interaction, tz_name: str, then_add: bool) -> None:
    g = gs(interaction.guild_id)
    g.user_timezones[interaction.user.id] = tz_name
    await sync_quiet_role(interaction.guild, g, interaction.user)
    save_state()
    await refresh_quiet_board(interaction.guild)
    if then_add:
        await show_day_picker(interaction)
    else:
        await interaction.response.edit_message(
            embed=build_quiet_embed(g, interaction.user.id, f"🌍 Timezone set to **{tz_name}**."),
            view=QuietHoursMenu(g, interaction.user.id))


class TimezoneSelect(discord.ui.Select):
    def __init__(self, g: GuildState, then_add: bool):
        self.then_add = then_add
        options = [discord.SelectOption(
            label=f"Same as the server ({g.timezone})"[:100], value=g.timezone,
            description=f"{local_clock(g.timezone)} there now")]
        options += [discord.SelectOption(label=label, value=tz, description=f"{local_clock(tz)} there now")
                    for label, tz in COMMON_TIMEZONES if tz != g.timezone]
        options.append(discord.SelectOption(label="Somewhere else...", value="other", emoji="✏️",
                                            description="Type a city or timezone name"))
        super().__init__(placeholder="Pick your timezone...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "other":
            await interaction.response.send_modal(TimezoneModal(self.then_add))
            return
        await set_user_timezone(interaction, self.values[0], self.then_add)


class TimezoneModal(discord.ui.Modal, title="Your Timezone"):
    name = discord.ui.TextInput(label="City or timezone", placeholder="e.g. Toronto, Berlin, Asia/Manila", max_length=60)

    def __init__(self, then_add: bool):
        super().__init__()
        self.then_add = then_add

    async def on_submit(self, interaction: discord.Interaction):
        tz_name = find_timezone(self.name.value)
        if tz_name is None:
            await interaction.response.send_message(
                f"I couldn't find `{self.name.value}`. Try a big city near you in the same timezone, like `Toronto` "
                "or `Berlin`, or a full name like `America/Toronto`.", ephemeral=True)
            return
        await set_user_timezone(interaction, tz_name, self.then_add)


class DaySelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=label, value=str(i)) for i, (label, _) in enumerate(DAY_OPTIONS)]
        super().__init__(placeholder="Pick the days...", options=options)

    async def callback(self, interaction: discord.Interaction):
        label, days = DAY_OPTIONS[int(self.values[0])]
        g = gs(interaction.guild_id)
        view = discord.ui.View(timeout=900)
        view.add_item(TimeSelect(days, label))
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🌙 Quiet Hours",
                description=f"Days: **{label}**\n**Step 2 of 2:** what time? (in {user_tz_name(g, interaction.user.id)})",
                color=discord.Color.dark_purple()),
            view=view)


class TimeSelect(discord.ui.Select):
    def __init__(self, days: List[int], day_label: str):
        self.days, self.day_label = days, day_label
        options = [discord.SelectOption(label=label, value=f"{start}|{end}") for label, start, end in TIME_OPTIONS]
        options.append(discord.SelectOption(label="Custom times...", value="custom", emoji="✏️"))
        super().__init__(placeholder="Pick a time block...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "custom":
            await interaction.response.send_modal(CustomTimeModal(self.days))
            return
        start, end = self.values[0].split("|")
        await add_quiet_rule(interaction, self.days, start, end)


class CustomTimeModal(discord.ui.Modal, title="Custom Quiet Hours"):
    start = discord.ui.TextInput(label="Quiet from", placeholder="e.g. 22:00 or 10pm", max_length=10)
    end = discord.ui.TextInput(label="Until", placeholder="e.g. 07:00 or 7am", max_length=10)

    def __init__(self, days: List[int]):
        super().__init__()
        self.days = days

    async def on_submit(self, interaction: discord.Interaction):
        start = parse_clock(self.start.value)
        end = parse_clock(self.end.value, allow_24=True)
        if start is None or end is None or start == end:
            await interaction.response.send_message(
                "I couldn't read those times. Try something like `22:00` and `07:00`, or `10pm` and `7am`.",
                ephemeral=True)
            return
        await add_quiet_rule(interaction, self.days, start, end)


# ---------------------------------------------------------------------------
# BOT
# ---------------------------------------------------------------------------


class QueueBot(discord.Client):
    def __init__(self):
        # No privileged intents: the bot only needs to see servers and button presses.
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True),
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        global CHECK_IN_VIEW
        self.add_dynamic_items(JoinQueueButton)
        self.add_view(QueuePanelView())
        self.add_view(QuietHoursPanelView())
        CHECK_IN_VIEW = CheckInView()
        self.add_view(CHECK_IN_VIEW)
        await self.tree.sync()
        sweep_queues.start()
        sweep_quiet_hours.start()


bot = QueueBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id}) in {len(bot.guilds)} server(s).")
    for guild in bot.guilds:
        await refresh_panel(guild.id)
        await refresh_quiet_board(guild)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Command error in /{interaction.command.name if interaction.command else '?'}: {error!r}")
    message = "Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------


@bot.tree.command(name="queue", description="Post the queue panel in this channel.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def queue_command(interaction: discord.Interaction):
    g = gs(interaction.guild_id)
    await interaction.response.send_message(embed=build_panel_embed(g), view=QueuePanelView(g))
    msg = await interaction.original_response()
    old = g.panel_message
    g.panel_message = {"channel_id": interaction.channel_id, "message_id": msg.id}
    save_state()
    if old and old["message_id"] != msg.id:  # the old panel stops updating, so tidy it away
        channel = bot.get_channel(old["channel_id"])
        if channel is not None:
            try:
                await channel.get_partial_message(old["message_id"]).delete()
            except discord.HTTPException:
                pass


@bot.tree.command(name="quiet-hours", description="Choose when you don't want to be pinged for the queue.")
@app_commands.guild_only()
async def quiet_hours_command(interaction: discord.Interaction):
    g = gs(interaction.guild_id)
    await interaction.response.send_message(
        embed=build_quiet_embed(g, interaction.user.id), view=QuietHoursMenu(g, interaction.user.id), ephemeral=True)


@bot.tree.command(name="quiet-hours-panel", description="Post a permanent \"set your quiet hours\" button here.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def quiet_hours_panel_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌙 Quiet Hours",
        description="Pick the times you don't want queue pings. Press the button any time to change them.",
        color=discord.Color.dark_purple(),
    )
    await interaction.response.send_message(embed=embed, view=QuietHoursPanelView())


@bot.tree.command(name="all-quiet-hours-panel", description="Post an auto-updating list of everyone's quiet hours.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def all_quiet_hours_panel_command(interaction: discord.Interaction):
    g = gs(interaction.guild_id)
    await interaction.response.send_message(embed=build_quiet_board_embed(interaction.guild, g))
    msg = await interaction.original_response()
    g.quiet_board_message = {"channel_id": interaction.channel_id, "message_id": msg.id}
    save_state()


@bot.tree.command(name="queue-remove", description="Take a player out of the queue.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def queue_remove_command(interaction: discord.Interaction, player: discord.Member):
    g = gs(interaction.guild_id)
    mode = find_user_mode(g, player.id)
    if mode is None:
        await interaction.response.send_message(f"{player.mention} isn't in a queue.", ephemeral=True)
        return
    entry = find_entry(g, mode, player.id)
    remove_entry(g, mode, player.id)
    save_state()
    await interaction.response.send_message(f"Removed {player.mention} from the **{mode}** queue.", ephemeral=True)
    if entry:
        await delete_prompt(entry)
    await refresh_panel(interaction.guild_id)


# ---------- /setup ----------

setup_group = app_commands.Group(
    name="setup", description="Configure the queue bot for this server.",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)


def setup_problems(guild: discord.Guild, g: GuildState) -> List[str]:
    """Things that will stop the bot working, in plain words."""
    problems = []
    me = guild.me
    role = guild.get_role(g.ping_role_id) if g.ping_role_id else None
    if g.ping_role_id and role is None:
        problems.append("The ping role was deleted. Pick a new one with `/setup ping-role`.")
    if not g.ping_role_id:
        problems.append("No ping role yet, so nobody gets pinged. Set one with `/setup ping-role`.")
    if role is not None:
        if not me.guild_permissions.manage_roles:
            problems.append("I don't have **Manage Roles**, so quiet hours and the Pings button can't work.")
        elif role >= me.top_role:
            problems.append(f"My role is below {role.mention}. Drag my role above it in Server Settings → Roles.")
        if not role.mentionable and not me.guild_permissions.mention_everyone:
            problems.append(f"{role.mention} isn't mentionable, so my pings won't notify anyone. Turn on "
                            "\"Allow anyone to @mention this role\" for it.")
    channel = notify_channel(g)
    if channel is not None and not channel.permissions_for(me).send_messages:
        problems.append(f"I can't send messages in {channel.mention}.")
    if not g.panel_message:
        problems.append("No queue panel yet. Run `/queue` in the channel where players should join.")
    return problems


def build_setup_embed(guild: discord.Guild, g: GuildState) -> discord.Embed:
    modes = "\n".join(f"• {m}: {n} players" for m, n in g.modes.items()) or "None"
    panel = f"<#{g.panel_message['channel_id']}>" if g.panel_message else "not posted yet"
    embed = discord.Embed(title="Queue Bot Settings", color=discord.Color.blurple())
    embed.add_field(name="Ping role", value=f"<@&{g.ping_role_id}>" if g.ping_role_id else "not set")
    embed.add_field(name="Ping channel",
                    value=f"<#{g.ping_channel_id}>" if g.ping_channel_id else "same channel as the panel")
    embed.add_field(name="Queue panel", value=panel)
    embed.add_field(name="Default timezone", value=g.timezone)
    embed.add_field(name="Timers", value=(
        f"In queue for {g.join_minutes} min, check-in {g.warning_minutes} min before the end, "
        f"\"Stay\" adds {g.renew_minutes} min"), inline=False)
    embed.add_field(name="Queues", value=modes, inline=False)
    problems = setup_problems(guild, g)
    embed.add_field(name="Checks", value="\n".join(f"⚠️ {p}" for p in problems) or "✅ Everything looks good.",
                    inline=False)
    return embed


@setup_group.command(name="show", description="Show this server's settings and anything that needs fixing.")
async def setup_show(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_setup_embed(interaction.guild, gs(interaction.guild_id)), ephemeral=True)


@setup_group.command(name="ping-role", description="The role pinged when people join the queue.")
@app_commands.describe(role="Leave empty to turn role pings off")
async def setup_ping_role(interaction: discord.Interaction, role: Optional[discord.Role] = None):
    g = gs(interaction.guild_id)
    if role is not None and (role.is_default() or role.managed):
        await interaction.response.send_message("Pick a normal role, not @everyone or a bot's role.", ephemeral=True)
        return
    g.ping_role_id = role.id if role else None
    g.quiet_muted.clear()  # those were for the old role
    save_state()
    await interaction.response.send_message(
        f"Ping role set to {role.mention}." if role else "Role pings turned off.", ephemeral=True)


@setup_group.command(name="ping-channel", description="Where pings, check-ins and match announcements go.")
@app_commands.describe(channel="Leave empty to use the queue panel's channel")
async def setup_ping_channel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    g = gs(interaction.guild_id)
    g.ping_channel_id = channel.id if channel else None
    save_state()
    await interaction.response.send_message(
        f"Pings will go to {channel.mention}." if channel else "Pings will go to the queue panel's channel.",
        ephemeral=True)


async def timezone_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower().replace(" ", "_")
    matches = [tz for tz in ALL_TIMEZONES if current in tz.lower()]
    return [app_commands.Choice(name=tz, value=tz) for tz in matches[:25]]


@setup_group.command(name="timezone", description="Default timezone for quiet hours, for players who haven't picked one.")
@app_commands.autocomplete(name=timezone_autocomplete)
async def setup_timezone(interaction: discord.Interaction, name: str):
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        await interaction.response.send_message(
            f"I don't know the timezone `{name}`. Start typing a city, like `Chicago`, and pick from the list.",
            ephemeral=True)
        return
    g = gs(interaction.guild_id)
    g.timezone = name
    save_state()
    await interaction.response.send_message(
        f"Default timezone set to **{name}** (it's {local_clock(name)} there now). Players who picked their own "
        "timezone keep theirs.", ephemeral=True)
    await refresh_quiet_board(interaction.guild)


@setup_group.command(name="timers", description="How long people stay in queue and when they get checked on.")
@app_commands.describe(
    queue_minutes="How long someone stays in queue after joining",
    warning_minutes="How many minutes before that runs out the check-in is posted",
    stay_minutes="How long pressing Stay in Queue keeps them in",
)
async def setup_timers(interaction: discord.Interaction,
                       queue_minutes: app_commands.Range[int, 2, 720],
                       warning_minutes: app_commands.Range[int, 1, 60],
                       stay_minutes: app_commands.Range[int, 2, 720]):
    if warning_minutes >= min(queue_minutes, stay_minutes):
        await interaction.response.send_message(
            "The check-in has to come before time runs out, so `warning_minutes` must be smaller than both "
            "`queue_minutes` and `stay_minutes`.", ephemeral=True)
        return
    g = gs(interaction.guild_id)
    g.join_minutes, g.warning_minutes, g.renew_minutes = queue_minutes, warning_minutes, stay_minutes
    save_state()
    await interaction.response.send_message("Timers updated. They apply to people who join from now on.",
                                            ephemeral=True)
    await refresh_panel(interaction.guild_id)


@setup_group.command(name="add-queue", description="Add a queue button, like Doubles (4) or Singles (2).")
@app_commands.describe(name="Button name", players="Players needed for a match")
async def setup_add_queue(interaction: discord.Interaction, name: str, players: app_commands.Range[int, 2, 20]):
    g = gs(interaction.guild_id)
    name = " ".join(name.split())
    if not name or len(name) > MAX_MODE_NAME:
        await interaction.response.send_message(f"Names can be up to {MAX_MODE_NAME} characters.", ephemeral=True)
        return
    existing = next((m for m in g.modes if m.lower() == name.lower()), None)
    if existing is None and len(g.modes) >= MAX_MODES:
        await interaction.response.send_message(f"You can have up to {MAX_MODES} queues.", ephemeral=True)
        return
    if existing and existing != name:
        g.queues[name] = g.queues.pop(existing, [])
        g.modes = {(name if m == existing else m): n for m, n in g.modes.items()}
    g.modes[name] = players
    save_state()
    await interaction.response.send_message(f"**{name}** queue: {players} players per match.", ephemeral=True)
    await refresh_panel(interaction.guild_id)


async def queue_name_autocomplete(interaction: discord.Interaction, current: str):
    modes = gs(interaction.guild_id).modes
    return [app_commands.Choice(name=m, value=m) for m in modes if current.lower() in m.lower()][:25]


@setup_group.command(name="remove-queue", description="Remove a queue button. Anyone waiting in it is taken out.")
@app_commands.autocomplete(name=queue_name_autocomplete)
async def setup_remove_queue(interaction: discord.Interaction, name: str):
    g = gs(interaction.guild_id)
    if name not in g.modes:
        await interaction.response.send_message(f"There's no queue called **{name}**.", ephemeral=True)
        return
    if len(g.modes) == 1:
        await interaction.response.send_message("That's the only queue. Add another one first.", ephemeral=True)
        return
    del g.modes[name]
    waiting = g.queues.pop(name, [])
    save_state()
    await interaction.response.send_message(f"Removed the **{name}** queue.", ephemeral=True)
    for e in waiting:
        await delete_prompt(e)
    await refresh_panel(interaction.guild_id)


bot.tree.add_command(setup_group)


# ---------------------------------------------------------------------------
# BACKGROUND LOOPS
# ---------------------------------------------------------------------------


@tasks.loop(seconds=SWEEP_INTERVAL_SECONDS)
async def sweep_queues():
    """Post check-ins to people whose time is nearly up, and remove people who didn't answer."""
    now = time.time()
    for guild_id, g in list(guilds.items()):
        if bot.get_guild(guild_id) is None:
            continue
        to_prompt, expired = [], []
        for mode, entries in g.queues.items():
            keep = []
            for e in entries:
                remaining = e.expires_at - now
                if remaining <= 0:
                    expired.append((mode, e))
                    continue
                if remaining <= g.warning_minutes * 60 and not e.prompted:
                    e.prompted = True
                    to_prompt.append((mode, e))
                keep.append(e)
            g.queues[mode] = keep
        if not to_prompt and not expired:
            continue
        save_state()

        channel = notify_channel(g)
        for mode, e in to_prompt:
            msg = await safe_send(
                channel,
                f"<@{e.user_id}> are you still there? You'll be removed from the **{mode}** queue "
                f"<t:{int(e.expires_at)}:R> unless you press **Stay in Queue**.",
                view=CHECK_IN_VIEW,
            )
            if msg is not None:
                e.prompt_message_id, e.prompt_channel_id = msg.id, msg.channel.id
        for mode, e in expired:
            await delete_prompt(e)
            await safe_send(channel, f"<@{e.user_id}> was removed from the **{mode}** queue for not answering "
                                     "the check-in.")
        save_state()
        await refresh_panel(guild_id)


@tasks.loop(minutes=1)
async def sweep_quiet_hours():
    """Take the ping role away when someone's quiet hours start and give it back when they end. Only people
    whose quiet hours just changed are looked up, so the bot doesn't need the members intent."""
    for guild_id, g in list(guilds.items()):
        guild = bot.get_guild(guild_id)
        if guild is None or not g.ping_role_id:
            continue
        changed = False
        for uid in set(g.quiet_hours) | set(g.quiet_muted) | set(g.quiet_active):
            quiet = user_in_quiet_hours(g, uid)
            if quiet == (uid in g.quiet_active) and not (not quiet and uid in g.quiet_muted):
                continue
            changed = True
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except discord.NotFound:  # left the server
                    g.quiet_hours.pop(uid, None)
                    g.quiet_active = [u for u in g.quiet_active if u != uid]
                    g.quiet_muted = [u for u in g.quiet_muted if u != uid]
                    continue
                except discord.HTTPException:
                    continue
            await sync_quiet_role(guild, g, member)
        if changed:
            save_state()


async def _wait_until_ready():
    await bot.wait_until_ready()


sweep_queues.before_loop(_wait_until_ready)
sweep_quiet_hours.before_loop(_wait_until_ready)


if __name__ == "__main__":
    guilds.update(load_state())
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_BOT_TOKEN environment variable before running. See docs/SETUP.md.")
    bot.run(token)
