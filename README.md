# chicen nugget

A Slack bot with two faces.

The first is a normal workspace bot. It welcomes people, handles tier-2 access requests, runs reminders and polls, posts daily recaps, and has an AI agent with tools.

The second is optional: a real Slack account (call it Boi) that answers your DMs and shows a typing indicator, so it feels like a person instead of an app.

Both run in one Python process over Socket Mode. No public URL, no web server, no external database. Everything lives in SQLite.

## What's in the box

- Mentions, DMs, thread follow-ups and optional ambient replies, with tool calling and per-user memory.
- A second Slack identity for personal assistant work.
- Posting in the owner's name, either as a customized bot message or from their real account (optional).
- Jev, a small classifier that decides whether an unaddressed message actually deserves a reply.
- AI images, charts and PowerPoint decks uploaded straight into the conversation.
- Welcomer, access requests, recaps, reminders, polls and dice.
- Runtime settings you can change from chat instead of editing `.env`.

## How it fits together

```
Slack --socket mode--> bot account (xoxb / xapp) --> features, scheduler, slash commands
   |
   +--- websocket ----> user account (xoxc / xoxd) --> personal replies + typing
```

One process, one database, one toolbox. The bot account owns everything that needs app scopes: events, buttons, slash commands. The user account is a normal Slack session, so it can read what that user can read, post as itself, and send typing frames Slack renders natively.

The two identities can run different models. They share memory, notes, tasks and settings.

### The user account

Boi connects to `wss://wss-primary.slack.com` with a browser session token (`xoxc-...`) and the matching cookie (`xoxd-...`). It is not an app token and needs no OAuth install.

- **Typing:** while the model runs, the gateway sends a typing frame every 2.5 seconds.
- **Messages:** the socket delivers `message` events. The gateway ignores bots, its own messages, the bot account, duplicate timestamps and thread chatter that is not addressed to it.
- **Threads:** it follows a thread only if the agent already knows it. Otherwise it answers DMs and direct mentions.
- **Reconnects:** Slack sends a `reconnect_url` frame after connect. The gateway caches it, falls back to `client.getWebSocketURL` when it expires, and backs off exponentially up to 30 seconds.
- **Presence:** on connect it asks Slack for `auto` presence. Slack still marks human accounts away after about 10 minutes of no activity. There is no API to force a user account active.
- **Isolation:** the gateway never handles workspace admin actions. Those stay on the bot account, so a stolen user session cannot approve access or change settings.

Treat `xoxc` and `xoxd` like passwords. They grant full access to the account, including private channels. Rotate them by signing out and back in, then updating `.env`.

### Posting in the owner's name

`as_user=true` on `send_dm` or `post_message` makes a message look like it came from the owner, then adds a small muted `Sent by <@assistant>` line.

There are two modes:

- **Default:** the bot app posts with the owner's display name and profile picture using Slack's `chat:write.customize` scope. It looks like them, keeps the small `APP` badge, and needs no extra credentials. This is the same trick `hackclub/pixl` uses for dashboard ticket replies.
- **Native (optional):** if the owner adds their own user token with `/nugget setup slack` (or `SLACK_OWNER_TOKEN` / `SLACK_OWNER_COOKIE`), the same call posts from their real profile with no app badge.

The default mode needs `chat:write`, `chat:write.customize` and `users:read` on the bot app. Add those at api.slack.com/apps and reinstall the app. Native mode is validated against `OWNER_USER_ID` on save and on startup. If the token belongs to someone else, native mode is disabled and the default mode keeps working.

Boi's token can never post natively as the owner. Slack does not allow one account to post as another.

## Quick start

### 1. Create the Slack app

1. Open <https://api.slack.com/apps>, choose **Create New App**, then **From an app manifest**.
2. Pick the workspace and paste [`slack-app-manifest.json`](slack-app-manifest.json).
3. Install the app and approve the scopes.
4. In **App Home > Messages Tab**, enable **Allow users to send messages from the Messages tab**.

The manifest sets up Socket Mode, events, interactivity and the single `/nugget` command.

### 2. Grab the credentials

