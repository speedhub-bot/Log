# Cookie Extractor Bot

Production-grade Telegram bot for extracting cookies from Netscape-format archive files with full admin panel, VIP system, queue management, and GB-scale file processing.

## Features

- **Cookie Extraction** — Extracts domain-specific cookies from `.zip`, `.rar`, `.7z`, `.tar.gz` archives
- **Large File Support** — Files >20 MB downloaded via Telethon user client (up to 10 GB for VIP)
- **Queue System** — Async priority queue with VIP skip-ahead and configurable concurrency
- **Quota System** — Per-user daily byte limits with midnight UTC reset
- **VIP Membership** — Unlimited quota, priority queue, larger file limits
- **Admin Panel** — Full control: user management, stats, broadcasts, settings, logs
- **Anti-Abuse** — Rate limiting, spam detection, domain blacklisting, auto-ban
- **Live Progress** — Real-time progress messages updated every 3 seconds
- **Scheduled Tasks** — APScheduler for VIP expiry, quota reset, temp cleanup, daily reports

## Project Structure

```
bot.py                     # Main entry point
config.py                  # All settings from environment variables
requirements.txt           # Dependencies
.env.example               # Environment variable template
handlers/
  user.py                  # /start, /mystats, /help, VIP request, settings
  extract.py               # Extraction conversation handler
  admin.py                 # Full admin panel with all commands
services/
  extractor.py             # SmartCookieExtractor + async wrapper
  downloader.py            # Telethon large file downloader
  queue.py                 # Async job queue with VIP priority
db/
  database.py              # All async database operations
  models.py                # SQL schema definitions
utils/
  formatting.py            # Human-readable bytes, time, progress bars
  validators.py            # Domain and file type validation
```

## Setup

### 1. Clone and install

```bash
git clone https://github.com/speedhub-bot/Log.git
cd Log
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your values
```

Required variables:
- `BOT_TOKEN` — From [@BotFather](https://t.me/BotFather)
- `API_ID` / `API_HASH` — From [my.telegram.org](https://my.telegram.org)
- `ADMIN_ID` — Your Telegram numeric user ID
- `SESSION_STRING` — Telethon session string (see below)

### 3. Generate SESSION_STRING

The session string is needed for downloading files larger than 20 MB.

```bash
python3 -c "
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
API_ID = int(input('API_ID: '))
API_HASH = input('API_HASH: ')
with TelegramClient(StringSession(), API_ID, API_HASH) as c:
    print('Session string:', c.session.save())
"
```

Copy the output string into your `.env` file as `SESSION_STRING`.

### 4. Run

```bash
python bot.py
```

## Commands

### User Commands
| Command | Description |
|---------|-------------|
| `/start` | Show main menu |
| `/extract` | Start cookie extraction |
| `/mystats` | View your statistics |
| `/help` | Usage guide (paginated) |

### Admin Commands
| Command | Description |
|---------|-------------|
| `/admin` | Admin panel (inline keyboard) |
| `/debug` | System diagnostics |
| `/ban <user_id> <reason>` | Ban a user |
| `/unban <user_id>` | Unban a user |
| `/vip <user_id> <days>` | Grant VIP (0 = forever) |
| `/revokevip <user_id>` | Remove VIP |
| `/setlimit <user_id> <gb>` | Custom daily limit |
| `/msg <user_id> <message>` | Message a user |
| `/addquota <user_id> <gb>` | Add extra quota |

## Deployment

### Railway

1. Push to GitHub
2. Connect repo in [Railway](https://railway.app)
3. Set environment variables in Railway dashboard
4. Deploy — the `bot.py` entry point runs automatically

### Linux Server

```bash
# Install system dependencies for archive extraction
sudo apt install unrar p7zip-full

# Run with systemd or screen/tmux
python bot.py
```

### System Requirements

- Python 3.10+
- `unrar` and `7za` (for `.rar` / `.7z` support)
- Sufficient disk space for temp extraction (depends on archive sizes)
