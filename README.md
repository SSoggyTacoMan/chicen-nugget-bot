# chicen nugget

chicen nugget is a slack bot for a hack club workspace. it's one process with two slack identities: the workspace bot app, and boi, an optional personal assistant account that talks like a real user.

it's built to be useful without being loud. it answers when asked, stays out of conversations that aren't its, and keeps the weird stuff — custom welcome messages, ping-group buttons, huddle summaries, voice clips — one command away.

- the bot app handles channels, mentions, thread follow-ups, slash commands, welcomer, access requests, recaps, reminders and polls.
- boi is the personal assistant. give it a slack user token and it answers your dms, group dms and mentions with a typing indicator.

they share one sqlite database, one toolbox and one memory. socket mode only, so there's no public url, no web server and no separate database to babysit.

if something feels off, `/nugget doctor` is the first stop. it checks scopes, tokens, sqlite, browser, github, sandbox, storage and models.

## what it does

- mentions, name calls like "hey nugget", dms, group dms, thread follow-ups and optional ambient replies
- dm chats: one thread per conversation, with `/nugget chats`, `/nugget chat new` and follow-up detection
- secret chats: `/nugget secret` keeps a conversation in memory only, read-only tools, discreet notification previews and an opt-in `--burn`
- search across slack messages and files, plus channel join, leave, create, topic, invite and pin tools
- reaction shortcuts and message shortcuts for research, explain and thread summaries
- persistent code sandboxes with file edits, background jobs, uploads and branch push
- github reads and writes: prs, issues, comments, reviews, close/reopen, merge and push
- optional watchers: GitHub Actions failures, GitHub notifications, new email and URL uptime
- headless chromium for pages that need javascript, clicking or forms
- optional api tokens for vercel, cloudflare, notion, linear, sentry, resend, openai and anthropic
- separate bot, boi and code models, with fallbacks and runtime switching
- posting in the owner's name, either as a customized bot message or from their real account
- jev, a small classifier that decides whether an unaddressed message deserves a reply
- images, charts, powerpoint decks, recaps and polls
- custom welcome messages with per-channel overrides, thread replies and ping-group join/leave buttons
- timers, alarms, reminders, tasks and durable notes for boi and the bot
- post-huddle summaries from slack ai notes
- ordered 9router fallback models when hack club ai quota runs out
- a message starting with `# ` is ignored, always. manual mute switch

## how it fits

```text
slack --socket mode--> bot app (xoxb / xapp) --> features, scheduler, slash commands
  |
  +--- user websocket --> boi (xoxc / xoxd) --> personal replies + typing
```

the bot app owns everything that needs app scopes: events, buttons, slash commands. boi is a normal slack session, so it can see what that user sees, post as itself and send typing frames slack renders natively.

the accounts are separate tokens, but they share the same code, database and tools. boi can't act through the bot app, and the bot app can't act through boi.

### boi's account

boi connects to `wss://wss-primary.slack.com` with a browser session token (`xoxc-...`) and matching cookie (`xoxd-...`). that's not an app token and there's no oauth install.

you can split the credentials: `SLACK_USER_TOKEN` (ideally `xoxp`) handles api calls and search, while `SLACK_WEB_TOKEN` + `SLACK_WEB_COOKIE` handle presence and typing. if the web pair is empty, boi falls back to the user token and cookie.

- typing: a typing frame goes out every 2.5 seconds while the model runs.
- messages: bots, its own messages, the bot account, duplicate timestamps and unrelated thread chatter get ignored.
- group dms: boi answers when someone `@mentions` it. private dm history and group history stay separate.
- threads: in channels boi follows a thread it already answered, so a follow-up question doesn't need another ping. threads the bot app owns stay with the bot app. dm threads stay in the thread they started in.
- dm chats: each dm conversation gets its own thread, rooted at the message that started it. `/nugget chats` lists them, `/nugget chat new` starts one, and `/nugget secret` starts a chat that is never saved. secret replies use a generic notification fallback so previews don't show the text.
- context: mentions carry the last few channel or thread lines, so a one-word ping like "advice?" still knows what it's about.
- reconnects: slack sends a `reconnect_url` frame, and the gateway falls back to `client.getWebSocketURL` with exponential backoff.
- presence: it asks slack for `auto` presence. slack still marks human accounts away after about ten minutes.
- isolation: workspace admin actions stay on the bot app. a stolen user session can't approve access or change settings.
- boi has the same toolbox as the bot: reminders, timers, alarms, tasks, notes, calendar, email, weather, files and code.

treat `xoxc` and `xoxd` like passwords. they grant full access to the account, including private channels. to rotate them, sign out and back in, then update the env.

`AUTO_JOIN_CHANNELS` joins only the public channel ids you list, on startup. leave it empty and it joins nothing. no wildcard. `CHAT_CHUNK_DELAY_SECONDS` and `CHAT_CHUNK_MAX` control paragraph splitting. `QUIET_HOURS` and `PROACTIVE_DAILY_CAP` gate ambient replies.

