# Team Reminder Bot

Telegram reminder bot that nags until the task is marked done, now multi-user: each approved person has their own private task list, reminders, snoozes, daily summary and dashboard link. The admin approves new members with a button and sees everyone's tasks on their dashboard.

## Team version notes

- The first person to message the bot becomes the admin. New people who message it wait until the admin taps Approve.
- Everyone sends "dashboard" to the bot to get their personal dashboard link. The link IS the login, tell people to keep it private.
- Set a PUBLIC_URL variable to your Railway domain (e.g. https://reminder-bot-production-db0f.up.railway.app) so those links come out clickable.
- Per-person channels: each user can send the bot "myemail them@x.com" (emails go out via the shared Gmail) and "mywhatsapp +44... APIKEY" (their own free CallMeBot key). Telegram always works with no setup.
- Admin extras: "users" lists members; the admin dashboard shows all tasks labelled by person and can tick off or edit any of them.
- Existing single-user tasks are kept and assigned to the admin automatically on first boot.

## How it reminds you

Task with a date/time: 24h before, 1h before, then every 15 min from the due time until you tap Done or reply "done".
Task with no time ("buy milk"): every 2 hours until done.
Change the pace with NAG_MINUTES and NO_DUE_NAG_MINUTES.

## Setup

### 1. Create the Telegram bot (2 min)

1. In Telegram, message @BotFather
2. Send /newbot, pick a name and a username
3. Copy the token it gives you (looks like 123456:ABC-xyz)

### 2. Create the repo and Railway service

1. New GitHub repo with these 3 files: main.py, requirements.txt, README.md
2. In Railway: New Project > Deploy from GitHub repo
3. In the service Settings, set Start Command: python main.py
4. IMPORTANT so tasks survive redeploys: in the service, add a Volume and set Mount Path to /data

### 3. Environment variables (Railway > service > Variables)

Required:

- TELEGRAM_BOT_TOKEN = token from BotFather
- ANTHROPIC_API_KEY = your existing Anthropic key (used to read screenshots and parse dates)

Email (Gmail):

- GMAIL_USER = yourgmail@gmail.com
- GMAIL_APP_PASSWORD = an App Password, NOT your normal password. Get one at myaccount.google.com > Security > 2-Step Verification > App passwords
- EMAIL_TO = where reminders go (defaults to GMAIL_USER)

WhatsApp (CallMeBot, free):

1. Save +34 621 331 709 in your phone contacts (check callmebot.com in case the number changed)
2. WhatsApp that contact the message: I allow callmebot to send me messages
3. It replies within 2 min with your API key
4. Set CALLMEBOT_PHONE = your WhatsApp number with country code, e.g. +447700900123
5. Set CALLMEBOT_APIKEY = the key it sent you

Dashboard:

- DASHBOARD_PASSWORD = any password you like. Without it the dashboard stays disabled.
- In Railway service Settings > Networking, click Generate Domain if the service doesn't have one
- Open that domain in your browser, enter the password (it stays logged in for 6 months per device)
- The dashboard shows active/overdue/done-today counts, every task with its next nag time, and lets you add tasks, mark done and delete. Marking done there stops the nagging same as replying done in Telegram.

Optional:

- TIMEZONE = Europe/London (default)
- NAG_MINUTES = 15 (nag interval after due time)
- NO_DUE_NAG_MINUTES = 120 (nag interval for tasks with no time)
- DAILY_SUMMARY_HOUR = 8 (morning summary of today's tasks; set -1 to turn off)
- QUIET_START and QUIET_END = e.g. 23 and 7 to hold all nags overnight (off by default; reminders due during quiet hours fire when it ends)
- ANTHROPIC_MODEL = claude-sonnet-4-5 (default)
- OWNER_CHAT_ID = your Telegram chat id (optional lock; otherwise the first person to message the bot becomes the owner, so message it yourself first)

### 4. First run

Message your bot "help". Then try: "dentist friday 2pm". Then reply "done".

## Usage

- Any plain text = new task ("call the bank tomorrow morning")
- Photo/screenshot of an appointment = new task (add a caption if the picture is unclear)
- list = show active tasks
- done = mark done (or "done 3" by number, or tap the Done button)
- delete 3 = remove a task without doing it
- Snooze buttons (1h / 3h / Tomorrow) on every reminder and on each dashboard card push the next nag back
- Dashboard has a List tab and a month Calendar tab showing tasks on their days with times
- Every morning at 8 the bot sends a summary of today's tasks and anything overdue

Notes:

- Email and WhatsApp are one-way mirrors. "Done" only works in Telegram.
- If CallMeBot or Gmail vars are missing, those channels are just skipped, the bot still works.
- CallMeBot is a free unofficial service, fine for personal use but it can occasionally be slow or down.