| value | where to find it |
|---|---|
| `SLACK_BOT_TOKEN` (`xoxb-...`) | OAuth & Permissions > Bot User OAuth Token |
| `SLACK_APP_TOKEN` (`xapp-...`) | Basic Information > App-Level Tokens, with `connections:write` |
| `OWNER_USER_ID` (`U...`) | your profile > More > Copy member ID |
| `SLACK_USER_TOKEN` | optional, see [the user account](#the-user-account) |
| `SLACK_USER_COOKIE` (`xoxd-...`) | optional, required alongside an `xoxc` token |
| `SLACK_OWNER_TOKEN` | optional, for native posts in the owner's name |
| `SLACK_OWNER_COOKIE` (`xoxd-...`) | optional, required alongside an `xoxc` owner token |

### 3. Configure

```bash
cp .env.example .env
```

The minimum looks like this:

```dotenv
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
OWNER_USER_ID=U012ABCDEF
TIMEZONE=Europe/Amsterdam
AI_API_KEY=sk-hc-v1-...
```

### 4. Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

A healthy start logs `online as bot user ...`. If the second identity is configured you also get `user token active as ...`.

### 5. Invite the bot

Invite it to the welcome, logs and panel channels, plus the private tier-2 channel. Slack only lets an app add members to a private channel it is already in.

## Setup from chat

You should not need to SSH into the VPS to connect personal integrations. Run `/nugget setup`, or ask the bot to open the setup panel. Each integration opens a Slack modal, so tokens and passwords never appear in chat.

| integration | command |
|---|---|
| Google Workspace | `/nugget setup google` |
| Mac companion | `/nugget setup mac` |
| GitHub | `/nugget setup github` |
| Email (IMAP) | `/nugget setup email` |
| Your Slack profile | `/nugget setup slack` |
| Status | `/nugget setup status` |
| Remove credentials | `/nugget setup disconnect <name>` |

Settings are stored in SQLite (`data/bot.db`) and override environment variables. Env values stay as a fallback for fresh deploys and CI.

### Google OAuth

Google uses a desktop-client flow so it works on a headless VPS:

1. Create a Google Cloud project and enable the Calendar, Gmail and Drive APIs.
2. Create an OAuth client of type **Desktop app**.
3. Add `http://localhost:9876/oauth2callback` as an authorized redirect URI.
4. Run `/nugget setup google`, paste the client id and secret, then open the authorization link on your own machine.
5. Google redirects to localhost and shows an error page. That is expected. Copy the full URL from the address bar and paste it into the setup modal.
6. The bot exchanges the code server-side and stores a refresh token. Calendar, Gmail and Drive become available right away.

Scopes: `calendar`, `gmail.modify`, `drive.readonly` and `userinfo.email`. To revoke access, remove the app from your Google account page and run `/nugget setup disconnect google`.

### Mac companion

Run `scripts/mac_companion.py` on the Mac, expose it with a Cloudflare Tunnel or ngrok, then run `/nugget setup mac`. It supports battery and volume checks, opening apps, files and URLs, notifications, clipboard, lock, sleep, speak, AppleScript and shell commands.

### GitHub and email

A GitHub token is optional for public reads. With a token you get notifications and a higher rate limit. IMAP works with Gmail and Fastmail app passwords. Use an app password, not your account password.

## Runtime settings

Settings live in SQLite and can be changed from chat with the `bot_config` tool or `/nugget config`. Owner and admins only. Secret values are rejected in chat, so tokens and app passwords have to go through the setup modals.

| key | effect |
|---|---|
| `recaps` | daily recap flow on or off |
| `daily_digest` | owner digest DM on or off |
| `personal_updates` | owner DMs for digest and recap reminders. public recaps keep running |
| `thread_followups` | unprompted replies in threads the bot is already in |
| `ambient_ai` | unprompted chime-ins in `AI_AMBIENT_CHANNELS` |
| `welcomer` | joins, leaves and onboarding DMs |
| `watched_channel_ids` | channels the welcomer watches. `*` means all |
| `tier2` | access request flow |
| `weather_location` | default location for weather |
| `ai_model` | bot app model |
| `ai_assistant_model` | Boi model |

Plain English works:

```text
turn off personal updates
enable thread followups
disable ambient
set weather_location to Amsterdam
```

`AI_THREAD_COOLDOWN` (120 seconds by default) limits how often an unprompted thread reply can happen. Mentions and DMs skip it.

## The AI agent

Any OpenAI-compatible endpoint with function calling works. The default is Hack Club AI.

```dotenv
AI_API_KEY=sk-hc-v1-...
AI_BASE_URL=https://ai.hackclub.com/proxy/v1
AI_MODEL=deepseek/deepseek-v4.1-flash
AI_FALLBACK_MODEL=anthropic/claude-haiku-5.5
AI_MAX_TOKENS=1200
AI_HISTORY_LIMIT=24
AI_CHANNELS=*
IMAGINE_MODEL=google/gemini-3.1-flash-image
IMAGINE_FALLBACK_MODEL=google/gemini-2.5-flash-image
```

The fallback model is used when the primary errors or times out. Conversation history and notes live in `data/bot.db`.

### Tools

| group | tools |
|---|---|
| web and research | `search_web`, `fetch_url`, `http_status`, `hacker_news`, `ocr_document` |
| code and dev | `run_code`, `github_info`, `hackatime_stats`, `system_diagnostics` |
| slack | `search_slack`, `fetch_slack_message`, `summarize_thread`, `get_channel_history`, `lookup_user`, `lookup_channel`, `send_dm`, `post_message`, `slack_status`, `slack_dnd`, `slack_react`, `slack_activity` |
| personal operator | `google_calendar`, `gmail`, `google_drive`, `mac_control`, `check_emails`, `daily_briefing`, `hackatime_stats` |
| setup | `integration_setup` |
| workspace | `flaron`, `pending_access_requests`, `get_stats`, `bot_config` |
| personal | `remember`, `recall`, `manage_tasks`, `remind`, `list_reminders`, `cancel_reminder` |
| media | `generate_image`, `make_chart`, `make_deck` |
| misc | `get_weather`, `public_holidays`, `roll_dice`, `dog_fact`, `taco_recipe`, `urban_dict`, `nugget_fact` |

Owner and admin checks run in code against the real Slack sender. The prompt cannot grant permissions on its own.

`flaron` also looks up workspace emojis, which is handy when you want a custom one to render.

### Artifacts

- **Charts:** `make_chart` renders bar, line, pie, scatter and histogram charts with matplotlib, then uploads a PNG.
- **Decks:** `make_deck` builds a real PPTX with python-pptx. Title slide plus title and bullet slides, optional speaker notes, capped at 30 slides.
- **Images:** `generate_image`, or `/imagine`, uploads an AI image using the configured image model.

Uploads land in the current channel or thread. Link unfurling is off so conversations stay compact.

## The Jev gate

A bot that replies to everything is noise. Jev is a typed classifier: no prose, just a probability or choice per question in one pass. For an unaddressed message the bot asks six questions.

| question | type | purpose |
|---|---|---|
| `should_reply` | `noul` | would a reply actually add something? |
| `addressed` | `noul` | is the message aimed at the bot? |
| `spam` | `noul` | is it spam, a mass ping or a link dump? |
| `needs_tools` | `noul` | does a correct answer need live data or an action? |
| `kind` | `choice` | question, help, feedback, celebration, bot, chatter, spam, other |
| `recipient` | `choice` | bot, someone_else, everyone |

Decision rule:

```text
reply = not spam
        and (aimed_at_bot or (worth_replying and recipient != someone_else))
```

A message aimed at another person never triggers an agent turn, even in a thread the bot is in, unless the bot is also addressed. `needs_tools` becomes a hint in the turn prompt so the model fetches data instead of guessing.

```dotenv
AI_GATE_ENABLED=true
AI_GATE_MODEL=jev-latest
AI_GATE_TIMEOUT=8
AI_JEV_URL=
AI_GATE_MIN_CONFIDENCE=0.5
AI_GATE_ADDRESSED_CONFIDENCE=0.6
AI_GATE_SPAM_THRESHOLD=0.7
AI_AMBIENT_CHANNELS=
AI_AMBIENT_COOLDOWN=120
AI_THREAD_COOLDOWN=120
```

Mentions and DMs skip the gate. Errors and timeouts fail closed. `AI_AMBIENT_CHANNELS=*` turns on ambient replies everywhere the bot is a member.

## Spend limits

Every completion is priced and recorded: chat, Jev calls, images and token-billed tools. Hack Club AI also enforces an account-wide daily allowance.

```dotenv
AI_USER_DAILY_BUDGET_USD=0.50   # owner exempt; 0 disables
AI_DAILY_BUDGET_USD=0            # optional bot-wide stop
AI_RATE_LIMIT_PER_MINUTE=12
AI_GLOBAL_RATE_LIMIT_PER_MINUTE=30
AI_MAX_CONTEXT_CHARS=24000
AI_IMAGE_COST_USD=0.04
```

Three limits work together: per-user rate limit, workspace rate limit and a per-user daily cap. `/nugget budget` shows your spend, the workspace total and the top users for the day.

## Commands

Everything lives under `/nugget` so it never collides with another app.

| command | what it does | who |
|---|---|---|
| `/nugget` | random nugget fact | anyone |
| `/nugget help` | command list | anyone |
| `/nugget ping` | bot and Slack API latency | anyone |
| `/nugget id` | ids and feature flags for this channel | anyone |
| `/nugget access` | open the tier-2 request modal | anyone |
| `/nugget accesspanel [channel]` | post or update the access panel | admin |
| `/nugget pending` | list pending tier-2 requests | admin |
| `/nugget reminder <when> <text>` | create a DM reminder | anyone |
| `/nugget reminder <#channel> <when> <text>` | channel reminder | admin |
| `/nugget reminders [all]` | list active reminders | anyone / admin |
| `/nugget cancel <id>` | cancel a reminder | owner/admin |
| `/nugget poll "q" "a" "b"` | live button poll, 2 to 5 options | anyone |
| `/nugget pollclose <id>` | close a poll | creator/admin |
| `/nugget roll 2d6+3` | dice | anyone |
| `/nugget choose a \| b \| c` | random pick | anyone |
| `/nugget stats` | workspace stats | admin |
| `/nugget budget` | AI spend and limits | admin |
| `/nugget digest` | build the daily digest now | admin |
| `/nugget recap` | post the daily recap prompt now | admin |
| `/nugget ask <question>` | one-shot AI answer | anyone in `AI_CHANNELS` |
| `/nugget summarize [n]` | AI catch-up for this channel | anyone in `AI_CHANNELS` |
| `/nugget imagine <prompt>` | generate and upload an image | admin |
| `/nugget forget` | wipe stored AI memory | DM / admin |
| `/nugget pinggroup [join\|leave]` | manage the ping group | anyone |
| `/nugget config` | view or change runtime settings | admin |
| `/nugget setup` | connect Google, Mac, GitHub, email or your Slack profile | admin |
| `/nugget whois @user` | user lookup | anyone |

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

Times use `TIMEZONE`. The first time expression is removed from the front, and the rest becomes the reminder body.

## Environment reference

Core:

| variable | default | meaning |
|---|---|---|
| `SLACK_BOT_TOKEN` | none | bot token (`xoxb-...`), required |
| `SLACK_APP_TOKEN` | none | Socket Mode token (`xapp-...`), required |
| `OWNER_USER_ID` | none | owner Slack member id, required |
| `ADMIN_USER_IDS` | owner | extra admins, comma separated |
| `BOT_NAME` | `Chicen Nugget Bot` | name used in messages |
| `TIMEZONE` | `UTC` | IANA timezone for reminders and recaps |
| `DATA_DIR` / `LOG_DIR` | `data` / `logs` | storage paths |
| `SLACK_USER_TOKEN` | none | optional user token (`xoxp` or `xoxc`) |
| `SLACK_USER_COOKIE` | none | `d` cookie (`xoxd`) for `xoxc` sessions |
| `SLACK_OWNER_TOKEN` | none | optional owner token for native posts in their name |
| `SLACK_OWNER_COOKIE` | none | `d` cookie for an `xoxc` owner token |

Welcomer and access:

| variable | default | meaning |
|---|---|---|
| `WATCHED_CHANNEL_IDS` | none | join and leave channels. `*` for all |
| `WELCOME_PUBLIC` | `true` | post a public welcome |
| `WELCOME_MESSAGE` | built in | supports `{user}`, `{channel}`, `{bot}`, `{name}` |
| `WELCOME_DM` | built in | onboarding DM, sent once by default |
| `WELCOME_DM_ONCE` | `true` | only DM a member on their first join |
| `WELCOME_PING` | none | raw usergroup mention appended to the welcome |
| `LOGS_CHANNEL_ID` | none | audit log channel |
| `USERGROUP_ID` | none | ping group, needs a user token |
| `USERGROUP_PROMPT` | `true` | ask joiners to opt in. `false` adds them automatically |
| `USERGROUP_GUARD` | `true` | DM people who reply under a usergroup ping |
| `THREAD_HELPER` | `true` | post thread anchors and yes/no prompts for pings |
| `ACCESS_CHANNEL_ID` | none | private channel for approved members |
| `ACCESS_PANEL_CHANNEL_ID` | first watched | where the request panel lives |
| `ACCESS_COOLDOWN_HOURS` | `24` | minimum gap between requests |
| `ADMIN_CHANNEL_ID` | none | shared approval channel. empty DMs each admin |
| `ACCESS_AUTO_APPROVE` | `false` | invite requesters immediately |

Recap and digest:

| variable | default | meaning |
|---|---|---|
| `RECAP_CHANNEL_ID` | none | public recap channel. empty disables recaps |
| `RECAP_PRIVATE_CHANNEL_ID` | none | optional private follow-up |
| `RECAP_TIME` | none | `HH:MM` in `TIMEZONE` |
| `RECAP_REMINDER_MINUTES` | `10` | heads-up DM. `0` disables |
| `RECAP_PING` / `RECAP_PRIVATE_PING` | none | usergroups pinged when a recap is done |
| `DAILY_DIGEST_TIME` | none | `HH:MM` for the owner digest. empty disables |

AI:

| variable | default | meaning |
|---|---|---|
| `AI_API_KEY` | none | enables the agent |
| `AI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL |
| `AI_MODEL` | `deepseek/deepseek-v4.1-flash` | bot app model, must support tools |
| `AI_FALLBACK_MODEL` | `anthropic/claude-haiku-5.5` | bot app fallback |
| `AI_ASSISTANT_MODEL` | `anthropic/claude-sonnet-5` | Boi model |
| `AI_ASSISTANT_FALLBACK_MODEL` | `anthropic/claude-haiku-5.5` | Boi fallback |
| `AI_MAX_TOKENS` | `1200` | max reply length |
| `AI_HISTORY_LIMIT` | `24` | turns loaded per reply |
| `AI_MAX_CONTEXT_CHARS` | `24000` | trim older turns above this size |
| `AI_CHANNELS` | `*` | channels where mentions and `/ask` work |
| `AI_ALLOW_ADMINS` | `true` | admins may DM the agent |
| `AI_RATE_LIMIT_PER_MINUTE` | `12` | per-user cap. `0` disables |
| `AI_GLOBAL_RATE_LIMIT_PER_MINUTE` | `30` | workspace cap. `0` disables |
| `AI_USER_DAILY_BUDGET_USD` | `0.50` | per-user daily cap. owner exempt |
| `AI_DAILY_BUDGET_USD` | `0` | optional bot-wide daily stop |
| `AI_GATE_ENABLED` | `true` | run the Jev gate on unprompted messages |
| `AI_GATE_MODEL` | `jev-latest` | gate model id |
| `AI_GATE_TIMEOUT` | `8` | seconds before failing closed |
| `AI_JEV_URL` | `<AI_BASE_URL>/jev/systemone` | Jev endpoint override |
| `AI_GATE_MIN_CONFIDENCE` | `0.5` | minimum `should_reply` to chime in |
| `AI_GATE_ADDRESSED_CONFIDENCE` | `0.6` | minimum probability the message is for the bot |
| `AI_GATE_SPAM_THRESHOLD` | `0.7` | spam probability that always suppresses |
| `AI_AMBIENT_CHANNELS` | none | channels for unprompted replies. `*` means all |
| `AI_AMBIENT_COOLDOWN` | `120` | minimum seconds between ambient replies per channel |
| `AI_THREAD_COOLDOWN` | `120` | minimum seconds between unprompted thread replies |
| `IMAGINE_MODEL` | `google/gemini-3.1-flash-image` | image model. empty disables `/imagine` and `generate_image` |
| `IMAGINE_FALLBACK_MODEL` | `google/gemini-2.5-flash-image` | image fallback |
| `AI_IMAGE_COST_USD` | `0.04` | per-image budget charge |
| `WEATHER_DEFAULT_LOCATION` | none | default weather location |
| `TAVILY_API_KEY` | none | optional search backend |
| `MAX_REMINDERS` | `200` | active reminder cap |

Jev only gates ambient replies for the bot app. Boi DMs and explicit mentions skip it. Both models can be changed at runtime with `/nugget config ai_model ...` and `/nugget config ai_assistant_model ...`.

## Deployment

The included workflow ships the repo to a server over SSH and restarts a systemd unit.

1. Add repository secrets: `ENV_FILE` (the whole production `.env`), `SSH_HOST`, `SSH_USER`, `SSH_KEY`, `SSH_PORT` (default 22), `DEPLOY_PATH` and `SERVICE_NAME`.
2. Push to `main`. `.github/workflows/deploy.yml` rsyncs the tree, writes `.env`, reinstalls requirements and restarts the service.
3. Tail logs with the `logs` workflow or `journalctl -u <SERVICE_NAME> -f` on the host.

Secrets never enter the repo. `.env`, `data/` and `logs/` are gitignored.

## Data and privacy

- **Storage:** one SQLite file at `data/bot.db`. Conversation history, AI notes, reminders, polls, access requests, runtime settings and a key/value store.
- **Retention:** history is per conversation and trimmed to `AI_HISTORY_LIMIT` turns. `/nugget forget` wipes your stored AI memory. Delete `data/bot.db` to reset everything.
- **Logs:** stdout plus rotating files in `logs/`. Tool arguments and reply text can show up in debug logs, so do not run debug logging on a shared host.
- **Third parties:** prompts go to your configured model provider (Hack Club AI by default). Jev calls go to the same base URL. Tool calls reach whatever public service they query.
- **Prompt injection:** web pages, files and Slack messages are treated as untrusted data. They are never treated as instructions.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `invalid_auth` on start | stale bot token | reinstall the app and update `SLACK_BOT_TOKEN` |
| `not_authed` or `missing_scope` | scope or install missing | update scopes and reinstall |
| posting in your name fails | `chat:write.customize` or `users:read` missing | add both scopes and reinstall |
| bot ignores mentions | channel not in `AI_CHANNELS`, or the bot was never invited | check `/nugget id` |
| bot replies to everything | gate disabled or thresholds too low | set `AI_GATE_ENABLED=true` and raise `AI_GATE_MIN_CONFIDENCE` |
| bot is silent in threads | `thread_followups` off, or the cooldown is active | enable it and lower `AI_THREAD_COOLDOWN` |
| user account keeps disconnecting | expired `xoxc` or `xoxd` | sign out and in, refresh both values |
| no typing indicator | second identity not configured | set `SLACK_USER_TOKEN` and `SLACK_USER_COOKIE` |
| `enterprise_is_restricted` from `rtm.connect` | workspace blocks classic RTM | expected. the gateway uses the client WebSocket |
| charts or decks fail | missing optional libs | `pip install matplotlib python-pptx` |
| reminders never fire | wrong `TIMEZONE` | fix the timezone and recreate the reminder |
| approval button does nothing | bot is not in the admin channel | invite it or set `ADMIN_CHANNEL_ID` |

Run `python main.py` in the foreground first. Startup failures print a plain error before Socket Mode starts.

### Docker

```bash
docker compose up -d --build
docker compose logs -f
```

Mount `.env` and persist `data/` and `logs/` as volumes. The compose file matches the systemd setup: one process, Socket Mode, no exposed ports.

### Script

`./deploy-remote.sh user@host remote-dir` rsyncs the tree and restarts the `chicen-nugget-bot` systemd unit.

## Project layout

```text
main.py                    entry point
bot/
  app.py                   startup, Bolt wiring, task lifecycle, user gateway
  config.py                Settings, env parsing, allowlists
  runtime.py               shared state: db, agent, gate, clients, locks
  db.py                    SQLite schema, history, kv store, reminders
  handlers.py              socket mode event registration
  slack_api.py             post, react and upload helpers, retries, member checks
  user_gateway.py          user WebSocket, typing frames, dedup, reconnect
  reminders.py             reminder creation and delivery
  ai/
    agent.py               prompt assembly, history, tool loop, prompt digest
    tools.py               tool registry and implementations
    gate.py                Jev gate: questions, thresholds, decision
    jev.py                 typed Jev client
    budget.py / limits.py  spend tracking and rate limits
  features/
    router.py              slash command routing
    commands.py            command implementations
    agent.py               mention, DM, thread and ambient handling
    scheduler.py           reminder tick, digest, recap jobs
    welcomer.py            join and leave, onboarding DM, ping group
    access.py              tier-2 request flow
    recaps.py              daily recap and private follow-up
    polls.py, threads.py, stats.py, nuggets.py, ai_commands.py
prompts/system.md          agent system prompt
tests/                     pytest suite
.github/workflows/         deploy and log tailing
```

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./.venv/bin/pytest
./.venv/bin/ruff check bot tests
```

Tests are plain pytest with `asyncio.run`. No network access and no Slack credentials needed.

## Contributing

Open an issue or a pull request. Keep changes small, keep the gate failing closed, and add a test when the behaviour is not obvious.

## License

MIT. See [LICENSE](LICENSE).