### posting in the owner's name

`as_user=true` on `send_dm` or `post_message` posts with the owner's display name and avatar, then adds a small muted `Sent by` line.

- default: the bot app does it with slack's `chat:write.customize` scope. no personal token is needed. the message keeps the small `APP` badge; that's expected.
- native: if the owner adds their own user token with `/nugget setup slack`, the same call posts from their real profile with no badge.

if slack still shows the bot's own name and avatar, the app wasn't reinstalled after the scope was added. reinstall it. the native token is optional.

## quick start

### 1. make the slack app

1. open <https://api.slack.com/apps>, choose **create new app**, then **from an app manifest**.
2. pick the workspace and paste [`slack-app-manifest.json`](slack-app-manifest.json).
3. install the app and approve the scopes.
4. in **app home > messages tab**, enable **allow users to send messages from the messages tab**.

after changing scopes or events, paste the manifest again and reinstall. saving alone doesn't update the installed token.

### 2. grab the credentials

| value | where to find it |
|---|---|
| `SLACK_BOT_TOKEN` (`xoxb-...`) | oauth & permissions > bot user oauth token |
| `SLACK_APP_TOKEN` (`xapp-...`) | basic information > app-level tokens, with `connections:write` |
| `OWNER_USER_ID` (`U...`) | your profile > more > copy member id |
| `SLACK_USER_TOKEN` | optional, for boi (`xoxp` or `xoxc`) |
| `SLACK_USER_COOKIE` (`xoxd-...`) | optional, required with an `xoxc` boi token |
| `SLACK_WEB_TOKEN` | optional, `xoxc` session for typing/presence |
| `SLACK_WEB_COOKIE` (`xoxd-...`) | optional, required with `SLACK_WEB_TOKEN` |
| `SLACK_OWNER_TOKEN` | optional, for native posts in the owner's name |
| `SLACK_OWNER_COOKIE` (`xoxd-...`) | optional, required with an `xoxc` owner token |

### 3. configure

```bash
cp .env.example .env
```

the minimum is:

```dotenv
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
OWNER_USER_ID=U012ABCDEF
TIMEZONE=Europe/Amsterdam
AI_API_KEY=sk-hc-v1-...
```

the full list of variables, defaults and comments lives in [`.env.example`](.env.example). don't paste secrets into chat.

### 4. run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

a healthy start logs `online as bot user ...`. if boi is configured you also get `user token active as ...`.

### 4b. docker

```bash
cp .env.example .env
docker compose up -d --build
```

the compose file mounts `data/` and `logs/`, so sqlite and logs survive restarts. the image already installs chromium for the browser tools.

### 5. invite the bot

invite it to the welcome, logs and panel channels, plus the private tier-2 channel. slack only lets an app add members to a private channel it's already in.

## connect things from chat

run `/nugget setup`, or ask the bot to open the setup panel. secrets go into slack modals, never normal messages.

| integration | command |
|---|---|
| google workspace | `/nugget setup google` |
| mac companion | `/nugget setup mac` |
| github | `/nugget setup github` |
| email (imap) | `/nugget setup email` |
| api keys | `/nugget setup apis` |
| ai and behavior | `/nugget setup ai` |
| voice | `/nugget setup voice` |
| images | `/nugget setup images` |
| welcome & ping group | `/nugget setup welcome` |
| fallback ai | `/nugget setup fallback` |
| watchers | `/nugget setup watchers` |
| storage | `/nugget setup storage` |
| auto-join channels | `/nugget setup channels` |
| your slack profile | `/nugget setup slack` |
| status | `/nugget setup status` |
| remove credentials | `/nugget setup disconnect <name>` |

settings land in sqlite (`data/bot.db`) and override env values. env stays as a fallback for fresh deploys and ci.

the panel also has AI & behavior, voice, images, welcome, fallback ai, watchers, storage and auto-join. those write runtime settings instead of requiring an env edit.

### voice

incoming slack voice messages are transcribed automatically when you DM the bot, mention it, or reply in a thread. it uses `openai/gpt-audio` first, then `google/gemini-2.5-flash`. whisper is only tried on non-hack-club endpoints because the proxy doesn't expose `/audio/transcriptions`. make sure the app has `files:read` and is reinstalled after manifest changes.

outgoing voice uses qwen3 tts through hack club ai's replicate proxy:

- `/nugget speak <text>` generates a clip and posts it.
- asking the bot to "say it out loud" or "send a voice message" uses the `speak` tool.
- `/nugget setup voice` changes the preset voice, language, style, character cap and transcription models.
- clips post as playable audio files. slack's public api does not expose native voice-note bubbles for apps.

if tts is off, check `TTS_MODEL`, `AI_API_KEY` and `TTS_VOICE`.

