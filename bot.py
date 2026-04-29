"""
Akaza Log Tool - Telegram Bot Edition
=====================================
A robust Telegram bot to extract cookies from log files by domain.

Setup Instructions:
1. Install dependencies:
   pip install python-telegram-bot[job-queue] telethon aiosqlite
2. Set environment variables:
   BOT_TOKEN: Your Telegram Bot Token
   API_ID: Your Telegram API ID
   API_HASH: Your Telegram API Hash
   ADMIN_ID: Your Telegram User ID
3. Run the bot:
   python bot.py
"""

import logging
import os
import io
import json
import zipfile
import asyncio
import tempfile
import aiosqlite
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import Counter

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    Application,
)
from telethon import TelegramClient

# --- Extractor Logic ---

class SmartCookieExtractor:
    """Efficiently extracts cookies from Netscape cookie format files"""

    def __init__(self, domain: str):
        self.domain = domain.lower().lstrip(".")

    def parse_cookie_line(self, line: str) -> Optional[Dict[str, str]]:
        parts = line.split("\t")
        if len(parts) < 7:
            return None
        try:
            return {
                "domain": parts[0].strip(),
                "flag": parts[1].strip(),
                "path": parts[2].strip(),
                "secure": parts[3].strip(),
                "expiration": parts[4].strip(),
                "name": parts[5].strip(),
                "value": parts[6].strip(),
            }
        except Exception:
            return None

    def _matches_domain(self, cookie_domain: str) -> bool:
        cookie_domain = cookie_domain.lower().lstrip(".")
        return cookie_domain == self.domain or \
               cookie_domain.endswith("." + self.domain) or \
               self.domain.endswith("." + cookie_domain)

    def extract_from_file_path(self, file_path: str) -> List[Dict[str, str]]:
        results = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    cookie = self.parse_cookie_line(line)
                    if cookie and self._matches_domain(cookie["domain"]):
                        results.append(cookie)
        except Exception:
            pass
        return results

    def process_zip(self, zip_path: str) -> Dict[str, List[Dict[str, str]]]:
        results = {}
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for file_info in zf.infolist():
                    if file_info.is_dir():
                        continue
                    name_lower = file_info.filename.lower()
                    if "cookie" in name_lower and name_lower.endswith(".txt"):
                        try:
                            with zf.open(file_info) as f:
                                content = f.read().decode("utf-8", errors="ignore")
                                cookies = []
                                for line in content.splitlines():
                                    line = line.strip()
                                    if not line or line.startswith("#"):
                                        continue
                                    cookie = self.parse_cookie_line(line)
                                    if cookie and self._matches_domain(cookie["domain"]):
                                        cookies.append(cookie)
                                if cookies:
                                    results[file_info.filename] = cookies
                        except Exception:
                            continue
        except Exception:
            pass
        return results

    def process_txt(self, filename: str, file_path: str) -> Dict[str, List[Dict[str, str]]]:
        name_lower = filename.lower()
        if "cookie" in name_lower and name_lower.endswith(".txt"):
            cookies = self.extract_from_file_path(file_path)
            if cookies:
                return {filename: cookies}
        return {}

    @staticmethod
    def format_results(all_cookies: List[Dict[str, Any]], format_type: str) -> str:
        if format_type == "netscape":
            lines = []
            for c in all_cookies:
                lines.append(f"{c['domain']}\t{c['flag']}\t{c['path']}\t{c['secure']}\t{c['expiration']}\t{c['name']}\t{c['value']}")
            return "\n".join(lines)
        elif format_type == "json":
            return json.dumps(all_cookies, indent=2)
        elif format_type == "simple":
            return "\n".join([f"{c['name']}={c['value']}" for c in all_cookies])
        return ""

    @staticmethod
    def get_statistics(results: Dict[str, List[Dict[str, str]]]) -> Dict[str, Any]:
        all_cookies = []
        for file_cookies in results.values():
            all_cookies.extend(file_cookies)
        name_counts = Counter(c['name'] for c in all_cookies)
        return {
            "total_cookies": len(all_cookies),
            "unique_names": len(name_counts),
            "files_processed": len(results),
            "top_10": name_counts.most_common(10)
        }

