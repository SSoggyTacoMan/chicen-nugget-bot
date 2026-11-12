# Chicen Nugget Bot

A Slack workspace assistant with a pluggable AI agent, a dual-identity mode for human-feeling replies, and a small set of moderation and scheduling features. Built with Python, Slack Bolt, asyncio and SQLite in socket mode, so it runs without a public URL.

```
welcome / tier-2 access / ping group / daily recap
AI mentions + DMs + thread follows + ambient chime-ins
optional second identity: replies as a real user, with native typing indicators
```

## Features

- **AI agent** — mentions, DMs, thread follow-ups and optional ambient replies. Tool calling, SQLite memory, per-user budgets and rate limits. Works with any OpenAI-compatible endpoint; defaults to Hack Club AI.
- **Dual identity (optional)** — a second Slack account replies through a client WebSocket gateway, so messages look human and show native `… is typing`. The bot account keeps handling workspace events in parallel.
- **Owner-profile posting (optional)** — with the owner's own token, the agent can post in their name and adds a small muted `Sent by @assistant` line, so authorship is always clear.
- **Jev gate** — a typed classifier decides whether an unaddressed message deserves a reply, who it is aimed at, and whether answering needs live data. Fails closed.
- **Rich artifacts** — AI images, rendered charts (`make_chart` → PNG) and PowerPoint decks (`make_deck` → PPTX), uploaded into the conversation.
- **Welcomer** — public welcome, onboarding DM, optional ping-group opt-in.
- **Tier-2 access** — request modal, admin approve/deny buttons, automatic channel invite.
- **Daily recap** — scheduled prompt with a done button, optional private follow-up and usergroup ping.
- **Reminders, polls, dice** — one-off and recurring reminders, button polls with live tallies, dice notation.
- **Runtime settings** — recaps, digest, personal updates, thread follows, welcomer channels, tier-2, ambient AI, and per-identity models can be changed from chat.
- **Slack-native output** — mrkdwn sanitising, broadcast-ping disarming, link-unfurl suppression, chunking for long replies.

## Architecture

```
Slack ──socket mode──▶ bot account (xoxb / xapp) ──▶ features, scheduler, slash commands
   │
   └──client websocket──▶ user account (xoxc / xoxd) ──▶ personal assistant replies + typing
```

Both identities share one process, one SQLite database and one toolbox, but can run different models. The bot account owns everything that needs app scopes (events, buttons, slash commands). The user account is a normal Slack session: it can read what the user can read, post as itself, and emit typing frames the Slack client renders natively. Neither token can post as the owner — that needs the owner's own token, below.

### The user account

The user gateway connects to `wss://wss-primary.slack.com` using a browser session token (`xoxc-…`) and the matching cookie (`xoxd-…`). It is not an app token and needs no OAuth install.

- **Typing indicators** — while the model runs, the gateway sends `{"type": "typing", "channel": …}` frames every 2.5 seconds. Slack renders them as a normal human typing state.
- **Message flow** — the socket delivers `message` events. The gateway ignores bots, its own messages, the bot account, duplicate timestamps and thread chatter that is not addressed to it.
- **Threads** — if the thread is already known to the agent, the user account follows it; otherwise it answers mentions and DMs only.
- **Reconnects** — Slack sends a `reconnect_url` frame after connect. The gateway caches it and reconnects with it, falling back to `client.getWebSocketURL` when it expires. Exponential backoff caps at 30 seconds.
- **Presence** — on connect the gateway asks Slack for `auto` presence so the account does not look permanently away.
- **Isolation** — the user gateway never handles workspace admin actions. Those stay on the bot account, so a stolen user session cannot approve access or change settings.
- **Deployment** — set `SLACK_USER_TOKEN` and `SLACK_USER_COOKIE`; leave them empty to run bot-only.

> Session tokens expire when the browser session is revoked. Treat `xoxc` and `xoxd` as passwords: they grant full access to the account, including reading private channels. Rotate them by signing out and in again, then updating `.env`.

### Posting as the owner

`as_user=true` on `send_dm` / `post_message` posts through the owner's **own** Slack profile, then adds a small muted `Sent by <@assistant>` context line.

- Connect it with `/nugget setup slack` (recommended), or set `SLACK_OWNER_TOKEN` / `SLACK_OWNER_COOKIE`.
- Use the owner's own user token (`xoxp-…`, or `xoxc-…` plus the `d` cookie). A bot token cannot post as a person.
- The token is checked with `auth_test()` when saved and at startup; if it does not belong to `OWNER_USER_ID`, owner posting is disabled.
- If the token is missing, the tool fails clearly. It never silently falls back to Boi or the bot app.
- Slack does not allow one account to post as another, so Boi's `SLACK_USER_TOKEN` can never post as the owner.