### welcome & ping group

`/nugget setup welcome` edits the public welcome, welcome DM, watched channels, ping group id and prompt. placeholders are `{user}`, `{channel}`, `{bot}` and `{name}`.

options:

- auto-add new members to the ping group
- put a join/leave ping-group button in the public welcome or a thread reply
- send a welcome DM with a leave button when they were auto-added, or join/skip when they weren't
- per-channel welcome and DM overrides

per-channel overrides are optional: pick a channel and set a custom public message or DM. the channel also has to be in the watched list.

use `/nugget pinggroup panel` to post a permanent join/leave panel in a channel. the buttons work for everyone and reply privately.

### tier 2 access

`/nugget access` opens the request modal. requests go to `ADMIN_CHANNEL_ID` (or the access panel channel), and admins get approve/deny buttons. the cooldown is a **seven-day minimum** between requests. `/nugget pending` lists open requests.

the `manage_channel` tool can `kick` or `remove` a member from a channel when the owner/admin asks. it won't kick the owner, the bot, or the person asking.

### images

`/nugget setup images` picks the nano banana model, fallback and the per-image budget charge.

| option | model | tradeoff |
|---|---|---|
| nano banana 2 | `google/gemini-3.1-flash-image` | default, balanced |
| nano banana 2.1 | `google/gemini-nano-banana-2.1` | newer, better |
| nano banana pro | `google/gemini-3-pro-image` | best, slowest and priciest |
| nano banana 2 lite | `google/gemini-3.1-flash-lite-image` | cheapest and fastest |
| gpt image 2 | `openai/gpt-5.4-image-2` | OpenAI, strong |
| gpt image mini | `openai/gpt-5-image-mini` | OpenAI, cheaper |

the cost field only affects the bot's internal `/nugget budget`, not the provider's bill. runtime choices override `IMAGINE_MODEL` / `IMAGINE_FALLBACK_MODEL` / `AI_IMAGE_COST_USD`.

### huddles

slack has no public api to start or join a huddle. the bot can read the post-huddle AI transcript instead:

- `/nugget huddle` reads the latest huddle with AI notes in the current channel and summarizes it privately.
- it uses the same `xoxc`/`xoxd` web session as boi (`SLACK_WEB_TOKEN` / `SLACK_WEB_COOKIE`, or `SLACK_USER_TOKEN` / `SLACK_USER_COOKIE`).
- it only works when someone turned on slack AI notes for that huddle. no notes means no transcript.
- the endpoint is undocumented and can break if slack changes it.

live joining is not supported: slack does not expose huddle audio to apps, and bot users cannot join huddles.

### google

google uses a desktop oauth client so it works on a headless vps:

1. create a google cloud project and enable calendar, gmail and drive.
2. create an oauth client of type **desktop app**.
3. add `http://localhost:9876/oauth2callback` as a redirect uri.
4. run `/nugget setup google`, paste the client id and secret, then open the auth link on your own machine.
5. google redirects to localhost and shows an error page. copy the full url from the address bar and paste it into the modal.
6. the bot exchanges the code server-side and stores a refresh token.

scopes: `calendar`, `gmail.modify`, `drive.readonly`, `userinfo.email`. to revoke, remove the app from your google account and run `/nugget setup disconnect google`.

### mac

run `scripts/mac_companion.py --token <long-random-secret>` on the mac, expose it with a cloudflare tunnel or ngrok, then run `/nugget setup mac`. it can check battery and volume, open apps, files and urls, send notifications, read the clipboard, lock, sleep, speak and run applescript or shell commands.

the bridge is a root-equivalent capability, so it **requires** `--token`, listens on `127.0.0.1` by default, and refuses `run`/`applescript` unless you pass `--allow-shell`. only tunnel it if you accept that anyone with the token can run commands as your mac user.

### github

a github token is optional for public reads. with one you get notifications, a higher rate limit and `github_write` in dms:

- `create_pr`
- `create_issue`
- `comment`
- `review` with `APPROVE`, `REQUEST_CHANGES` or `COMMENT`
- `close` / `reopen`
- `merge`
- `push_branch` with `open_pr=true`

for writes, use a fine-grained token with **contents: write**, **pull requests: write** and **issues: write**. classic tokens need `repo`.

`close`, `merge` and `push_branch` require `confirm=true` and an explicit request from the owner. push blocks `main`/`master`, never force-pushes, and sends the token through `git askpass` so it doesn't land in `.git/config` or logs.

set `WATCH_GITHUB_CI=true` and the scheduler will DM you when a new Actions run fails in the configured repo. quiet hours and `PROACTIVE_DAILY_CAP` apply.

### watchers

all watchers are opt-in, baseline on first run, dedupe by id/uid, and honor quiet hours plus the daily cap.

```dotenv
WATCH_GITHUB_CI=true
WATCH_GITHUB_NOTIFICATIONS=true
WATCH_EMAIL=true
WATCH_URLS=https://example.com,https://status.example.com
```

