# Setting Up the Doubles Queue Bot in Your Server

This guide takes you from nothing to a working queue in your server. You don't need to know how to code. It takes
about 20 minutes.

You'll do four things:

1. [Create your bot on Discord](#step-1-create-your-bot-on-discord)
2. [Invite it to your server](#step-2-invite-it-to-your-server)
3. [Get it running](#step-3-get-it-running) (on your computer, or on a host so it's always online)
4. [Set it up inside Discord](#step-4-set-it-up-inside-discord)

---

## Step 1: Create your bot on Discord

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and log in.
2. Click **New Application** (top right). Give it a name, like `Queue Bot`. This is the name people will see. Accept
   the terms and click **Create**.
3. In the left menu, click **Bot**.
4. Under **Token**, click **Reset Token**, confirm, and then **Copy**. Paste it somewhere safe for now (a note on your
   computer is fine).

> ⚠️ **Treat the token like a password.** Anyone who has it can control your bot. Never post it in Discord or put it
> in a public GitHub repo. If it ever leaks, come back here and click **Reset Token** again.

5. Still on the **Bot** page, you can leave **Public Bot** on or off. Turn it off if only you should be able to
   invite it. You do **not** need to turn on any of the "Privileged Gateway Intents".

## Step 2: Invite it to your server

You need the **Manage Server** permission in the server for this.

1. In the left menu, click **OAuth2**, then find the **OAuth2 URL Generator**.
2. Under **Scopes**, check **`bot`** and **`applications.commands`**.
3. A **Bot Permissions** box appears below. Check:
   - **View Channels**
   - **Send Messages**
   - **Embed Links**
   - **Read Message History**
   - **Manage Roles**
4. Copy the **Generated URL** at the bottom, open it in your browser, pick your server and click **Authorize**.

The bot shows up in your member list, offline for now. That's expected until Step 3.

> Shortcut: you can also use this link, replacing `YOUR_CLIENT_ID` with the **Application ID** from the
> **General Information** page:
> `https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot+applications.commands&permissions=268520448`

## Step 3: Get it running

The bot is a small Python program. It's online only while it's running somewhere. Pick one:

- **Option A, your own computer.** Free and quick to try, but the bot goes offline when your computer sleeps or
  shuts down.
- **Option B, a hosting service.** Online all the time. Railway is shown here; any host that can run Python works.

### Option A: Your own computer

1. **Install Python 3.10 or newer** from [python.org/downloads](https://www.python.org/downloads/).
   On Windows, check **"Add python.exe to PATH"** on the first screen of the installer.
2. **Download the bot.** On this repo's GitHub page, click the green **Code** button → **Download ZIP**, and unzip it
   somewhere easy to find, like your Desktop.
3. **Open a terminal in that folder.**
   - Windows: open the unzipped folder, click the address bar, type `cmd` and press Enter.
   - Mac: open **Terminal**, type `cd ` (with a space), drag the folder into the window, and press Enter.
4. **Install what the bot needs:**
   ```
   python -m pip install -r requirements.txt
   ```
   (On a Mac, use `python3` instead of `python` in these commands.)
5. **Give it your token and start it.** Replace `your-token-here` with the token from Step 1.
   - Windows (Command Prompt):
     ```
     set DISCORD_BOT_TOKEN=your-token-here
     python queue_bot.py
     ```
   - Windows (PowerShell):
     ```
     $env:DISCORD_BOT_TOKEN="your-token-here"
     python queue_bot.py
     ```
   - Mac or Linux:
     ```
     export DISCORD_BOT_TOKEN="your-token-here"
     python3 queue_bot.py
     ```
6. When you see `Logged in as Queue Bot#1234`, the bot is online. Leave this window open. To stop the bot, press
   **Ctrl+C**. To start it again later, repeat step 5.

The bot saves everything to `queue_state.json` in the same folder. Don't delete it, or the bot forgets its settings.

### Option B: Railway (always online)

[Railway](https://railway.com) runs the bot for you. It's a paid service with a small free trial; check their
current pricing.

1. **Get your own copy of the code.** Log in to GitHub, open this repo and click **Fork** (top right) →
   **Create fork**.
2. Sign in to Railway with your GitHub account. Click **New Project** → **Deploy from GitHub repo**, and pick your
   fork.
3. Open the new service and go to **Variables**. Add:
   - `DISCORD_BOT_TOKEN` = your token from Step 1
   - `QUEUE_STATE_FILE` = `/data/queue_state.json`
4. **Add a volume so settings survive updates.** Right-click the service on the project canvas (or use the command
   palette) → **Attach Volume**, and set its mount path to `/data`. Without this, every redeploy wipes your settings
   and queues.
5. If Railway asks for a start command, use `python queue_bot.py`.
6. Deploy. Open **Deployments** → **View Logs**; when you see `Logged in as ...`, the bot is online.

## Step 4: Set it up inside Discord

Do these in your server, as someone with the **Manage Server** permission. Slash commands can take a minute to show
up the first time; if you don't see them, fully restart Discord (Ctrl+R on desktop).

### 4.1 Make a ping role

This is the role the bot pings when people join the queue. People who want queue pings will have it.

1. **Server Settings → Roles → Create Role.** Name it something like `Queue Pings`.
2. Turn on **"Allow anyone to @mention this role"**, then **Save Changes**. (Without this, the bot's pings won't
   notify anyone.)
3. Still on the **Roles** page, **drag the bot's role above `Queue Pings`** in the list. The bot can only hand out
   roles that are below its own.

### 4.2 Tell the bot about it

1. `/setup ping-role` and pick your `Queue Pings` role.
2. `/setup timezone` and start typing your city or region (like `Chicago`, `London`, `Sydney`), then pick it from the
   list. Quiet hours use this timezone, so pick the one most of your players are in.
3. *(Optional)* `/setup ping-channel` and pick a channel if you want pings, check-ins and match messages somewhere
   other than the queue panel's channel.
4. *(Optional)* Change the queues. You start with one, **Doubles** for 4 players.
   - `/setup add-queue name:Singles players:2` adds a Singles button.
   - `/setup add-queue name:Doubles players:4` with a different number changes an existing queue.
   - `/setup remove-queue` removes one.
5. *(Optional)* `/setup timers` changes how long people stay in queue (default 35 minutes), how early the check-in
   comes (5 minutes before the end) and how long **Stay in Queue** adds (30 minutes).

### 4.3 Post the panels

1. Go to the channel where players should join (for example `#queue`) and run **`/queue`**. The queue panel appears.
   Pin it if you like.
2. *(Optional)* In a roles or info channel, run **`/quiet-hours-panel`** for a permanent "Set My Quiet Hours" button.
3. *(Optional)* **`/all-quiet-hours-panel`** posts a list of everyone's quiet hours that updates itself.

### 4.4 Check everything

Run **`/setup show`**. At the bottom, **Checks** should say **✅ Everything looks good.** If it lists a problem, it
tells you what to fix.

That's it. Your queue is live. 🎉

---

## How it works for your players

Paste this in your server if it helps:

> **How the queue works**
> • Press **🔔 Pings On/Off** on the queue panel to get (or stop getting) a ping when people are looking for a game.
> • Press a queue button (like **Doubles**) to join. When enough people are in, the bot mentions everyone in the match.
> • You stay in queue for 35 minutes. Near the end, the bot asks if you're still there. Press **Stay in Queue** to
>   keep going, or you'll be taken out of the queue.
> • Press **Leave Queue** any time to leave.
> • Press **🌙 Quiet Hours** (or type `/quiet-hours`) to pick times you don't want pings, like weeknights after 11pm.
>   The bot pauses your pings during those times and turns them back on after.

## Troubleshooting

| Problem | Fix |
|---|---|
| The slash commands don't show up | Wait a minute and fully restart Discord. Make sure you checked **`applications.commands`** in Step 2; if not, invite the bot again with it checked. |
| Only I can't see the admin commands | They're only shown to people with **Manage Server**. You can change that in **Server Settings → Integrations → your bot**. |
| The bot is offline | It isn't running. On your computer, start it again (Step 3A, step 5). On Railway, check the logs. |
| Pings appear but nobody gets notified | Turn on **"Allow anyone to @mention this role"** for the ping role (Step 4.1). |
| "I can't hand out @Queue Pings" | Give the bot **Manage Roles** and drag its role above the ping role (Step 4.1). |
| Quiet hours are off by a few hours | Set the right timezone with `/setup timezone`. |
| The panel stopped updating | Someone deleted it. Run `/queue` again. |
| Settings disappeared after an update on Railway | The volume is missing or `QUEUE_STATE_FILE` doesn't point into it (Step 3B, steps 3 and 4). |
| `Set the DISCORD_BOT_TOKEN environment variable` when starting | The token wasn't set in that terminal window. Do Step 3A, step 5 again in the same window you start the bot from. |
| `Improper token has been passed` | The token is wrong or was reset. Copy a fresh one (Step 1). |

## Updating the bot

- **Your computer:** stop the bot (Ctrl+C), download the new ZIP, copy your `queue_state.json` into the new folder,
  run `python -m pip install -r requirements.txt` again, and start it.
- **Railway:** on your fork's GitHub page, click **Sync fork**. Railway redeploys on its own, and your settings stay
  on the volume.

## Using one bot for several servers

You don't need a separate bot per server. Invite the same bot to each one (Step 2) and do Step 4 in each. Every
server keeps its own settings, queues and quiet hours.