## Setup from chat

You do not need to SSH into the VPS to connect personal integrations. Run `/nugget setup`, or ask the bot to open the setup panel. Each integration opens a Slack modal, so secrets are not posted in chat.

| integration | setup |
|---|---|
| Google Workspace | `/nugget setup google` |
| Mac companion | `/nugget setup mac` |
| GitHub | `/nugget setup github` |
| Email (IMAP) | `/nugget setup email` |
| Your Slack profile | `/nugget setup slack` |
| Status | `/nugget setup status` |
| Remove credentials | `/nugget setup disconnect <name>` |

Settings are stored in SQLite (`data/bot.db`) and override environment variables. Environment values remain a fallback for fresh deploys and CI.

### Google OAuth

Google uses a desktop-client OAuth flow, so it works on a headless VPS:

1. Create a Google Cloud project and enable the Calendar, Gmail, and Drive APIs.
2. Create an OAuth client of type **Desktop app**.
3. Add `http://localhost:9876/oauth2callback` as an authorized redirect URI.
4. Run `/nugget setup google`, paste the client id and secret, then open the DM authorization link on your own machine.
5. Google redirects to `localhost` and shows an error page. That is expected. Copy the full URL from the address bar and paste it into the setup modal.
6. The bot exchanges the code server-side and stores the refresh token. Calendar, Gmail, and Drive become available immediately.

The flow uses `calendar`, `gmail.modify`, `drive.readonly`, and `userinfo.email` scopes. Revoke access from your Google account page and run `/nugget setup disconnect google` to remove the stored token.

### Mac companion

Run `scripts/mac_companion.py` on the Mac, expose it through a Cloudflare Tunnel or ngrok, then `/nugget setup mac`. It supports battery/volume/status, open apps/files/URLs, notifications, clipboard, lock, sleep, speak, AppleScript, and shell commands.

### GitHub and email

A GitHub token is optional for public reads, but enables notifications and higher rate limits. IMAP works with Gmail/Fastmail app passwords; use an app password, not your account password.

## Quick start

### 1. Create the Slack app

1. Open <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Pick the workspace and paste [`slack-app-manifest.json`](slack-app-manifest.json).
3. Install the app and approve the requested scopes.
4. In **App Home → Messages Tab**, enable **Allow users to send messages from the Messages tab**.

The manifest enables socket mode, events, interactivity and the single `/nugget` slash command.

### 2. Collect credentials

