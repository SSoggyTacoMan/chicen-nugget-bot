# Chicen Nugget Bot — a personal Slack bot

A small, friendly Slack bot for a personal workspace. It welcomes new members, runs a button-based **tier 2 private-channel access request** flow, chats as a **personal AI agent** with tools and memory, nudges people about **usergroup pings**, runs a **daily recap** routine, and ships with slash commands, reminders, scheduled messages, polls and dice.

Built with Python, [Slack Bolt](https://slack.dev/bolt-python/) and **socket mode**, so it runs on your laptop or a small VPS without a public URL or webhook server.

```
member joins ──▶ public welcome + DM + ping group
member clicks "request tier 2 access" ──▶ modal ──▶ DM to admin
admin clicks Approve ──▶ bot invites member to private channel ──▶ DM "you're in"
```

## What it does

- **Welcomer** — watches channels for joins/leaves, posts a public welcome, DMs an onboarding message once, optionally pings a user group and adds/removes members from it.
- **Tier 2 access requests** — a panel button or `/nugget access` opens a short modal. Requests are stored in SQLite and sent to the admin with Approve/Deny buttons. Approving invites the member to your private channel automatically. Already-members are told the door is open, and `ACCESS_AUTO_APPROVE=true` skips the queue.
- **Usergroup helpers** — DMs anyone who replies in a thread under a usergroup ping (replies re-ping the whole group), and posts a “thread here!” anchor when a ping includes 🧵 (or offers yes/no buttons when it doesn't).
- **Daily recap** — a scheduled public prompt asks how your day went. Marking it done links your reply, adds ✨, optionally pings a usergroup, and can open a second private recap.
- **Personal AI agent** — DM it (owner/admins only) or mention it in a channel. It has conversation memory in SQLite and tools for weather, dice, reminders, Slack search, channel history, DMs, notes, stats, URLs and optional web search. It also powers `/nugget ask`, `/nugget summarize` and `/nugget imagine`.
- **Jev gate** — in channels listed in `AI_AMBIENT_CHANNELS`, the bot asks Jev (`jev-latest`) four typed questions about every message: should it reply, was it addressed to the bot, is it spam, and what kind of message is it. Only a confident yes runs the expensive agent turn, so it can chime in on questions and help without replying to every message.
- **One slash command** — everything lives under `/nugget`, so it never collides with other apps: `/nugget help`, `/nugget ping`, `/nugget roll 2d6`, `/nugget poll ...`, `/nugget ask ...`, `/nugget imagine ...`, `/nugget pinggroup`, and more.
- **Reminders & scheduled messages** — one-off and recurring (`10m`, `tomorrow 09:00`, `daily 08:30`, `weekly mon 18:00`, `every 2h`), delivered by DM or posted to a channel by an admin.
- **Polls** — `/nugget poll "question" "a" "b"` posts live button tallies with bars; click another option to change your vote.
- **Daily digest** — optional DM summary of joins, leaves, pending access requests and upcoming reminders.
- **Optional user token** — enables Slack message search and user-group automation without changing the main bot identity.

## Quick start

### 1. Create the Slack app from the manifest

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Pick your workspace.
3. Paste the contents of [`slack-app-manifest.json`](slack-app-manifest.json). The app/bot name is set to `Chicen Nugget Bot`; edit it if you like.
4. Create the app, then **Install to Workspace** and allow it.
5. Open **App Home → Messages Tab** and check **Allow users to send messages from the Messages tab**. The manifest enables it, but apps created before that setting was added need the toggle flipped manually. Without it Slack says *Sending messages to this app has been turned off*.

If you change the name, also set `BOT_NAME` in `.env` (and optionally change the app name in Slack later).

> The bot registers exactly **one** slash command, `/nugget`, and routes everything through subcommands. That avoids the workspace-wide collisions you get from registering `/poll`, `/roll`, `/ping` and friends. If your workspace rejects the optional user scopes in the manifest, remove the `oauth_config.scopes.user` block; everything except Slack search and usergroup automation still works.

### 2. Collect three values

| value | where |
|---|---|
| `SLACK_BOT_TOKEN` (`xoxb-…`) | **OAuth & Permissions** → Bot User OAuth Token |
| `SLACK_APP_TOKEN` (`xapp-…`) | **Basic Information** → App-Level Tokens → generate with `connections:write` |
| `OWNER_USER_ID` (`U…`) | Your Slack profile → **More** → **Copy member ID** |

Socket mode is already enabled by the manifest, and so are the events, interactivity and slash commands.

### 3. Configure

```bash
cp .env.example .env
# edit .env: tokens, OWNER_USER_ID, TIMEZONE, WATCHED_CHANNEL_IDS, ACCESS_CHANNEL_ID, ...
```

Minimum useful setup:

```dotenv
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
OWNER_USER_ID=U012ABCDEF
BOT_NAME="Chicen Nugget Bot"
TIMEZONE=Europe/Amsterdam

WATCHED_CHANNEL_IDS=C012WELCOME
ACCESS_CHANNEL_ID=C012TIER2PRIVATE
ACCESS_PANEL_CHANNEL_ID=C012WELCOME
LOGS_CHANNEL_ID=C012LOGS
```

### 4. Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

You should see `Chicen Nugget Bot online as bot user …` and `starting socket mode...`. DM the bot or run `/ping`.

> Prefer `uv`? `uv venv && uv pip install -r requirements.txt && uv run main.py`.

### 5. Invite the bot to the right channels

The bot can only read/post where it is a member.

- `/invite @Chicen Nugget Bot` in the welcome channel, logs channel and panel channel.
- **Invite it to the private tier 2 channel too.** This is required: Slack only lets an app add people to a private channel that the app itself is in.

## Tier 2 access flow

1. A member clicks **Request access** on the panel (or in their welcome DM), or runs `/access`.
2. A modal asks why they want access and makes them agree to the rules.
3. The bot checks whether they're already in `ACCESS_CHANNEL_ID`; if so, it tells them the door is already open.
4. The bot DMs the admin (or posts in `ADMIN_CHANNEL_ID`) with **Approve** / **Deny** buttons.
5. Approve → `conversations.invite` adds them to `ACCESS_CHANNEL_ID`, the bot DMs them “the door swings open”, and the request is marked approved.
6. Deny → the admin can leave a short reason, which is DM'd to the requester.

Set `ACCESS_AUTO_APPROVE=true` to invite people immediately when they submit the modal. If the invite fails, the request stays pending and still goes to an admin.

Requests are stored so you can see pending/approved/denied history. Duplicate pending requests and a per-person cooldown (`ACCESS_COOLDOWN_HOURS`, default 24) prevent spam.

If approval fails with `not_in_channel`, invite the bot to the private channel. If it fails with `missing_scope`, reinstall the app with the manifest's `groups:write` scope.

## Usergroup helpers

These need no extra scopes, just `message.channels` / `message.groups` (already in the manifest).

- **Ping group opt-in** (`USERGROUP_ID`, `USERGROUP_PROMPT=true`) — when someone joins a watched channel, the bot DMs them a short intro with **:bell: add me** / **no thanks** buttons. Clicking adds them to the usergroup (via `SLACK_USER_TOKEN`). Each person is asked once. Set `USERGROUP_PROMPT=false` for the old auto-add behaviour. `/pinggroup` lets anyone join, leave, or check their status any time.
- **Ping guard** (`USERGROUP_GUARD=true`) — when someone replies in a thread whose parent contains a usergroup mention, the bot DMs them: “please don't thread on user group mentions”. Thread replies re-ping the whole group.
- **Thread helper** (`THREAD_HELPER=true`) — when a message pings the group and includes 🧵 (or `:thread:`), the bot posts a `🧵 thread here!` anchor for people to reply under. If the ping has no 🧵 marker, the poster gets ephemeral **yes** / **no** buttons instead.

The bot needs to be able to read the parent message, so it only works in channels it's a member of.

## Daily recap

The recap is a small daily ritual:

```dotenv
RECAP_CHANNEL_ID=C012GENERAL
RECAP_TIME=20:00
RECAP_PRIVATE_CHANNEL_ID=C012PRIVATE   # optional
RECAP_REMINDER_MINUTES=10              # 0 disables the heads-up DM
RECAP_PING=S0123456789                 # optional usergroup ping
RECAP_PRIVATE_PING=S0123456789         # optional private-channel ping
```

At `RECAP_TIME` the bot posts a public “how was your day?” prompt with a 🌙 **done!** button. The owner replies in the thread, then clicks done: the bot marks the prompt ✨, links the owner's latest reply, and optionally pings the group. If `RECAP_PRIVATE_CHANNEL_ID` is set, it then posts a private follow-up recap prompt with its own done button.

Admins can trigger a prompt immediately with `/recap`. Recaps are disabled until `RECAP_CHANNEL_ID` and `RECAP_TIME` are set.

## AI agent

The agent is wired for **Hack Club AI** by default, and also works with any **OpenAI-compatible** chat completions endpoint. It needs a model with function/tool calling.

```dotenv
AI_API_KEY=sk-hc-v1-...
AI_BASE_URL=https://ai.hackclub.com/proxy/v1
AI_MODEL=openai/gpt-5.6-luna
AI_FALLBACK_MODEL=anthropic/claude-haiku-5.5
AI_RATE_LIMIT_PER_MINUTE=12
AI_GLOBAL_RATE_LIMIT_PER_MINUTE=30
AI_USER_DAILY_BUDGET_USD=0.50
AI_DAILY_BUDGET_USD=0
```

Get a key from <https://ai.hackclub.com/dashboard>. The fallback model is tried automatically when the primary model errors or times out. `AI_RATE_LIMIT_PER_MINUTE=0` disables the per-user limit.

`gpt-5.6-luna` is the default because it matched `gpt-5.5` on tool calls and Slack-style answers in testing, at about 1/25th the price and roughly the same latency. `claude-haiku-5.5` is the fallback: a different provider, faster, and cheap enough that a bad day doesn't matter. Both handle function calling.

### The jev gate

In a busy channel a bot that answers everything is noise. Jev is TypeSafe's *System One* model: it doesn't write text, it returns a typed probability for each question you ask it, in one pass. Hack Club AI serves it at `/jev/systemone`, so the bot asks four questions about every ambient message:

| question | type | what it decides |
|---|---|---|
| `should_reply` | `noul` | would a reply genuinely add something? |
| `addressed` | `noul` | is the message aimed at the bot? |
| `spam` | `noul` | is it spam, a mass ping, or a link dump? |
| `kind` | `choice` | question, help, feedback, celebration, bot, chatter, spam, other |

A message is answered when `should_reply` clears `AI_GATE_MIN_CONFIDENCE` (or `addressed` clears `AI_GATE_ADDRESSED_CONFIDENCE`) and `spam` stays below `AI_GATE_SPAM_THRESHOLD`. The `kind` answer is passed to the agent as a hint, so it knows whether to troubleshoot, congratulate, or keep it short.

```dotenv
AI_GATE_ENABLED=true
AI_GATE_MODEL=jev-latest
AI_GATE_TIMEOUT=8
# defaults to <AI_BASE_URL>/jev/systemone
AI_JEV_URL=
AI_GATE_MIN_CONFIDENCE=0.5
AI_GATE_ADDRESSED_CONFIDENCE=0.6
AI_GATE_SPAM_THRESHOLD=0.7
# channels where the bot listens to every message; empty = off
AI_AMBIENT_CHANNELS=
# minimum seconds between unprompted replies in one channel
AI_AMBIENT_COOLDOWN=120
```

Mentions and DMs always skip the gate. If Jev errors or times out, the gate fails closed (no reply), so a broken gate never turns into spam. Set `AI_AMBIENT_CHANNELS=*` to let it do this everywhere. The gate needs a Jev-capable endpoint; the rest of the bot still works with any OpenAI-compatible one.

### Spend limits

The bot accounts for every completion it makes — chat, Jev decisions, and `/imagine` — in `data/bot.db` and prices it from a small table. Hack Club AI already stops the account at its daily allowance, so the bot does not add a second global stop by default. What it does enforce is a **per-user** daily cap: once one person reaches `AI_USER_DAILY_BUDGET_USD`, their AI requests stop until 00:00 UTC. The owner is exempt. A guest with one very large request therefore can't spend the whole day's allowance.

```dotenv
AI_USER_DAILY_BUDGET_USD=0.50   # owner is exempt; 0 disables
AI_DAILY_BUDGET_USD=0           # optional extra bot-wide stop
AI_GLOBAL_RATE_LIMIT_PER_MINUTE=30
AI_MAX_CONTEXT_CHARS=24000
AI_IMAGE_COST_USD=0.04
```

Three limits work together:

- per-user rate limit (`AI_RATE_LIMIT_PER_MINUTE`) stops one person from spamming;
- workspace rate limit (`AI_GLOBAL_RATE_LIMIT_PER_MINUTE`) stops a burst across the whole server;
- per-user daily cap stops one person from quietly spending the day.

Usage is attributed to the Slack user who asked, so `/nugget budget` shows your own spend, the workspace total, and the top users for the day. Unknown models are priced pessimistically ($1/$5 per million) so a typo can't under-count.

Other endpoints still work:

```dotenv
# OpenRouter
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=openai/gpt-4o-mini

# local Ollama
AI_BASE_URL=http://localhost:11434/v1
AI_API_KEY=ollama
AI_MODEL=llama3.2
```

Who can use it:

- **DMs:** owner always; admins too when `AI_ALLOW_ADMINS=true`. Other users are silently ignored.
- **Channels:** anyone can mention the bot in a channel allowed by `AI_CHANNELS` (`*` = every channel the bot is in). It replies in a thread. Once it has replied in a thread, it keeps following that thread without a fresh mention.
- **Ambient:** in channels listed in `AI_AMBIENT_CHANNELS` it also listens to unaddressed messages and replies only when the jev gate says yes, at most once per `AI_AMBIENT_COOLDOWN` seconds.
- **Commands:** `/ask` and `/summarize` follow `AI_CHANNELS`; `/imagine` and `/forget` in channels are admin-only.

Conversation history and notes are stored in `data/bot.db`. Nothing is sent anywhere except your Slack workspace, your chosen AI endpoint, and the optional tools you enable.

### Persona

The bot's voice and rules live in [`prompts/system.md`](prompts/system.md): who it is, how it talks, when to use a tool, how to behave when it chimed in unprompted, and how to keep private data private. Edit that file to change the personality — no code changes needed. Each turn is also given the current time, the channel or DM it's in, who is speaking, and why it is replying (mention, thread follow-up, DM, or unprompted).

Changing the persona also tells the bot to drop its stored chat turns on the next start. The model learns tone from its own old replies, so a rewrite would otherwise keep sounding like the previous version. Owner notes and other data are untouched.

### AI commands

| command | what it does |
|---|---|
| `/ask <question>` | one-shot answer without touching thread memory |
| `/summarize [n]` | AI catch-up for the last `n` channel messages (default 50, max 200) |
| `/imagine <prompt>` | generate an image and upload it (admin; needs `files:write`) |
| `/forget` | wipe stored AI turns for the current DM, or every AI thread in a channel (admin) |
| `/nugget` | a random nugget fact |

Image generation uses `google/gemini-3.1-flash-lite-image` by default:

```dotenv
IMAGINE_MODEL=google/gemini-3.1-flash-lite-image
IMAGINE_FALLBACK_MODEL=google/gemini-2.5-flash-image
```

Aspect ratios: `--square` (default), `--wide` / `--landscape`, `--tall` / `--portrait`, or `--ratio 4:3`. Example: `/imagine a nugget astronaut on the moon --wide`.

If `/imagine` returns `missing_scope`, reinstall the app so it picks up the manifest's `files:write` scope.

### Tools

| tool | what it does | who |
|---|---|---|
| `get_weather` | current weather + 1–7 day forecast via open-meteo | anyone |
| `roll_dice` | `d20`, `2d6+3`, `coin` | anyone |
| `nugget_fact` | random nugget trivia | anyone |
| `remind` | create one-off/recurring reminders, DM or channel | owner/admin |
| `list_reminders` / `cancel_reminder` | manage reminders | owner/admin |
| `search_slack` | search workspace messages | needs user token |
| `lookup_user` / `lookup_channel` | resolve names to real ids | anyone |
| `get_channel_history` | read recent channel messages | owner/admin |
| `send_dm` | DM someone as the bot | owner/admin |
| `remember` / `recall` | durable notes + past AI turns | owner/admin |
| `post_message` | post as the bot to a channel | owner/admin |
| `pending_access_requests` | list pending tier 2 requests | owner/admin |
| `get_stats` | workspace stats | owner/admin |
| `fetch_url` | read a public page as text (SSRF guarded) | owner/admin |
| `search_web` | Tavily web search, if `TAVILY_API_KEY` is set | owner/admin |

Owner-only is enforced against the real Slack sender id in code, not just in the prompt.

## Commands

Everything is a subcommand of `/nugget`.

| command | what it does | who |
|---|---|---|
| `/nugget` | random nugget fact | anyone |
| `/nugget help` | command list | anyone |
| `/nugget ping` | bot + Slack API latency | anyone |
| `/nugget id` | channel/user/team ids and feature flags for this channel | anyone |
| `/nugget access` | open the tier 2 request modal | anyone |
| `/nugget accesspanel [channel]` | post/update the access panel | admin |
| `/nugget pending` | list pending tier 2 requests | admin |
| `/nugget reminder <when> <text>` | create a DM reminder | anyone |
| `/nugget reminder <#channel> <when> <text>` | channel reminder | admin |
| `/nugget schedule <#channel> <when> <text>` | same, with a required channel | admin |
| `/nugget reminders [all]` | list active reminders | anyone / admin |
| `/nugget cancel <id>` | cancel a reminder | owner / admin |
| `/nugget poll "q" "a" "b"` | live button poll (2–5 options) | anyone |
| `/nugget pollclose <id>` | close a poll | creator / admin |
| `/nugget roll 2d6+3` | dice | anyone |
| `/nugget choose a \| b \| c` | random pick | anyone |
| `/nugget stats` | workspace stats | admin |
| `/nugget budget` | today's AI spend, remaining budget, and limits | admin |
| `/nugget digest` | build the daily digest now | admin |
| `/nugget recap` | post the daily recap prompt now | admin |
| `/nugget ask <question>` | one-shot AI answer | anyone in `AI_CHANNELS` |
| `/nugget summarize [n]` | AI catch-up for this channel | anyone in `AI_CHANNELS` |
| `/nugget imagine <prompt>` | generate and upload an image | admin |
| `/nugget forget` | wipe stored AI memory | DM / admin |
| `/nugget pinggroup [join\|leave]` | join, leave, or check the ping group | anyone |
| `/nugget whois @user` | user lookup | anyone |

Admins are `OWNER_USER_ID` plus anyone in `ADMIN_USER_IDS`.

### Reminder syntax

```text
10m stretch
in 2 hours meeting
1d 2h 30m big thing
tomorrow 09:00 standup
today 18:30 dinner
mon 09:00 weekly sync
2026-11-10 09:00 one-off
daily 08:30 good morning
weekly mon 18:00 weekly recap
every 2h drink water
```

Times use `TIMEZONE`. The first time expression is removed from the front; everything after it is the reminder body. `/reminders` shows ids and next run times.

## Environment reference

| variable | default | meaning |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | bot token (`xoxb-…`), required |
| `SLACK_APP_TOKEN` | — | app-level socket mode token (`xapp-…`), required |
| `SLACK_USER_TOKEN` | — | optional user token (`xoxp-…`) for search + usergroups |
| `OWNER_USER_ID` | — | your Slack member id, required |
| `ADMIN_USER_IDS` | owner | comma-separated extra admins |
| `BOT_NAME` | `Chicen Nugget Bot` | display name used in messages |
| `TIMEZONE` | `UTC` | IANA timezone for reminders/digest |
| `DATA_DIR` / `LOG_DIR` | `data` / `logs` | storage paths |
| `WATCHED_CHANNEL_IDS` | — | welcome/leave channels; `*` for every channel |
| `WELCOME_PUBLIC` | `true` | post a public welcome |
| `WELCOME_MESSAGE` | welcome text | supports `{user}`, `{channel}`, `{bot}`, `{name}` |
| `WELCOME_DM` | onboarding text | same placeholders; sent once per member by default |
| `WELCOME_DM_ONCE` | `true` | only DM a member on their first join |
| `WELCOME_PING` | — | raw Slack usergroup mention appended to the public welcome |
| `LOGS_CHANNEL_ID` | — | join/leave/access audit log |
| `USERGROUP_ID` | — | ping group to offer joiners (needs user token) |
| `USERGROUP_PROMPT` | `true` | DM joiners and let them opt in; `false` = auto-add |
| `USERGROUP_PROMPT_TEXT` | built-in | opt-in DM copy; supports `{user}`, `{channel}`, `{bot}` |
| `USERGROUP_PROMPT_ONCE` | `true` | only ask each person once |
| `ACCESS_CHANNEL_ID` | — | private channel to invite approved members to |
| `ACCESS_PANEL_CHANNEL_ID` | first watched | where the request panel lives |
| `ACCESS_PANEL_TEXT` | built-in | panel copy |
| `ACCESS_COOLDOWN_HOURS` | `24` | minimum gap between requests |
| `ADMIN_CHANNEL_ID` | — | shared approval channel; empty = DM each admin |
| `ACCESS_AUTO_APPROVE` | `false` | invite requesters immediately instead of queuing |
| `USERGROUP_GUARD` | `true` | DM people who reply under a usergroup ping |
| `THREAD_HELPER` | `true` | post 🧵 thread anchors / yes-no prompt for pings |
| `RECAP_CHANNEL_ID` | — | public daily recap channel; empty disables recaps |
| `RECAP_PRIVATE_CHANNEL_ID` | — | optional private follow-up recap channel |
| `RECAP_TIME` | — | `HH:MM` in `TIMEZONE` for the public recap |
| `RECAP_REMINDER_MINUTES` | `10` | heads-up DM before the recap; `0` disables |
| `RECAP_PING` | — | usergroup id to ping when the public recap is done |
| `RECAP_PRIVATE_PING` | — | usergroup id to ping when the private recap is done |
| `AI_API_KEY` | — | enables the AI agent |
| `AI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL |
| `AI_MODEL` | `openai/gpt-5.6-luna` | model id (must support tools) |
| `AI_FALLBACK_MODEL` | `anthropic/claude-haiku-5.5` | retried automatically when the primary model fails |
| `AI_MAX_TOKENS` | `1200` | max reply length |
| `AI_HISTORY_LIMIT` | `24` | conversation turns loaded per reply |
| `AI_MAX_CONTEXT_CHARS` | `24000` | trim older turns above this size |
| `AI_CHANNELS` | `*` | channels where mentions and `/ask` are answered |
| `AI_ALLOW_ADMINS` | `true` | let admins DM the agent too |
| `AI_RATE_LIMIT_PER_MINUTE` | `12` | per-user AI request cap; `0` disables |
| `AI_GLOBAL_RATE_LIMIT_PER_MINUTE` | `30` | workspace-wide AI request cap; `0` disables |
| `AI_USER_DAILY_BUDGET_USD` | `0.50` | per-user UTC-day spend cap; owner exempt; `0` disables |
| `AI_DAILY_BUDGET_USD` | `0` | optional bot-wide UTC-day stop; off by default |
| `AI_GATE_ENABLED` | `true` | ask jev whether to reply in ambient channels |
| `AI_GATE_MODEL` | `jev-latest` | System One model used for gate decisions |
| `AI_GATE_TIMEOUT` | `8` | seconds before the gate fails closed |
| `AI_JEV_URL` | `<AI_BASE_URL>/jev/systemone` | Jev endpoint override |
| `AI_GATE_MIN_CONFIDENCE` | `0.5` | min `should_reply` probability to chime in |
| `AI_GATE_ADDRESSED_CONFIDENCE` | `0.6` | min probability the message is for the bot |
| `AI_GATE_SPAM_THRESHOLD` | `0.7` | spam probability that always suppresses a reply |
| `AI_AMBIENT_CHANNELS` | — | channels where the bot may reply unprompted; `*` = all |
| `AI_AMBIENT_COOLDOWN` | `120` | min seconds between unprompted replies per channel |
| `IMAGINE_MODEL` | `google/gemini-3.1-flash-lite-image` | image model for `/imagine`; empty disables |
| `IMAGINE_FALLBACK_MODEL` | `google/gemini-2.5-flash-image` | retried when image generation fails |
| `AI_IMAGE_COST_USD` | `0.04` | rough per-image charge recorded against the daily budget |
| `DAILY_DIGEST_TIME` | — | `HH:MM` in `TIMEZONE`; empty disables |
| `WEATHER_DEFAULT_LOCATION` | — | default place for the weather tool |
| `TAVILY_API_KEY` | — | enables `search_web` |
| `MAX_REMINDERS` | `200` | active reminder cap |

## Optional user token

`SLACK_USER_TOKEN` is only used for:

- `search.messages` (tool: `search_slack`), scope `search:read`
- adding/removing users from a usergroup, scopes `usergroups:read` + `usergroups:write`

The manifest requests those user scopes. Some workspaces do not expose a user token for internal apps; in that case the bot still works, those two features just report that the token is missing. This project intentionally does **not** depend on a user account for normal bot events, which keeps it reliable and within Slack's normal app model.

> The original `noodle` project runs as a dedicated user account. That gives a more "human" presence but is fragile and not the standard Slack app path. This bot keeps the same personality/agent ideas on a normal bot identity, with an optional user token only for reading/search.

## Docker

```bash
mkdir -p data logs
docker compose up -d --build
docker compose logs -f
```

The container reads `.env` and bind-mounts `./data` and `./logs`. The container runs as a non-root user, so if the host directories are created by Docker as root, fix permissions once:

```bash
sudo chown -R 10001:10001 data logs
```

## Hosting notes

Socket mode needs an outbound WebSocket, not an inbound port. It runs fine on a laptop, Raspberry Pi, small VPS, Fly.io, Railway, etc. Keep exactly **one** process running per app token. For systemd:

```ini
[Unit]
Description=Chicen Nugget Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/chicen-nugget-bot
ExecStart=/opt/chicen-nugget-bot/.venv/bin/python /opt/chicen-nugget-bot/main.py
Restart=always
RestartSec=5
User=nugget

[Install]
WantedBy=multi-user.target
```

### Deploying with GitHub Actions

`.github/workflows/deploy.yml` rsyncs the repo to the host, writes `.env` from the
`ENV_FILE` secret, installs requirements and restarts the unit. It runs on every
push to `main`, and on demand:

```bash
gh workflow run deploy.yml
gh run watch
```

Secrets it needs: `SSH_PRIVATE_KEY` (a key that can log in to the host) and
`ENV_FILE` (the full contents of `.env`). This is handy when your network blocks
outbound SSH — the runner connects for you.

To read the log from anywhere, `.github/workflows/logs.yml` tails it the same way:

```bash
gh workflow run logs.yml -f lines=200
gh run watch
```

## Data & privacy

- Everything is stored locally in `data/bot.db` (SQLite): members/join stats, access requests, reminders, polls, AI conversation turns and notes.
- The AI agent sends the current conversation and tool results to your configured AI endpoint.
- Weather uses open-meteo (no key). Optional web search uses Tavily. Optional URL fetch is SSRF-guarded and never follows redirects.
- No analytics, no telemetry, no third-party database.
- Delete `data/bot.db` to wipe everything. Back it up by copying the file (and its `-wal`/`-shm` siblings while stopped).

## Troubleshooting

| symptom | fix |
|---|---|
| `Sending messages to this app has been turned off` | **App Home → Messages Tab** → enable **Allow users to send messages from the Messages tab** (also in the manifest as `features.app_home.messages_tab_enabled`). |
| `missing_scope` | add the scope in **OAuth & Permissions**, reinstall the app, restart. The manifest already includes what the built-in features need. |
| `/imagine` says `missing_scope` | the app was installed before `files:write` was added; reinstall from the manifest. |
| AI says budget used up (`402`) | Hack Club AI's daily limit reset at 00:00 UTC, or use a different endpoint/key. |
| AI says rate limited (`429`) | wait the `Retry-After` time, or lower `AI_RATE_LIMIT_PER_MINUTE`. |
| `not_in_channel` when approving access | invite the bot to the private tier 2 channel. |
| slash commands say `dispatch_failed` | the bot process is down, or socket mode/app token is wrong. |
| welcome DM never arrives | the member may have DMs from apps restricted; check logs. Also `WELCOME_DM_ONCE=true` only sends on the first join. |
| AI ignores channel mentions | channel must be in `AI_CHANNELS`; mention with the bot's real `@handle`; make sure the model supports tool calling. |
| AI errors about tools | pick a tool-capable model in `AI_MODEL`. |
| reminders fire at the wrong time | set `TIMEZONE` to your IANA zone and restart; recurring reminders keep their wall-clock time. |
| `invalid_auth` | regenerate the bot token after reinstalling and update `.env`. |
| user-token features missing | set `SLACK_USER_TOKEN` with `search:read` and usergroup scopes, or ignore them. |

Logs go to the console and `logs/bot.log` (rotating).

## Project layout

```
main.py                     entrypoint
slack-app-manifest.json     paste into api.slack.com/apps
bot/
  app.py                    builds Bolt, authenticates, starts socket mode + scheduler
  config.py                 env settings
  db.py                     SQLite schema + queries
  handlers.py               event registration
  runtime.py                shared app/db/client state
  reminders.py              reminder creation/formatting helpers
  slack_api.py              resilient Slack Web API wrappers
  ai/
    agent.py                OpenAI-compatible tool loop + memory
    budget.py               daily spend tracking and hard stops
    gate.py                 jev decisions: reply, addressed, spam, kind
    jev.py                  typed System One client (/jev/systemone)
    limits.py               per-user AI rate limiter
    tools.py                model-callable tools
  features/
    ai_commands.py          /ask, /summarize, /forget, /imagine
    welcomer.py             joins/leaves, welcome DM, ping-group opt-in
    access.py               tier 2 modal + approval + invite
    agent.py                Slack message/mention glue
    commands.py             slash commands
    nuggets.py              /nugget facts
    polls.py                button polls
    recaps.py               daily public/private recap flow
    scheduler.py            reminders + digest + recap loop
    stats.py                stats/digest formatting
    threads.py              usergroup ping guard + thread helper
  utils/
    timeparse.py            "when" parsing + recurrence math
    dice.py                 dice notation
    text.py                 chunking/mentions
prompts/system.md           agent persona
tests/                      pytest suite
data/                       SQLite (created at runtime, gitignored)
logs/                       bot.log (created at runtime, gitignored)
```

## Development

```bash
pip install -r requirements.txt
pip install pytest
pytest
```

The tests cover time parsing/recurrence, dice, text chunking, AI rate limits/commands, usergroup/thread helpers, recap scheduling and the SQLite layer. They do not need Slack credentials.

## Ideas for v2

- Reaction roles (emoji → usergroup), using the existing user-token path.
- Thread summaries and "catch me up" for a channel.
- Voice-note transcription.
- A small web dashboard for access requests and stats.
- Starboard / highlight feeds.
- Per-user quiet hours for proactive DMs.
- Embedding-based semantic memory search (the SQLite layer is already the single source of truth).

Have fun, be kind. MIT-style: do whatever you want with it.
