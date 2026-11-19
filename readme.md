# Requires Python 3.12+
# macOS: brew install python@3.12
# then: python3.12 -m venv .venv && source .venv/bin/activate
# Prefer: .venv/bin/python (avoid shell `python` aliases to 3.9)

# Postgres (required)
# DATABASE_URL=postgresql://...  in .env
# On startup the app creates schema `claw` and stores:
#   app_config, users, sessions, schedules, memory, reminders, news_sent, token_usage
# Env keys from .env are synced into claw.app_config (new keys only; admin edits win)
#
# Roles: new users get role=user. Promote an admin with:
#   .venv/bin/python scripts/set_admin.py you@example.com
#   # or: psql "$DATABASE_URL" -f scripts/set_admin.sql
# Admin UI: Models + App config (MAX_QUESTIONS_PER_DAY default 10, MAX_INPUT_TOKENS default 2000)

# Console (local)
.venv/bin/python -m uvicorn api:app --reload --host 0.0.0.0 --port 8500
# or: .venv/bin/python main.py
Then open http://127.0.0.1:8500/

# Vercel
# Entry: main:app (see pyproject.toml [tool.vercel] + vercel.json)
# Deploy: vercel --prod
# Set DATABASE_URL + other env vars in the Vercel project

# Google Sign-In (web console)
# 1. Create an OAuth client (Web) in Google Cloud Console
# 2. Add authorized JavaScript origins: http://127.0.0.1:8500 and your Vercel URL
# 3. Set:
$env:GOOGLE_CLIENT_ID = "1234567890-xxxx.apps.googleusercontent.com"

# Bedrock (default)
$env:MODEL_PROVIDER = "bedrock"
$env:BEDROCK_MODEL_ID = "global.anthropic.claude-haiku-5-5"

# Groq
$env:MODEL_PROVIDER = "groq"
$env:GROQ_API_KEY = "..."
$env:GROQ_MODEL_ID = "llama-3.3-70b-versatile"
# optional: $env:MODEL_ID = "..."  # overrides provider default

# OpenRouter
$env:MODEL_PROVIDER = "openrouter"
$env:OPENROUTER_API_KEY = "..."   # OPEN_ROUTER_KEY is also accepted
$env:OPENROUTER_MODEL_ID = "openai/gpt-4o-mini"

TELEGRAM_BOT_TOKEN=9202266608:AAEs3iUltTzQ0aNRRjoWzQLFG5_7OiFO4Mw
GMAIL_EMAIL=soft.arch.kishore@gmail.com
GMAIL_APP_PASSWORD=dqiujboflebjsilj
GMAIL_IMAP_SERVER=imap.gmail.com
TELEGRAM_CHAT_ID=9264133237
GMAIL_IMAP_SERVER=imap.gmail.comuv
GMAIL_SMTP_SERVER=smtp.gmail.com
GMAIL_SMTP_PORT=587


- check new at every 1 hour and find top 5 news and send email ( agent/new_agent )
- check reminders and schedules for the given day and send them on telegram ( reminder.py  )
- store the facts of the family member names and birthdays and anniversaris in memories ( agent )
- 