| value | where |
|---|---|
| `SLACK_BOT_TOKEN` (`xoxb-…`) | OAuth & Permissions → Bot User OAuth Token |
| `SLACK_APP_TOKEN` (`xapp-…`) | Basic Information → App-Level Tokens, with `connections:write` |
| `OWNER_USER_ID` (`U…`) | your profile → More → Copy member ID |
| `SLACK_USER_TOKEN` | optional, see [the user account](#the-user-account) |
| `SLACK_USER_COOKIE` (`xoxd-…`) | optional, required alongside an `xoxc` token |
| `SLACK_OWNER_TOKEN` | optional, the owner's own token for posting in their name |
| `SLACK_OWNER_COOKIE` (`xoxd-…`) | optional, required alongside an `xoxc` owner token |

### 3. Configure

```bash
cp .env.example .env
```

Minimum:

```dotenv
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
OWNER_USER_ID=U012ABCDEF
TIMEZONE=Europe/Amsterdam
AI_API_KEY=sk-hc-v1-...
```

### 4. Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

A healthy start logs `online as bot user …`, plus `user token active as …` when the second identity is configured.

### 5. Invite the bot

Invite it to the welcome, logs and panel channels, and to the private tier-2 channel. Slack only lets an app add members to a private channel the app itself is already in.

## Personal controls

Settings live in SQLite and change from chat through the `bot_config` tool, or with `/nugget config`. Owner/admin only. Secret values (tokens, app passwords) are rejected in chat; connect those through `/nugget setup` modals instead.

| key | effect |
|---|---|
| `recaps` | daily recap flow on/off |
| `daily_digest` | owner digest DM on/off |
| `personal_updates` | owner DMs: digest and recap reminder. public recaps keep running |
| `thread_followups` | unprompted replies in threads the bot is already in |
| `ambient_ai` | unprompted chime-ins in `AI_AMBIENT_CHANNELS` |
| `welcomer` | joins/leaves and onboarding DMs |
| `watched_channel_ids` | channel ids the welcomer watches; `*` for all |
| `tier2` | access request flow |
| `weather_location` | default location for the weather tool |
| `ai_model` | bot app model (runtime override) |
| `ai_assistant_model` | Boi model (runtime override) |

Examples:

```text
turn off personal updates
enable thread followups
disable ambient
set weather_location to Amsterdam
```

`AI_THREAD_COOLDOWN` (default 120s) limits how often an unprompted thread reply can happen. Mentions and DMs bypass it.

## AI agent

The agent is provider-agnostic: any OpenAI-compatible chat completions endpoint with function calling works. Default is Hack Club AI.

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

The fallback model is tried when the primary errors or times out. Turns and notes live in `data/bot.db`.

### Tools

| group | tools |
|---|---|
| web/research | `search_web`, `fetch_url`, `http_status`, `hacker_news`, `ocr_document` |
| code/dev | `run_code`, `github_info`, `hackatime_stats`, `system_diagnostics` |
| slack | `search_slack`, `fetch_slack_message`, `summarize_thread`, `get_channel_history`, `lookup_user`, `lookup_channel`, `send_dm`, `post_message`, `slack_status`, `slack_dnd`, `slack_react`, `slack_activity` |
| personal operator | `google_calendar`, `gmail`, `google_drive`, `mac_control`, `check_emails`, `daily_briefing`, `hackatime_stats` |
| setup | `integration_setup` |
| workspace | `flaron`, `pending_access_requests`, `get_stats`, `bot_config` |
| personal | `remember`, `recall`, `manage_tasks`, `remind`, `list_reminders`, `cancel_reminder` |
| media | `generate_image`, `make_chart`, `make_deck` |
| misc | `get_weather`, `public_holidays`, `roll_dice`, `dog_fact`, `taco_recipe`, `urban_dict`, `nugget_fact` |

Owner/admin checks run in code against the real Slack sender, not only in the prompt.

### Artifacts

- **Charts** — `make_chart` renders bar, line, pie, scatter and histogram charts with matplotlib and uploads a PNG. Pass `labels`, `values`, optional `series`, a title and axis labels.
- **Decks** — `make_deck` builds a real PPTX with python-pptx: a title slide plus title/bullet slides with optional speaker notes, capped at 30 slides.
- **Images** — `generate_image` (or `/imagine`) uploads an AI image with the configured image model.

Uploads land in the current channel or thread. Link unfurling is disabled so conversations stay compact.

## The Jev gate

A bot that replies to everything is noise. Jev is a typed classifier: it writes no prose, it returns a probability or choice per question in one pass. For any unaddressed message the bot asks six questions.

| question | type | purpose |
|---|---|---|
| `should_reply` | `noul` | would a reply genuinely add something? |
| `addressed` | `noul` | is the message aimed at the bot? |
| `spam` | `noul` | is it spam, a mass ping, or a link dump? |
| `needs_tools` | `noul` | does a correct answer need live data or an action? |
| `kind` | `choice` | question, help, feedback, celebration, bot, chatter, spam, other |
| `recipient` | `choice` | bot, someone_else, everyone |

Decision rule:

```text
reply = not spam
        and (aimed_at_bot or (worth_replying and recipient != someone_else))
```

A message aimed at another person never triggers an agent turn, even in a thread the bot is part of, unless it is also addressed to the bot. `needs_tools` becomes a hint in the turn prompt so the model fetches data instead of guessing. `recipient`, `kind`, `bot_last` and `needs_tools` are summarised into one short hint line.

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

Mentions and DMs skip the gate. Errors and timeouts fail closed. `AI_AMBIENT_CHANNELS=*` enables ambient replies everywhere the bot is a member.

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

Three limits work together: per-user rate limit, workspace rate limit and a per-user daily cap. `/nugget budget` shows your spend, the workspace total and the day's top users.

## Commands

Everything lives under `/nugget`, so it never collides with other apps.

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
| `/nugget poll "q" "a" "b"` | live button poll, 2–5 options | anyone |
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
| `/nugget setup` | connect Google, Mac, GitHub, email, or your Slack profile | admin |
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

Times use `TIMEZONE`. The first time expression is removed from the front; the rest is the reminder body.

## Environment reference

Core:

| variable | default | meaning |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | bot token (`xoxb-…`), required |
| `SLACK_APP_TOKEN` | — | app-level socket mode token (`xapp-…`), required |
| `OWNER_USER_ID` | — | owner Slack member id, required |
| `ADMIN_USER_IDS` | owner | comma-separated extra admins |
| `BOT_NAME` | `Chicen Nugget Bot` | name used in messages |
| `TIMEZONE` | `UTC` | IANA timezone for reminders and recaps |
| `DATA_DIR` / `LOG_DIR` | `data` / `logs` | storage paths |
| `SLACK_USER_TOKEN` | — | optional user token (`xoxp`/`xoxc`) |
| `SLACK_USER_COOKIE` | — | `d` cookie (`xoxd`) for `xoxc` sessions |

Welcomer and access:

| variable | default | meaning |
|---|---|---|
| `WATCHED_CHANNEL_IDS` | — | join/leave channels; `*` for every channel |
| `WELCOME_PUBLIC` | `true` | post a public welcome |
| `WELCOME_MESSAGE` | built-in | supports `{user}`, `{channel}`, `{bot}`, `{name}` |
| `WELCOME_DM` | built-in | onboarding DM, sent once by default |
| `WELCOME_DM_ONCE` | `true` | only DM a member on their first join |
| `WELCOME_PING` | — | raw usergroup mention appended to the welcome |
| `LOGS_CHANNEL_ID` | — | audit log channel |
| `USERGROUP_ID` | — | ping group, needs a user token |
| `USERGROUP_PROMPT` | `true` | ask joiners to opt in; `false` = auto-add |
| `USERGROUP_GUARD` | `true` | DM people who reply under a usergroup ping |
| `THREAD_HELPER` | `true` | post thread anchors and yes/no prompts for pings |
| `ACCESS_CHANNEL_ID` | — | private channel for approved members |
| `ACCESS_PANEL_CHANNEL_ID` | first watched | where the request panel lives |
| `ACCESS_COOLDOWN_HOURS` | `24` | minimum gap between requests |
| `ADMIN_CHANNEL_ID` | — | shared approval channel; empty = DM each admin |
| `ACCESS_AUTO_APPROVE` | `false` | invite requesters immediately |

Recap and digest:

| variable | default | meaning |
|---|---|---|
| `RECAP_CHANNEL_ID` | — | public recap channel; empty disables recaps |
| `RECAP_PRIVATE_CHANNEL_ID` | — | optional private follow-up |
| `RECAP_TIME` | — | `HH:MM` in `TIMEZONE` |
| `RECAP_REMINDER_MINUTES` | `10` | heads-up DM; `0` disables |
| `RECAP_PING` / `RECAP_PRIVATE_PING` | — | usergroups pinged when a recap is done |
| `DAILY_DIGEST_TIME` | — | `HH:MM` for the owner digest; empty disables |

AI:

| variable | default | meaning |
|---|---|---|
| `AI_API_KEY` | — | enables the agent |
| `AI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL |
| `AI_MODEL` | `deepseek/deepseek-v4.1-flash` | bot app model, must support tools |
| `AI_FALLBACK_MODEL` | `anthropic/claude-haiku-5.5` | bot app fallback, retried on failure |
| `AI_ASSISTANT_MODEL` | `anthropic/claude-sonnet-5` | Boi model (personal DMs) |
| `AI_ASSISTANT_FALLBACK_MODEL` | `anthropic/claude-haiku-5.5` | Boi fallback |
| `AI_MAX_TOKENS` | `1200` | max reply length |
| `AI_HISTORY_LIMIT` | `24` | turns loaded per reply |
| `AI_MAX_CONTEXT_CHARS` | `24000` | trim older turns above this size |
| `AI_CHANNELS` | `*` | channels where mentions and `/ask` work |
| `AI_ALLOW_ADMINS` | `true` | admins may DM the agent |
| `AI_RATE_LIMIT_PER_MINUTE` | `12` | per-user cap; `0` disables |
| `AI_GLOBAL_RATE_LIMIT_PER_MINUTE` | `30` | workspace cap; `0` disables |
| `AI_USER_DAILY_BUDGET_USD` | `0.50` | per-user daily cap; owner exempt |
| `AI_DAILY_BUDGET_USD` | `0` | optional bot-wide daily stop |
| `AI_GATE_ENABLED` | `true` | run the Jev gate on unprompted messages |
| `AI_GATE_MODEL` | `jev-latest` | gate model id |
| `AI_GATE_TIMEOUT` | `8` | seconds before failing closed |
| `AI_JEV_URL` | `<AI_BASE_URL>/jev/systemone` | Jev endpoint override |
| `AI_GATE_MIN_CONFIDENCE` | `0.5` | min `should_reply` to chime in |
| `AI_GATE_ADDRESSED_CONFIDENCE` | `0.6` | min probability the message is for the bot |
| `AI_GATE_SPAM_THRESHOLD` | `0.7` | spam probability that always suppresses |
| `AI_AMBIENT_CHANNELS` | — | channels for unprompted replies; `*` = all |
| `AI_AMBIENT_COOLDOWN` | `120` | min seconds between ambient replies per channel |
| `AI_THREAD_COOLDOWN` | `120` | min seconds between unprompted thread replies |
| `IMAGINE_MODEL` | `google/gemini-3.1-flash-image` | image model; empty disables `/imagine` and `generate_image` |
| `IMAGINE_FALLBACK_MODEL` | `google/gemini-2.5-flash-image` | image fallback |
| `AI_IMAGE_COST_USD` | `0.04` | per-image budget charge |
| `WEATHER_DEFAULT_LOCATION` | — | default weather location |
| `TAVILY_API_KEY` | — | optional search backend |
| `MAX_REMINDERS` | `200` | active reminder cap |

Jev is only the ambient-reply gate for the bot app. Boi DMs and explicit mentions skip it. Both models can be changed at runtime with `/nugget config ai_model …` and `/nugget config ai_assistant_model …`.

## Deployment

The included workflow ships the repo to a server over ssh and restarts a systemd unit.

1. Add repository secrets:
   - `ENV_FILE` — the entire production `.env`.
   - `SSH_HOST`, `SSH_USER`, `SSH_KEY`, `SSH_PORT` (default 22).
   - `DEPLOY_PATH`, `SERVICE_NAME`.
2. Push to `main`. `.github/workflows/deploy.yml` rsyncs the tree, writes `.env`, reinstalls requirements and restarts the service.
3. Tail logs with the `logs` workflow or `journalctl -u <SERVICE_NAME> -f` on the host.

Secrets never enter the repo. `.env`, `data/` and `logs/` are gitignored.

## Data and privacy

- **Storage** — one SQLite file at `data/bot.db`: conversation history, AI notes, reminders, polls, access requests, runtime settings and a key/value store.
- **Retention** — history is per conversation and trimmed to `AI_HISTORY_LIMIT` turns. `/nugget forget` wipes your stored AI memory. Deleting `data/bot.db` resets everything.
- **Logs** — stdout plus rotating files in `logs/`. Tool arguments and reply text can appear in debug logs; do not enable debug logging on a shared host.
- **Third parties** — prompts go to your configured model provider (Hack Club AI by default), Jev calls go to the same base URL, tool calls reach the public services they query.
- **Prompt injection** — retrieved web pages, files and Slack messages are treated as untrusted data. They are never treated as instructions.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `invalid_auth` on start | stale bot token | reinstall the app, update `SLACK_BOT_TOKEN` |
| `not_authed` / `missing_scope` | scope or install missing | update scopes, reinstall |
| bot ignores mentions | channel not in `AI_CHANNELS`, or mention in a channel it was not invited to | check `/nugget id` |
| bot replies to everything | gate disabled or thresholds too low | `AI_GATE_ENABLED=true`, raise `AI_GATE_MIN_CONFIDENCE` |
| bot is silent in threads | `thread_followups` off or cooldown active | enable it, lower `AI_THREAD_COOLDOWN` |
| user account not connecting | expired `xoxc`/`xoxd` | sign out and in, refresh both values |
| no typing indicator | second identity not configured | set `SLACK_USER_TOKEN` and `SLACK_USER_COOKIE` |
| `enterprise_is_restricted` from `rtm.connect` | workspace blocks classic RTM | expected; the user gateway uses the client WebSocket |
| charts or decks fail | missing optional libs | `pip install matplotlib python-pptx` |
| reminders never fire | wrong `TIMEZONE` | fix the timezone and re-create the reminder |
| approval button does nothing | bot not in the admin channel | invite it, or set `ADMIN_CHANNEL_ID` |

Run `python main.py` in the foreground first; startup failures print a plain error before socket mode begins.

### Alternative: Docker

```bash
docker compose up -d --build
docker compose logs -f
```

Mount `.env` and persist `data/` and `logs/` as volumes. The compose file matches the systemd deployment: one process, socket mode, no exposed ports.

### Alternative: script

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
  slack_api.py             post/react/upload helpers, retries, member checks
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
    welcomer.py            join/leave, onboarding DM, ping group
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

Tests are plain pytest with asyncio.run, no network access and no Slack credentials.

## Contributing

Open an issue or a pull request. Keep changes small, keep the gate failing closed, and add a test when behaviour is non-obvious.

## License

MIT. See [LICENSE](LICENSE).
