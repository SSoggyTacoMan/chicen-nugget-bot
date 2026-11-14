# chicen nugget

a slack bot for a hack club workspace. one process, two slack identities.

- the bot app handles channels, mentions, thread follow-ups, slash commands, welcomer, access requests, recaps, reminders and polls.
- boi is an optional personal assistant account. give it a slack user token and it answers your dms, group dms and mentions with a typing indicator.

they share one sqlite database, one toolbox and one memory. socket mode only, so there's no public url, no web server and no separate database.

## what it does

- mentions, name calls like "hey nugget", dms, group dms, thread follow-ups and optional ambient replies
- search across slack messages and files, plus channel join, leave, create, topic, invite and pin tools
- reaction shortcuts and message shortcuts for research, explain and thread summaries
- persistent code sandboxes with file edits, background jobs, uploads and branch push
- github reads and writes: prs, issues, comments, reviews, close/reopen, merge and push
- headless chromium for pages that need javascript, clicking or forms
- optional api tokens for vercel, cloudflare, notion, linear, sentry, resend, openai and anthropic
- separate bot, boi and code models, with fallbacks and runtime switching
- posting in the owner's name, either as a customized bot message or from their real account
- jev, a small classifier that decides whether an unaddressed message deserves a reply
- images, charts, powerpoint decks, welcomer, access requests, recaps, reminders and polls

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

- typing: a typing frame goes out every 2.5 seconds while the model runs.
- messages: bots, its own messages, the bot account, duplicate timestamps and unrelated thread chatter get ignored.
- group dms: boi answers when someone `@mentions` it. private dm history and group history stay separate.
- threads: in channels it follows a thread only if the agent already knows it. dm threads stay in the thread they started in.
- reconnects: slack sends a `reconnect_url` frame, and the gateway falls back to `client.getWebSocketURL` with exponential backoff.
- presence: it asks slack for `auto` presence. slack still marks human accounts away after about ten minutes.
- isolation: workspace admin actions stay on the bot app. a stolen user session can't approve access or change settings.

treat `xoxc` and `xoxd` like passwords. they grant full access to the account, including private channels. to rotate them, sign out and back in, then update the env.

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
| your slack profile | `/nugget setup slack` |
| status | `/nugget setup status` |
| remove credentials | `/nugget setup disconnect <name>` |

settings land in sqlite (`data/bot.db`) and override env values. env stays as a fallback for fresh deploys and ci.

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

run `scripts/mac_companion.py` on the mac, expose it with a cloudflare tunnel or ngrok, then run `/nugget setup mac`. it can check battery and volume, open apps, files and urls, send notifications, read the clipboard, lock, sleep, speak and run applescript or shell commands.

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
| boi chat | `anthropic/claude-sonnet-5` | `anthropic/claude-haiku-5.5` |
| code turns | `AI_CODE_MODEL` (empty) | `AI_CODE_FALLBACK_MODEL` |

`AI_CODE_MODEL` is optional. when set, a `sandbox`, `github_write`, `browser`, `api_request`, `run_code`, `inspect_code` or `clone_repo` call switches the rest of that turn to the code model. normal chat stays on the identity model.

change them at runtime:

```text
/nugget config ai_model ...
/nugget config ai_assistant_model ...
/nugget config ai_code_model ...
```

## tools

| group | tools |
|---|---|
| web | `search_web`, `fetch_url`, `http_status`, `hacker_news`, `ocr_document`, `browser` |
| code | `inspect_code`, `clone_repo`, `sandbox`, `run_code`, `github_info`, `github_write`, `hackatime_stats`, `system_diagnostics` |
| slack | `search_slack`, `fetch_slack_message`, `summarize_thread`, `get_channel_history`, `lookup_user`, `lookup_channel`, `manage_channel`, `send_dm`, `post_message`, `slack_status`, `slack_dnd`, `slack_react`, `slack_activity` |
| personal | `google_calendar`, `gmail`, `google_drive`, `mac_control`, `check_emails`, `daily_briefing`, `api_request` |
| setup | `integration_setup` |
| workspace | `flaron`, `pending_access_requests`, `get_stats`, `bot_config` |
| memory | `remember`, `recall`, `forget`, `manage_tasks`, `remind`, `list_reminders`, `cancel_reminder` |
| media | `generate_image`, `make_chart`, `make_deck` |
| misc | `get_weather`, `public_holidays`, `roll_dice`, `dog_fact`, `taco_recipe`, `urban_dict`, `nugget_fact` |

owner and admin checks happen in code against the real slack sender. the prompt can't grant permissions on its own.

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
reply = not spam
        and (aimed_at_bot or (worth_replying and recipient != someone_else))
```

a message aimed at another person never triggers an agent turn unless the bot is also addressed. `needs_tools` becomes a hint so the model fetches data instead of guessing.

mentions and dms skip the gate. errors and timeouts fail closed. `AI_AMBIENT_CHANNELS=*` turns ambient replies on everywhere the bot is a member.

## spend limits

every completion is priced and recorded: chat, jev, images and token-billed tools. hack club ai also enforces an account-wide daily allowance.

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
| `/nugget poll "q" "a" "b"` | live button poll | anyone |
| `/nugget ask <question>` | one-shot ai answer | ai channels |
| `/nugget summarize [n]` | ai catch-up for this channel | ai channels |
| `/nugget imagine <prompt>` | generate an image | admin |
| `/nugget config` | runtime settings | admin |
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

editing `.env` directly on the server is temporary. the next deploy rewrites it from `ENV_FILE`. for persistent env changes, update the secret and redeploy. for tokens and non-secret settings, prefer `/nugget setup` and `/nugget config`, which live in sqlite and survive deploys.

`./deploy-remote.sh user@host [remote-dir]` is the manual path. it rsyncs, installs dependencies and writes a systemd unit at `/etc/systemd/system/<service>.service`, assuming the repo is at `/root/slackbot`.

## data and security

- storage: one sqlite file at `data/bot.db`. conversation history, notes, reminders, polls, access requests, runtime settings and a key/value store.
- retention: history is per conversation and trimmed to `AI_HISTORY_LIMIT` turns. `/nugget forget` wipes stored ai memory. delete `data/bot.db` to reset everything.
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
| code model never runs | set `AI_CODE_MODEL` in env or with `/nugget config ai_code_model ...` |
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
    tools.py               tool registry and implementations
    sandbox.py             workspaces and background jobs
    browser.py             chromium sessions and url guards
    gate.py                jev gate
  features/
    agent.py               mention, dm, thread, reaction and group dm handling
    shortcuts.py           message shortcuts
    scheduler.py           reminders, digest, recaps
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

## license

mit. do whatever, just don't blame us.