- GitHub CI: new failed Actions runs.
- GitHub notifications: new unread notifications.
- Email: new unread IMAP mail with subject/from/snippet.
- URLs: DMs when a URL goes down or comes back.

intervals are separate: `WATCH_GITHUB_CI_INTERVAL`, `WATCH_GITHUB_NOTIFICATIONS_INTERVAL`, `WATCH_EMAIL_INTERVAL`, `WATCH_URL_INTERVAL`. email watcher needs `/nugget setup email`; GitHub ones need `/nugget setup github`.

### email

imap works with gmail and fastmail app passwords. use an app password, not your account password.

### api keys

`/nugget setup apis` stores optional tokens for vercel, cloudflare, notion, linear, sentry, resend, openai and anthropic. boi can call them with `api_request`, using the service's real docs for path and body. tokens live in sqlite and are never echoed back.

env fallbacks: `VERCEL_TOKEN`, `CLOUDFLARE_API_TOKEN`, `NOTION_TOKEN`, `LINEAR_API_KEY`, `SENTRY_AUTH_TOKEN`, `RESEND_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`.

mcp servers aren't wired yet. for now it's these presets plus the built-in google, github, email and mac integrations.

## models

| job | default | fallback |
|---|---|---|
| bot chat | `deepseek/deepseek-v4.1-flash` | `anthropic/claude-haiku-5.5` |
| boi quick chat | `anthropic/claude-haiku-5.5` | `openai/gpt-6-luna` → sonnet |
| boi chat | `anthropic/claude-sonnet-5.5` | `openai/gpt-6.1-sol` |
| code turns | `anthropic/claude-sonnet-5.5` | `openai/gpt-6.1-sol` |

short/simple Boi messages use the quick model (`AI_ASSISTANT_FAST_MODEL`, default `anthropic/claude-haiku-5.5`), then luna (`AI_ASSISTANT_FAST_FALLBACK_MODEL`), then the main Boi model; anything that needs tools, code or a longer answer goes to the main Boi model. set it empty to disable the fast path. `AI_CODE_MODEL` is set by default. a `sandbox`, `github_write`, `browser`, `api_request`, `run_code`, `inspect_code` or `clone_repo` call switches the rest of that turn to the code model. normal chat stays on the identity model.

### quota fallback

when hack club ai returns a quota or payment error, the bot marks the UTC day as empty, DMs the owner once, and switches to the 9router fallback. fallback models are tried in order; set them with `/nugget setup fallback` or `AI_ROUTER_MODELS` (comma-separated, default `kgw/kilo-auto/free`). the fallback is text-only with tools disabled, so voice, images and memory search still need HCAI quota. it resets at 00:00 UTC.

set the 9router key with `/nugget setup fallback` or `AI_ROUTER_API_KEY`. the key is stored in SQLite, never in chat or git.

semantic memory uses `AI_EMBEDDING_MODEL` (default `qwen/qwen3-embedding-8b`). new turns are embedded in the background, and `search_memory` searches by meaning. to index old history:

```text
/nugget memory backfill 500
```

or from the server:

```bash
.venv/bin/python scripts/backfill_memory.py 1000
```

`/nugget memory status` and `/nugget memory search <query>` are also there.

change them at runtime:

```text
/nugget config ai_model ...
/nugget config ai_assistant_model ...
/nugget config ai_code_model ...
```

### dm chats and secret chats

DMs used to be one endless conversation. Now each DM chat is its own thread, rooted at the message that started it, and `/nugget chats` keeps a small index.

- `/nugget chat new [title]` always starts a new thread. So does saying "new chat ...".
- otherwise a main-DM message continues the active chat if it lands within `DM_THREAD_MINUTES` (default 5) and looks like a follow-up ("and...", "what about...", a short reply to a bot question). A cold or unrelated message starts a new thread. `DM_THREAD_CLASSIFIER=true` lets Jev decide the borderline ones.
- replying inside a thread always stays in it.
- `/nugget forget` wipes the active chat; `/nugget forget all` wipes every DM chat.
- `DM_CHATS=false` restores the old single-timeline DM behavior. Secret chats still work and stay secret.

`/nugget secret [title]` starts a chat that is not saved:

- no rows in `messages`, no embeddings, no memory/notes in the prompt. Turns live in process memory (`SECRET_CHAT_TTL_MINUTES`, default 120) and a restart forgets them.
- the only thing on disk is a marker row `dm_secret_threads (user_id, thread_ts, expires_at)` that lasts until you end the chat. It stores no content; it stops a restart from silently treating an old secret thread as a normal, saved chat.
- tools are read-only: `SECRET_CHAT_TOOLS=web` (default) allows `search_web`, `fetch_url`, `get_weather`, `roll_dice` and `nugget_fact`; `none` disables tools. Nothing can post, save, schedule, email or upload.
- `SECRET_CHAT_DISCREET=true` sends the body in Block Kit with a generic `SECRET_PREVIEW_TEXT` fallback, so phone notifications, the DM list and thread previews don't show the message. The actual text is only in the opened message.
- `/nugget end` ends it and wipes memory. `/nugget end --burn` also deletes the bot's own messages with `chat.delete`. Your own messages stay: a bot token can't delete them.
- slack still stores the conversation, and the model provider still sees it. this is "the bot won't remember", not "nobody can see it".

