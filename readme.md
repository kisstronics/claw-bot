# Console (local)
python -m uvicorn api:app --reload --host 0.0.0.0 --port 8500
# or: python main.py
Then open http://127.0.0.1:8500/

# Vercel
# Entry: main:app (see pyproject.toml [tool.vercel] + vercel.json)
# Deploy: vercel --prod
# Set env vars in the Vercel project (MODEL_PROVIDER, API keys, GOOGLE_CLIENT_ID, TELEGRAM_*, GMAIL_*, etc.)

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
