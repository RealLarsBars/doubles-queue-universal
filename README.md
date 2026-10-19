# Doubles Queue Bot

A simple matchmaking queue for any Discord server. Players press a button to join, the bot pings your queue role
when someone joins, announces the match when the queue fills, and removes anyone who stops answering. Each player
picks their own quiet hours, so pings only reach them when they want them.

Every server sets itself up with `/setup` commands, so one copy of the bot can run in as many servers as you like
and nobody has to edit code.

**Want it in your server? Follow the step-by-step guide: [docs/SETUP.md](docs/SETUP.md).**

## What it does

- **Queue panel.** `/queue` posts a panel with a button for each queue (Doubles, 4 players, out of the box), plus
  Leave Queue, Pings On/Off and Quiet Hours buttons. The panel always shows who's waiting.
- **Pings.** When the first person joins a queue, the bot pings your ping role. When the queue is one player short it
  pings again. Other joins are announced without a ping.
- **Matches.** When enough people are in, the first ones in line are matched and mentioned in a "Match found"
  message.
- **Check-ins and removal.** Everyone gets 35 minutes in queue. 5 minutes before that runs out, the bot mentions them
  with a **Stay in Queue / Leave Queue** check-in. Staying adds 30 more minutes. If they don't answer, they're
  removed from the queue and the channel is told why.
- **Quiet hours.** Players pick their own timezone, then the days and times (presets or their own, like 10pm to 7am)
  when they don't want pings.
  While their quiet hours are on, the bot takes the ping role off them, and gives it back when they end. Someone who
  turned pings off with the button stays off.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/quiet-hours` | everyone | set, view or remove your quiet hours |
| `/queue` | admins | post the queue panel in this channel (posting a new one removes the old one) |
| `/quiet-hours-panel` | admins | post a permanent "Set My Quiet Hours" button |
| `/all-quiet-hours-panel` | admins | post an auto-updating list of everyone's quiet hours |
| `/queue-remove player` | admins | take someone out of the queue |
| `/setup show` | admins | show this server's settings and anything that needs fixing |
| `/setup ping-role [role]` | admins | the role pinged when people join (leave empty to turn pings off) |
| `/setup ping-channel [channel]` | admins | where pings, check-ins and match messages go (default: the panel's channel) |
| `/setup timezone name` | admins | the default timezone for players who haven't picked their own, like `America/New_York` (default `UTC`) |
| `/setup timers queue_minutes warning_minutes stay_minutes` | admins | how long people stay in queue (default 35, 5, 30) |
| `/setup add-queue name players` | admins | add a queue button, or change a queue's player count (up to 5 queues) |
| `/setup remove-queue name` | admins | remove a queue button |

"Admins" means people with the **Manage Server** permission. You can change who sees each command in
**Server Settings → Integrations → your bot**.

## Running it

```
pip install -r requirements.txt
export DISCORD_BOT_TOKEN="your-token-here"
python queue_bot.py
```

| Environment variable | Required | Meaning |
|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | the bot's token from the Discord Developer Portal |
| `QUEUE_STATE_FILE` | no | where settings and queues are saved (default `queue_state.json`). On a host like Railway, point it at a volume so it survives redeploys |

The bot needs no privileged intents. In each server it needs **View Channels, Send Messages, Embed Links, Read
Message History** and **Manage Roles**, and its role has to sit above the ping role. `/setup show` checks all of
this for you.

## Saving

Everything (each server's settings, queues and quiet hours) lives in one JSON file, written on every change. Saves
go to a temp file that is then swapped in, a backup is kept next to it (`.bak`, refreshed every 10 minutes), and if
the file can't be read on startup the bot loads the backup. After a restart, the panels and check-in buttons keep
working and the panels are brought up to date.

## Tests

```
pip install pytest
python -m pytest tests
```

## Limits

- One process and one JSON file. Plenty for many servers, but not built to run several copies at once.
- Up to 5 queues per server and 10 quiet hours schedules per player.