## tools

| group | tools |
|---|---|
| web | `search_web`, `fetch_url`, `http_status`, `hacker_news`, `ocr_document`, `browser` |
| code | `inspect_code`, `clone_repo`, `sandbox`, `run_code`, `github_info`, `github_write`, `hackatime_stats`, `system_diagnostics` |
| slack | `search_slack`, `fetch_slack_message`, `summarize_thread`, `get_channel_history`, `lookup_user`, `lookup_channel`, `manage_channel`, `send_dm`, `post_message`, `slack_status`, `slack_dnd`, `slack_react`, `slack_activity` |
| personal | `google_calendar`, `gmail`, `google_drive`, `mac_control`, `check_emails`, `daily_briefing`, `api_request` |
| setup | `integration_setup` |
| workspace | `flaron`, `pending_access_requests`, `get_stats`, `bot_config` |
| memory | `remember`, `recall`, `forget`, `search_memory`, `manage_tasks`, `remind`, `list_reminders`, `cancel_reminder` |
| media | `generate_image`, `make_chart`, `make_deck` |
| misc | `get_weather`, `public_holidays`, `roll_dice`, `dog_fact`, `taco_recipe`, `urban_dict`, `nugget_fact` |

owner and admin checks happen in code against the real slack sender. the prompt can't grant permissions on its own. tools whose integration isn't connected are omitted from the model's tool list entirely.

`search_slack` uses the user session for real workspace search and falls back to local history. `manage_channel` lists, inspects, joins, leaves, creates, sets topic/purpose, invites, pins and lists members.

### sandbox

`sandbox` is a persistent workspace for actual dev work. create one, clone a repo, run commands, edit files and keep reading across turns. it stays on disk until you delete it. `clone_repo` is just a one-shot peek.

owner/admin only, dm only. it's a workspace boundary, not a security boundary: commands run as the same os user as the bot.

| action | what it does |
|---|---|
| `create` | make a named workspace. pass `repo_url` (`owner/repo` or an https git url) to clone into it |
| `exec` | run a shell command. pass `background=true` for long jobs |
| `read` | read a file with line numbers |
| `write` | create or overwrite a file. `append` adds to the end |
| `edit` | replace exact text in a file |
| `diff` | run `git diff` in the workspace |
| `upload` | send a file from the workspace into slack |
| `jobs` / `job` | list background jobs or check one by id |
| `list_files` | list a directory, skipping `.git` and build noise |
| `search` | text or regex search across the workspace |
| `info` / `list` | show workspace metadata and free disk |
| `delete` | remove the workspace and metadata. always needs a name |

`exec`, `read`, `write`, `edit`, `diff`, `upload`, `list_files`, `search` and `info` use the newest workspace when `name` is omitted. `delete` never guesses.

long commands can run in the background:

```text
boi: sandbox exec name=widget command="pytest -q" background=true
boi: sandbox job job_id=abc123
```

when the change is ready and github is connected:

```text
boi: sandbox diff name=widget
boi: github_write action=push_branch sandbox=widget branch=boi/fix-login preview=true
boi: github_write action=push_branch sandbox=widget branch=boi/fix-login message="fix login redirect" confirm=true open_pr=true title="Fix login redirect"
```

that commits the sandbox, pushes the branch and opens the pr.

workspaces live in `SANDBOX_DIR`, default `data/sandboxes`. `SANDBOX_MAX_WORKSPACES` caps how many stick around (20 by default). `SANDBOX_EXEC_TIMEOUT` is the default command timeout and `SANDBOX_MAX_EXEC_TIMEOUT` is the hard ceiling. child processes get `HOME` and `TMPDIR` inside the workspace and lose variables that look like tokens, keys, secrets, passwords, auth, ssh, cookies or credentials. that's hygiene, not isolation. don't run code you wouldn't run on the host.

```dotenv
SANDBOX_DIR=
SANDBOX_MAX_WORKSPACES=20
SANDBOX_EXEC_TIMEOUT=120
SANDBOX_MAX_EXEC_TIMEOUT=600
```

### storage

`/nugget storage` shows data, db, sandbox, browser and log sizes plus free disk. `/nugget storage prune` runs retention now.