# --- Database Logic ---

class Database:
    def __init__(self, db_path="bot_data.db"):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    is_vip BOOLEAN DEFAULT 0,
                    is_banned BOOLEAN DEFAULT 0,
                    quota_used INTEGER DEFAULT 0,
                    quota_reset_at TIMESTAMP,
                    last_extraction_stats TEXT
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS vip_requests (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS global_stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER DEFAULT 0
                )
            ''')
            for key in ['total_cookies', 'total_files', 'total_users', 'total_jobs']:
                await db.execute('INSERT OR IGNORE INTO global_stats (key, value) VALUES (?, 0)', (key,))
            await db.commit()

    async def get_user(self, user_id):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)) as cursor:
                return await cursor.fetchone()

    async def add_user(self, user_id, username):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR IGNORE INTO users (user_id, username, quota_reset_at)
                VALUES (?, ?, ?)
            ''', (user_id, username, datetime.now().isoformat()))
            await db.execute('UPDATE global_stats SET value = value + 1 WHERE key = "total_users"')
            await db.commit()

    async def update_user_quota(self, user_id, size_bytes):
        async with aiosqlite.connect(self.db_path) as db:
            user = await self.get_user(user_id)
            if not user: return
            now = datetime.now()
            reset_at = datetime.fromisoformat(user['quota_reset_at']) + timedelta(hours=10)
            if now > reset_at:
                await db.execute('UPDATE users SET quota_used = ?, quota_reset_at = ? WHERE user_id = ?',
                                 (size_bytes, now.isoformat(), user_id))
            else:
                await db.execute('UPDATE users SET quota_used = quota_used + ? WHERE user_id = ?', (size_bytes, user_id))
            await db.commit()

    async def set_vip(self, user_id, status: bool):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE users SET is_vip = ? WHERE user_id = ?', (1 if status else 0, user_id))
            await db.commit()

    async def set_banned(self, user_id, status: bool):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE users SET is_banned = ? WHERE user_id = ?', (1 if status else 0, user_id))
            await db.commit()

    async def add_vip_request(self, user_id, username):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('INSERT OR REPLACE INTO vip_requests (user_id, username, status, created_at) VALUES (?, ?, "pending", ?)',
                             (user_id, username, datetime.now().isoformat()))
            await db.commit()

    async def get_pending_vip_requests(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM vip_requests WHERE status = 'pending'") as cursor:
                return await cursor.fetchall()

    async def update_vip_request(self, user_id, status):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE vip_requests SET status = ? WHERE user_id = ?', (status, user_id))
            await db.commit()

    async def increment_global_stat(self, key, amount=1):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE global_stats SET value = value + ? WHERE key = ?', (amount, key))
            await db.commit()

    async def get_global_stats(self):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT key, value FROM global_stats') as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    async def save_last_stats(self, user_id, stats_json):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE users SET last_extraction_stats = ? WHERE user_id = ?', (stats_json, user_id))
            await db.commit()

    async def get_all_users(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT * FROM users') as cursor:
                return await cursor.fetchall()

# --- Bot Implementation ---

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8482556356:AAFYFKTA_FFFpAaz2tRsgKtalbOrwptPkVY")
API_ID_STR = os.getenv("API_ID", "430309434")
API_ID = int(API_ID_STR) if API_ID_STR.isdigit() else 0
API_HASH = os.getenv("API_HASH", "39d0b0430309434e7ab02ab1742dd170")
ADMIN_ID = int(os.getenv("ADMIN_ID", "5944410248"))

(ASKING_DOMAIN, ASKING_FILES, ASKING_OUTPUT_PREF, ASKING_FORMAT, ASKING_STATS_CONFIRM, BROADCASTING) = range(6)

db_instance = Database()
telethon_client: TelegramClient = None
active_jobs = {}
QUOTA_LIMIT = 2 * 1024 * 1024 * 1024

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db_instance.add_user(user.id, user.username)
    kb = [[InlineKeyboardButton("🍪 Extract Cookies", callback_query_data="menu_extract")],
          [InlineKeyboardButton("📊 Statistics", callback_query_data="menu_stats"), InlineKeyboardButton("❓ Help", callback_query_data="menu_help")],
          [InlineKeyboardButton("💎 Request VIP", callback_query_data="menu_vip")],
          [InlineKeyboardButton("🚪 Exit", callback_query_data="menu_exit")]]
    if user.id == ADMIN_ID: kb.insert(3, [InlineKeyboardButton("🛠 Admin Panel", callback_query_data="admin_panel")])
    msg = "👋 **Welcome to Akaza Log Tool Bot!**\n\nI can help you extract cookies from log files by domain."
    reply_markup = InlineKeyboardMarkup(kb)
    if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=reply_markup, parse_mode="Markdown")
    else: await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = "📖 **Help**\n\n1️⃣ **Domain**: Enter target.\n2️⃣ **Upload**: Send ZIP or TXT.\n3️⃣ **Output**: Separate or combined.\n4️⃣ **Quota**: 2GB/10h."
    kb = [[InlineKeyboardButton("⬅️ Back", callback_query_data="menu_back")]]
    if update.callback_query: await update.callback_query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else: await update.message.reply_text(help_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await db_instance.get_user(update.effective_user.id)
    text = "📊 **Last Stats**\n\n"
    if user_data and user_data['last_extraction_stats']:
        s = json.loads(user_data['last_extraction_stats'])
        text += f"• Cookies: `{s['total_cookies']:,}`\n• Unique: `{s['unique_names']}`\n• Files: `{s['files_processed']}`\n\n**Top 10:**\n"
        for n, c in s['top_10']: text += f"• `{n}`: {c:,}\n"
    else: text += "No data."
    kb = [[InlineKeyboardButton("⬅️ Back", callback_query_data="menu_back")]]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def extract_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await db_instance.get_user(update.effective_user.id)
    if user_data and user_data['is_banned']:
        await update.effective_message.reply_text("❌ Banned.")
        return ConversationHandler.END
    msg = "🌐 **Step 1: Enter Domain** (e.g., `youtube.com`):"
    if update.callback_query: await update.callback_query.message.reply_text(msg, parse_mode="Markdown"); await update.callback_query.answer()
    else: await update.message.reply_text(msg, parse_mode="Markdown")
    return ASKING_DOMAIN

async def process_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    domain = update.message.text.strip().lower()
    if "." not in domain: await update.message.reply_text("❌ Invalid."); return ASKING_DOMAIN
    context.user_data['domain'] = domain
    await update.message.reply_text(f"✅ Domain: `{domain}`\n\n📂 **Step 2: Upload Files** (ZIP or TXT)", parse_mode="Markdown")
    context.user_data['files'] = []
    return ASKING_FILES

async def handle_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f_info = update.message.document
    if not f_info: return ASKING_FILES
    uid = update.effective_user.id
    user = await db_instance.get_user(uid)
    if not user['is_vip'] and uid != ADMIN_ID:
        if user['quota_used'] + f_info.file_size > QUOTA_LIMIT:
            reset = datetime.fromisoformat(user['quota_reset_at']) + timedelta(hours=10)
            if datetime.now() < reset: await update.message.reply_text(f"❌ Quota full. Reset: {reset}"); return ASKING_FILES
    context.user_data['files'].append({'file_id': f_info.file_id, 'file_name': f_info.file_name, 'file_size': f_info.file_size, 'message_id': update.message.message_id})
    kb = [[InlineKeyboardButton("✅ Done", callback_query_data="files_done")]]
    await update.message.reply_text(f"📥 Added: `{f_info.file_name}`\nTotal: {len(context.user_data['files'])}", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ASKING_FILES

async def files_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('files'): await update.callback_query.message.reply_text("⚠️ Upload at least one."); return ASKING_FILES
    kb = [[InlineKeyboardButton("ZIP (Separate)", callback_query_data="pref_separate")], [InlineKeyboardButton("Combined", callback_query_data="pref_combined")], [InlineKeyboardButton("Both", callback_query_data="pref_both")]]
    await update.callback_query.edit_message_text("⚙️ **Step 3: Preference**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ASKING_OUTPUT_PREF

async def process_pref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pref = update.callback_query.data.replace("pref_", "")
    context.user_data['pref'] = pref
    if pref in ["combined", "both"]:
        kb = [[InlineKeyboardButton("Netscape", callback_query_data="fmt_netscape")], [InlineKeyboardButton("JSON", callback_query_data="fmt_json")], [InlineKeyboardButton("Simple", callback_query_data="fmt_simple")]]
        await update.callback_query.edit_message_text("📄 **Step 4: Format**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return ASKING_FORMAT
    return await ask_stats_confirm(update, context)

async def process_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['format'] = update.callback_query.data.replace("fmt_", "")
    return await ask_stats_confirm(update, context)

async def ask_stats_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("Yes", callback_query_data="stats_yes"), InlineKeyboardButton("No", callback_query_data="stats_no")]]
    msg = "📊 **Step 5: Show Stats?**"
    if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else: await update.effective_message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ASKING_STATS_CONFIRM

async def process_stats_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['show_stats'] = update.callback_query.data == "stats_yes"
    asyncio.create_task(run_extraction(update, context))
    await update.callback_query.edit_message_text("🚀 **Processing!**...")
    return ConversationHandler.END

async def run_extraction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    job_id = f"{uid}_{int(datetime.now().timestamp())}"
    domain = context.user_data['domain']
    active_jobs[job_id] = {"status": "Starting", "user_id": uid, "domain": domain}
    files = context.user_data['files']
    pref = context.user_data['pref']
    fmt = context.user_data.get('format', 'netscape')
    extractor = SmartCookieExtractor(domain)
    all_res = {}
    status_msg = await update.effective_message.reply_text(f"⏳ Processing 0/{len(files)}...")
    try:
        for i, fi in enumerate(files, 1):
            if job_id not in active_jobs: return
            active_jobs[job_id]['status'] = f"Downloading {fi['file_name']}"
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name
                if fi['file_size'] > 20 * 1024 * 1024:
                    m = await telethon_client.get_messages(uid, ids=fi['message_id'])
                    await telethon_client.download_media(m, tmp_path)
                else:
                    nf = await context.bot.get_file(fi['file_id'])
                    await nf.download_to_drive(tmp_path)

            active_jobs[job_id]['status'] = f"Extracting {fi['file_name']}"
            if fi['file_name'].lower().endswith('.zip'): all_res.update(extractor.process_zip(tmp_path))
            else: all_res.update(extractor.process_txt(fi['file_name'], tmp_path))
            try: os.remove(tmp_path)
            except: pass
            await status_msg.edit_text(f"⏳ Processed {i}/{len(files)}...")
            await db_instance.update_user_quota(uid, fi['file_size'])

        if not all_res: await status_msg.edit_text(f"❌ No cookies found for `{domain}`."); return
        combined = []
        for fc in all_res.values(): combined.extend(fc)
        if pref in ["combined", "both"]:
            content = extractor.format_results(combined, fmt)
            bio = io.BytesIO(content.encode()); bio.name = f"{domain}_combined.{('txt' if fmt != 'json' else 'json')}"
            await update.effective_message.reply_document(bio, caption=f"✅ Combined {domain}")
        if pref in ["separate", "both"]:
            z_bio = io.BytesIO()
            with zipfile.ZipFile(z_bio, 'w') as zf:
                for idx, (on, cks) in enumerate(all_res.items(), 1):
                    zf.writestr(f"akaza_{domain}_{idx}.txt", extractor.format_results(cks, "netscape"))
            z_bio.seek(0); z_bio.name = f"{domain}_separate.zip"
            await update.effective_message.reply_document(z_bio, caption=f"✅ Separate {domain}")
        stats = extractor.get_statistics(all_res)
        await db_instance.save_last_stats(uid, json.dumps(stats))
        await db_instance.increment_global_stat('total_cookies', stats['total_cookies'])
        await db_instance.increment_global_stat('total_files', stats['files_processed'])
        await db_instance.increment_global_stat('total_jobs')
        if context.user_data['show_stats']:
            st_text = f"📊 **Stats**\n• Cookies: `{stats['total_cookies']:,}`\n• Unique: `{stats['unique_names']}`\n"
            await update.effective_message.reply_text(st_text, parse_mode="Markdown")
        await status_msg.edit_text("✅ **Complete!**")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Error: {str(e)}")
    finally: active_jobs.pop(job_id, None)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    for jid in list(active_jobs.keys()):
        if active_jobs[jid]['user_id'] == uid: active_jobs.pop(jid)
    await update.message.reply_text("👋 Stopped.")
    return await start(update, context)

async def exit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    for jid in list(active_jobs.keys()):
        if active_jobs[jid]['user_id'] == uid: active_jobs.pop(jid)
    await update.callback_query.answer(); await update.callback_query.edit_message_text("👋 Goodbye!"); return ConversationHandler.END

async def request_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; await db_instance.add_vip_request(u.id, u.username)
    await update.callback_query.answer("Sent!"); await update.callback_query.edit_message_text("💎 VIP request pending.")
    kb = [[InlineKeyboardButton("✅ Approve", callback_query_data=f"adm_app_{u.id}"), InlineKeyboardButton("❌ Reject", callback_query_data=f"adm_rej_{u.id}")]]
    await context.bot.send_message(ADMIN_ID, f"💎 **New VIP Request**\nUser: @{u.username} (`{u.id}`)", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    s = await db_instance.get_global_stats()
    msg = f"🛠 **Admin Panel**\n• Users: `{s['total_users']}`\n• Jobs: `{s['total_jobs']}`\n• Active: `{len(active_jobs)}`"
    kb = [[InlineKeyboardButton("💎 VIP Requests", callback_query_data="adm_requests")], [InlineKeyboardButton("📢 Broadcast", callback_query_data="adm_bc_start")], [InlineKeyboardButton("🚫 Users", callback_query_data="adm_u_list")], [InlineKeyboardButton("💼 Jobs", callback_query_data="adm_jobs")], [InlineKeyboardButton("⬅️ Back", callback_query_data="menu_back")]]
    if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else: await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; d = q.data
    if d.startswith("adm_app_"):
        uid = int(d.split("_")[2]); await db_instance.set_vip(uid, True); await db_instance.update_vip_request(uid, 'approved')
        try: await context.bot.send_message(uid, "🎉 VIP Approved!")
        except: pass
        await q.edit_message_text(f"✅ User {uid} approved.")
    elif d.startswith("adm_rej_"):
        uid = int(d.split("_")[2]); await db_instance.update_vip_request(uid, 'rejected')
        try: await context.bot.send_message(uid, "❌ VIP Rejected.")
        except: pass
        await q.edit_message_text(f"❌ User {uid} rejected.")
    elif d == "adm_requests":
        reqs = await db_instance.get_pending_vip_requests()
        if not reqs: await q.answer("None."); return
        text = "💎 **Pending**\n"; kb = []
        for r in reqs:
            text += f"• {r['username']}\n"
            kb.append([InlineKeyboardButton(f"✅ {r['username']}", callback_query_data=f"adm_app_{r['user_id']}"), InlineKeyboardButton(f"❌ {r['username']}", callback_query_data=f"adm_rej_{r['user_id']}")])
        kb.append([InlineKeyboardButton("✅ All", callback_query_data="adm_app_all"), InlineKeyboardButton("❌ All", callback_query_data="adm_rej_all")])
        kb.append([InlineKeyboardButton("⬅️ Back", callback_query_data="admin_panel")])
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif d == "adm_app_all":
        for r in await db_instance.get_pending_vip_requests():
            await db_instance.set_vip(r['user_id'], True); await db_instance.update_vip_request(r['user_id'], 'approved')
            try: await context.bot.send_message(r['user_id'], "🎉 VIP Approved!")
            except: pass
        await admin_panel(update, context)
    elif d == "adm_rej_all":
        for r in await db_instance.get_pending_vip_requests():
            await db_instance.update_vip_request(r['user_id'], 'rejected')
            try: await context.bot.send_message(r['user_id'], "❌ VIP Rejected.")
            except: pass
        await admin_panel(update, context)
    elif d == "adm_jobs":
        if not active_jobs: await q.answer("None."); return
        text = "💼 **Jobs**\n"
        for jid, j in active_jobs.items(): text += f"• `{j['user_id']}`: `{j['domain']}` - `{j['status']}`\n"
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_query_data="admin_panel")]]), parse_mode="Markdown")
    elif d == "adm_bc_start": await q.edit_message_text("📢 Send broadcast:"); return BROADCASTING
    elif d == "adm_u_list":
        users = await db_instance.get_all_users(); text = "🚫 **Users**\n"; kb = []
        for u in users[:15]:
            text += f"• {u['username']} ({'💎' if u['is_vip'] else '🆓'}{'🛑' if u['is_banned'] else ''})\n"
            kb.append([InlineKeyboardButton(f"Ban {u['username']}", callback_query_data=f"adm_ban_{u['user_id']}"), InlineKeyboardButton(f"VIP {u['username']}", callback_query_data=f"adm_vt_{u['user_id']}")])
        kb.append([InlineKeyboardButton("⬅️ Back", callback_query_data="admin_panel")])
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif d.startswith("adm_ban_"):
        uid = int(d.split("_")[2]); u = await db_instance.get_user(uid)
        await db_instance.set_banned(uid, not u['is_banned']); await admin_panel(update, context)
    elif d.startswith("adm_vt_"):
        uid = int(d.split("_")[2]); u = await db_instance.get_user(uid)
        await db_instance.set_vip(uid, not u['is_vip']); await admin_panel(update, context)