retention defaults: conversation history and vectors older than 180 days (`MEMORY_RETENTION_DAYS`), dm chat rows and secret-chat markers older than that, sandboxes unused for 30 days (`SANDBOX_TTL_DAYS`), old job logs after 7 days, and sqlite backups after 7 days. logs already rotate at 2 MB with three backups. `0` disables a retention setting.

### browser

`browser` is a real headless chromium session for pages `fetch_url` can't handle. open a public page, read the rendered text, click, fill forms, press keys, wait for elements, go back/forward/reload and upload a screenshot.

owner/admin only, dm only. http/https only. localhost, private ips, link-local and cloud metadata addresses are blocked, along with `file://` and `javascript:`. it doesn't log into sites yet. sessions are capped and expire after 15 minutes idle.

```dotenv
BROWSER_ENABLED=true
BROWSER_HEADLESS=true
BROWSER_TIMEOUT=30
BROWSER_MAX_SESSIONS=2
BROWSER_IDLE_TIMEOUT=900
BROWSER_DIR=
```

install chromium once on the server:

```bash
.venv/bin/pip install -r requirements.txt
./scripts/install_browser.sh
```

if chromium is missing, the tool says so instead of taking the bot down.

### shortcuts

react with:

- `:mag:` to research a message
- `:thread:` to summarize it
- `:pushpin:` to pin it (owner/admin only)

message shortcuts: **explain this** and **summarize thread**. both answer in the thread.

## the jev gate

a bot that replies to everything is noise. jev is a typed classifier, not another chat model. for an unaddressed message it answers six questions in one pass:

| question | purpose |
|---|---|
| `should_reply` | would a reply add something? |
| `addressed` | is it aimed at the bot? |
| `spam` | is it spam, a mass ping or a link dump? |
| `needs_tools` | does a correct answer need live data or an action? |
| `kind` | question, help, feedback, celebration, bot, chatter, spam, other |
| `recipient` | bot, someone else, everyone |

decision:

```text
ambient: reply = not spam
        and (aimed_at_bot
             or (assist and worth_replying and recipient != someone_else))

assist = kind is help/feedback, or a question that needs live data/tools
```

a message aimed at another person never triggers an agent turn unless the bot is also addressed. short follow-ups in a thread where the bot spoke last bypass the gate. `needs_tools` becomes a hint so the model fetches data instead of guessing.

mentions carry the last few channel or thread lines as context, so a tagged one-word message ("advice?") can still see what it refers to. thread follow-ups work the same way, with a cooldown (`AI_THREAD_COOLDOWN`) and the same "is this actually for me" checks (`bot/features/followup.py`).

a thread can belong to both identities. whoever spoke last answers an unmentioned follow-up, so the bot app and boi never double up; saying `boi` by name keeps the bot app out of it. group DMs stay mention-only for boi.

the prompt also gives the model a public-channel veto: it can return `[NO_REPLY]` for messages that were not for it or would overshare. public replies containing sandbox, token, host, `.env`, sqlite, setup or model details are suppressed before posting.

mentions and dms skip the gate. errors and timeouts fail closed. `AI_AMBIENT_CHANNELS=*` turns ambient replies on everywhere the bot is a member.

## spend limits

every completion is priced and recorded: chat, jev, images, transcription, tts and token-billed tools. hack club ai also enforces an account-wide daily allowance.

three limits work together: per-user rate limit, workspace rate limit and a per-user daily cap. `/nugget budget` shows your spend, the workspace total and the top users for the day.

```dotenv
AI_USER_DAILY_BUDGET_USD=0.50
AI_DAILY_BUDGET_USD=0
AI_RATE_LIMIT_PER_MINUTE=12
AI_GLOBAL_RATE_LIMIT_PER_MINUTE=30
AI_MAX_CONTEXT_CHARS=24000
```

## commands

everything lives under `/nugget`. `/nugget help` is the full list.

| command | what it does | who |
|---|---|---|
| `/nugget` | random nugget fact | anyone |
| `/nugget help` | command list | anyone |
| `/nugget ping` | bot and slack api latency | anyone |
| `/nugget id` | ids and flags for this channel | anyone |
| `/nugget access` | open the tier-2 request modal | anyone |
| `/nugget reminder <when> <text>` | create a reminder | anyone |
| `/nugget timer <duration> [label]` | countdown timer | anyone |
| `/nugget alarm daily <HH:MM> [label]` | daily alarm | anyone |
| `/nugget pinggroup [join|leave|panel]` | manage or post the ping-group panel | anyone / admin |
| `/nugget poll "q" "a" "b"` | live button poll | anyone |
| `/nugget ask <question>` | one-shot ai answer | ai channels |
| `/nugget chats` | list your dm chats, one thread each | anyone (dm) |
| `/nugget chat new [title]` | start a new dm chat thread | anyone (dm) |
| `/nugget secret [title]` | unsaved in-memory chat, web tools only | anyone (dm) |
| `/nugget end [--burn]` | end a secret chat, optionally delete my replies | anyone (dm) |
| `/nugget summarize [n]` | ai catch-up for this channel | ai channels |
| `/nugget imagine <prompt>` | generate an image | admin |
| `/nugget speak <text>` | generate a qwen3 voice clip | admin |
| `/nugget huddle` | summarize the last huddle with slack ai notes | admin |
| `/nugget config` | runtime settings | admin |
| `/nugget memory` | status, search or backfill semantic memory | admin |
| `/nugget storage` | data usage, free disk, optional prune | admin |
| `/nugget doctor` | scopes, tokens, sqlite, browser, github, sandbox | admin |
| `/nugget setup` | connect integrations | admin |

reminder syntax:

```text
10m stretch
in 2 hours meeting
tomorrow 09:00 standup
daily 08:30 good morning
weekly mon 18:00 weekly recap
```

times use `TIMEZONE`. the first time expression is removed from the front and the rest becomes the reminder body.

## people and roles

three levels, set in `.env`:

| role | env | can do |
|---|---|---|
| owner | `OWNER_USER_ID` | everything: mail, calendar, github, mac, memory, posting as themselves, global settings |
| admin | `ADMIN_USER_IDS` | co-operator: AI tools, host dev tools (`run_code`, sandbox, browser), moderation. **not** owner-private data by default, cannot post as the owner, gets a separate sandbox namespace |
| moderator | `MODERATOR_USER_IDS` | tier-2 access approvals and the access panel only. no AI tools, no private data, no host access |

admins are **not** co-owners. if an admin really needs the owner's gmail/calendar/github/mac/notes, set `ADMIN_PRIVATE_ACCESS=true` and accept that they are owner-equivalent. for a completely personal assistant, run a separate instance instead of sharing one.

## environment

copy `.env.example` and read the comments there. the required values are `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `OWNER_USER_ID` and `AI_API_KEY`.

boi needs `SLACK_USER_TOKEN` plus `SLACK_USER_COOKIE` when the token is an `xoxc` session. models, browser, sandbox, gate, budget, recap and integration settings all have defaults and comments in `.env.example`.

## deploy

the normal path is github actions on a push to `main`, or a manual **run workflow**.

the workflow:

1. checks out the repo
2. installs the ssh key from `SSH_PRIVATE_KEY`
3. `rsync`s the tree to the server, excluding `.env`, `.git`, `.venv`, `data/`, `logs/` and db files
4. overwrites the server `.env` with the `ENV_FILE` secret
5. recreates the venv, installs requirements, best-effort installs chromium
6. `systemctl restart chicen-nugget-bot`
7. prints service status and the log tail, then `DEPLOY_OK`

github secrets:

| secret | meaning |
|---|---|
| `SSH_HOST` | server hostname or ip |
| `SSH_USER` | ssh user |
| `SSH_PRIVATE_KEY` | private key with the public half in `authorized_keys` |
| `ENV_FILE` | the whole production `.env` |
| `SSH_PORT` | optional, default `22` |
| `DEPLOY_PATH` | optional, default `slackbot` |
| `SERVICE_NAME` | optional, default `chicen-nugget-bot` |
| `SERVICE_USER` | optional, default `chicen`; the non-root account the bot runs as |

editing `.env` directly on the server is temporary. the next deploy rewrites it from `ENV_FILE`. for persistent env changes, update the secret and redeploy. for tokens and non-secret settings, prefer `/nugget setup` and `/nugget config`, which live in sqlite and survive deploys.

`./deploy-remote.sh user@host [remote-dir]` is the manual path. it rsyncs, installs dependencies and writes a systemd unit at `/etc/systemd/system/<service>.service`. the unit runs as `SERVICE_USER` (default `chicen`), not root, because the bot exposes shell and browser tools; the script creates the account and chowns the repo if needed.

### giving someone their own instance

the bot is single-tenant by design: one owner, one set of credentials, one database, one sandbox root. `admin` is a trusted co-operator on **your** instance, not a private assistant for someone else. if another person should have their own credentials, channels and memory with no access to yours, give them their own deployment:

```
./scripts/new-instance.sh alice U012ABCDEF   # creates instances/alice.env, mode 600
```

then:

1. create a second slack app for them (own bot token, app token, socket mode on) and fill `instances/alice.env`.
2. deploy it under its own service name, unix user and directory:

```
SERVICE_NAME=nugget-alice SERVICE_USER=alice \
  ENV_FILE=instances/alice.env ./deploy-remote.sh root@host alice-bot