async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = await db_instance.get_all_users(); count = 0
    for u in users:
        try: await context.bot.send_message(u['user_id'], update.message.text); count += 1
        except: pass
    await update.message.reply_text(f"✅ Sent {count}."); return ConversationHandler.END

async def post_init(application: Application):
    await db_instance.init()
    global telethon_client
    telethon_client = TelegramClient('bot_session', API_ID, API_HASH)
    await telethon_client.start(bot_token=BOT_TOKEN)

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler('extract', extract_start), CallbackQueryHandler(extract_start, pattern="^menu_extract$"), CallbackQueryHandler(admin_callback, pattern="^adm_bc_start$")],
        states={
            ASKING_DOMAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_domain)],
            ASKING_FILES: [MessageHandler(filters.Document.ALL, handle_files), CallbackQueryHandler(files_done, pattern="^files_done$")],
            ASKING_OUTPUT_PREF: [CallbackQueryHandler(process_pref, pattern="^pref_")],
            ASKING_FORMAT: [CallbackQueryHandler(process_format, pattern="^fmt_")],
            ASKING_STATS_CONFIRM: [CallbackQueryHandler(process_stats_confirm, pattern="^stats_")],
            BROADCASTING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(exit_callback, pattern="^menu_exit$"), CallbackQueryHandler(start, pattern="^menu_back$")],
    )
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("help", help_command)); app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(conv); app.add_handler(CallbackQueryHandler(start, pattern="^menu_back$")); app.add_handler(CallbackQueryHandler(help_command, pattern="^menu_help$"))
    app.add_handler(CallbackQueryHandler(show_stats, pattern="^menu_stats$")); app.add_handler(CallbackQueryHandler(request_vip, pattern="^menu_vip$"))
    app.add_handler(CallbackQueryHandler(exit_callback, pattern="^menu_exit$")); app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))

    app.run_polling()