```

`alice` is then the **owner of her own instance**: full tools, her own gmail/github/mac/browser credentials, her own `AI_CHANNELS`, admin channel and database. nothing is shared with your instance, and she is not an admin on yours.

isolation details:

- separate repo dir, `data/`, `logs/`, sandboxes and systemd unit per instance.
- separate unix user per instance; files are created with umask `077` and the data dir is `0700`, so one instance's `run_code` cannot read the other's database. both still share the host kernel - for a hard boundary use a separate host or container.
- `instances/` is gitignored and excluded from `rsync`, so env files with tokens never leave the machine they were created on.
- optionally add them as `MODERATOR_USER_IDS` on your instance if they also help approve access requests. that grants no AI tools, no credentials and no host access.

`ENV_FILE` defaults to the local `.env`.

## data and security

- storage: one sqlite file at `data/bot.db`. conversation history, notes, reminders, polls, access requests, runtime settings, semantic memory vectors, dm chat titles and secret-chat markers, and a key/value store. secret-chat message content is never written here.
- retention: history is per conversation and trimmed to `AI_HISTORY_LIMIT` turns. `/nugget forget` wipes the active chat (or `all` DM chats), including semantic vectors. delete `data/bot.db` to reset everything.
- logs: stdout plus rotating files in `logs/`. tool arguments and reply text can show up in debug logs.
- secrets: tokens go through slack modals. never paste a token into chat. if you do, rotate it.
- prompt injection: web pages, files and slack messages are untrusted data, never instructions.
- sandbox: commands run as the bot os user with secret-looking env vars stripped. it's a workspace, not a jail.
- browser: public pages only, private addresses blocked, no logged-in sessions yet.

## troubleshooting

| symptom | fix |
|---|---|
| `invalid_auth` on start | reinstall the app and update `SLACK_BOT_TOKEN` |
| `missing_scope` | update the manifest, reinstall, update the token if it changed |
| posting in your name fails | reinstall the app with `chat:write.customize`; no personal token needed |
| github writes fail | add a token with contents/pull requests/issues write via `/nugget setup github` |
| browser says chromium is missing | run `./scripts/install_browser.sh` |
| code model never runs | check `AI_CODE_MODEL` in env or `/nugget config ai_code_model ...` |
| group dms are ignored | manifest needs `mpim:*` scopes and `message.mpim`; reinstall |
| bot ignores mentions | check `AI_CHANNELS` and whether it's in the channel |
| user account keeps disconnecting | refresh `SLACK_USER_TOKEN` and `SLACK_USER_COOKIE` |
| no typing indicator | boi isn't configured, or the user token is invalid |
| reminders never fire | fix `TIMEZONE` and recreate the reminder |
| approval button does nothing | bot isn't in the admin channel |

## project layout

```text
main.py                    entry point
bot/
  app.py                   startup, bolt wiring, user gateway
  config.py                settings and env parsing
  db.py                    sqlite schema, history, kv store
  handlers.py              socket mode event registration
  slack_api.py             post, react, upload, retries
  user_gateway.py          user websocket, typing, dedup
  ai/
    agent.py               prompt, history, tool loop, model routing
    memory.py              embeddings and semantic memory
    tools.py               tool registry and implementations
    sandbox.py             workspaces and background jobs
    browser.py             chromium sessions and url guards
    gate.py                jev gate
  features/
    agent.py               mention, dm, thread, reaction and group dm handling
    shortcuts.py           message shortcuts
    scheduler.py           reminders, digest, recaps
    watchers.py            github ci watcher
    retention.py           storage cleanup
    doctor.py              /nugget doctor checks
    welcomer.py, access.py, recaps.py, router.py, commands.py
prompts/system.md          agent system prompt
tests/                     pytest suite
.github/workflows/         deploy
```

## development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
ruff check bot tests
python main.py
```

don't run debug logging on a shared host. tool arguments and reply text can end up in logs.

## credits

this bot is a remix, not an invention. two projects shaped it a lot, and both are worth a look:

- **[noodle](https://github.com/MatthiasLubbertsen/noodle)** by **Matthias Lubbertsen** — the biggest inspiration. the jev reply gate (`bot/ai/gate.py`, `bot/ai/jev.py`), semantic/embedding memory (`bot/ai/memory.py`, `bot/db.py`), per-paragraph reply chunking (`bot/utils/text.py`), the `flaron` directory lookup and exa web search/fetch fallback (`bot/ai/tools.py`), the `run_code` tool surface, quiet hours plus the proactive daily cap, the `# ` ignore prefix, and the "own slack account with a typing indicator" idea (`bot/user_gateway.py`) all started there. some of it was adapted, some reimplemented for a python/bolt-async stack.
- **[novabot-slack](https://github.com/aelithron/novabot-slack)** by **aelithron** — the daily recap flow (public recap → private recap, with a heads-up before it) in `bot/features/recaps.py`, the tier-2 private-channel request flow in `bot/features/access.py`, and the usergroup ping helpers (don't thread on a ping; post a safe 🧵 anchor) in `bot/features/threads.py` are adapted from it.

thanks to both for publishing their bots and writing them down. if something here should be credited more specifically, open an issue and it'll get fixed.

## license

mit. do whatever, just don't blame us.
