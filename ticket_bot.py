import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timezone
import os
import json
import io
import aiohttp
from collections import defaultdict
from difflib import SequenceMatcher
import re
import time

# ===== 設定 =====
TOKEN = os.environ.get("TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://fdajbuwhxxmwunpxpkwf.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ADMIN_ROLE_ID = 1486603522748317737
SUB_ROLE_ID = 1479446163353632911  # 追加スタッフロール

SPAM_LIMIT = 5
SPAM_INTERVAL = 5
RAID_ACCOUNT_AGE_DAYS = 7
RAID_JOIN_LIMIT = 5
RAID_JOIN_INTERVAL = 10
MAX_MESSAGE_LENGTH = 140
MAX_NEWLINES = 10
TIMEOUT_MINUTES = 5
SPAM_COUNT = 3
CONTENT_SPAM_USERS = 4
CONTENT_SPAM_SECONDS = 5
CONTENT_SPAM_RATIO = 0.80
CONTENT_SPAM_PREFIX = 8
# ================

# ===== Supabase HTTPクライアント =====
def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

async def sb_get(table: str, params: str = ""):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{SUPABASE_URL}/rest/v1/{table}?{params}", headers=sb_headers()) as r:
            return await r.json()

async def sb_upsert(table: str, data: dict):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{SUPABASE_URL}/rest/v1/{table}", headers={**sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}, json=data) as r:
            return await r.json()

async def sb_delete(table: str, params: str):
    async with aiohttp.ClientSession() as s:
        async with s.delete(f"{SUPABASE_URL}/rest/v1/{table}?{params}", headers=sb_headers()) as r:
            return r.status

# ===== スタッフ権限チェック =====
def is_staff(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if member.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in member.roles}
    return ADMIN_ROLE_ID in role_ids or SUB_ROLE_ID in role_ids

def staff_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_staff(interaction):
            await interaction.response.send_message("❌ このコマンドはスタッフのみ使用できます。", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def admin_check():
    """管理者ロール（ADMIN_ROLE_ID）または administrator権限のみ許可"""
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if member.guild_permissions.administrator:
            return True
        if any(r.id == ADMIN_ROLE_ID for r in member.roles):
            return True
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用できます。", ephemeral=True)
        return False
    return app_commands.check(predicate)

# ===========================
# ===== ページネーション =====
# ===========================

class PageView(discord.ui.View):
    """汎用ページネーションView"""
    def __init__(self, pages: list, title: str, color: discord.Color, ephemeral: bool = True):
        super().__init__(timeout=120)
        self.pages = pages      # [ "line1\nline2\n..." ] のリスト
        self.title = title
        self.color = color
        self.ephemeral = ephemeral
        self.current = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.current == 0
        self.next_button.disabled = self.current >= len(self.pages) - 1

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.title,
            description=self.pages[self.current],
            color=self.color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"ページ {self.current + 1} / {len(self.pages)}")
        return embed

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


def paginate(lines: list, per_page: int = 20) -> list:
    """行リストをper_page行ずつのページに分割"""
    pages = []
    for i in range(0, len(lines), per_page):
        pages.append("\n".join(lines[i:i+per_page]))
    return pages

# ===== 警告データ（Supabase） =====
async def get_warns(user_id: int) -> int:
    rows = await sb_get("warns", f"user_id=eq.{user_id}")
    if rows and len(rows) > 0:
        return rows[0].get("count", 0)
    return 0

async def set_warns(user_id: int, count: int):
    await sb_upsert("warns", {"user_id": user_id, "count": count})

async def reset_warns(user_id: int):
    await sb_upsert("warns", {"user_id": user_id, "count": 0})

# ===== 禁止ワード（Supabase） =====
DEFAULT_BAD_WORDS = ["@everyone", "@here", "ガイジ", "がいじ", "カス", "きえろ", "ちんこ", "まんこ", "死ね", "消えろ", "障害", "障害者", "貧乏", "死んどけ", "人殺し", "施設育ち", "がいきち", "ガイキチ", "きちがい", "キチガイ", "ゲイ", "そちん", "ちんぽ", "ちんちん", "テンガ", "TENGA"]

async def load_bad_words_db() -> list:
    rows = await sb_get("bad_words", "select=word")
    if rows and len(rows) > 0:
        return [r["word"] for r in rows]
    for word in DEFAULT_BAD_WORDS:
        await sb_upsert("bad_words", {"word": word})
    return DEFAULT_BAD_WORDS

async def add_bad_word_db(word: str):
    await sb_upsert("bad_words", {"word": word})

async def remove_bad_word_db(word: str):
    await sb_delete("bad_words", f"word=eq.{word}")

# ===== サーバー設定（Supabase） =====
async def get_config(key: str):
    rows = await sb_get("config", f"key=eq.{key}&select=value")
    if rows and len(rows) > 0:
        val = rows[0].get("value")
        try:
            return int(val)
        except (TypeError, ValueError):
            return val
    return None

async def set_config(key: str, value):
    await sb_upsert("config", {"key": key, "value": str(value)})

async def get_ticket_category_id():
    return await get_config("ticket_category_id")

async def get_auth_category_id():
    return await get_config("auth_category_id")

async def get_log_channel_id():
    return await get_config("log_channel_id")

async def get_mod_log_channel_id():
    val = await get_config("mod_log_channel_id")
    return val or await get_config("log_channel_id")

async def get_backup_channel_id():
    return await get_config("backup_channel_id")

# ===== AutoMod除外ロール（Supabase） =====
async def get_exempt_role_ids() -> list:
    rows = await sb_get("exempt_roles", "select=role_id")
    if rows and len(rows) > 0:
        return [r["role_id"] for r in rows]
    return []

async def add_exempt_role_id(role_id: int):
    await sb_upsert("exempt_roles", {"role_id": role_id})

async def remove_exempt_role_id(role_id: int):
    await sb_delete("exempt_roles", f"role_id=eq.{role_id}")

# ===== 長文スパム除外チャンネル（Supabase） =====
async def get_spam_ignore_ids() -> list:
    rows = await sb_get("spam_ignore", "select=channel_id")
    if rows and len(rows) > 0:
        return [r["channel_id"] for r in rows]
    return []

async def add_spam_ignore_id(channel_id: int):
    await sb_upsert("spam_ignore", {"channel_id": channel_id})

async def remove_spam_ignore_id(channel_id: int):
    await sb_delete("spam_ignore", f"channel_id=eq.{channel_id}")

# ===== 荒らしサーバーブラックリスト（Supabase） =====
# テーブル: blocked_servers { guild_id bigint PK }
async def get_blocked_servers() -> list:
    rows = await sb_get("blocked_servers", "select=guild_id")
    if rows and isinstance(rows, list) and len(rows) > 0 and isinstance(rows[0], dict):
        return [r["guild_id"] for r in rows]
    return []

async def add_blocked_server(guild_id: int):
    await sb_upsert("blocked_servers", {"guild_id": guild_id})

async def remove_blocked_server(guild_id: int):
    await sb_delete("blocked_servers", f"guild_id=eq.{guild_id}")

# ===== 特定ロール許可URLドメイン（Supabase） =====
# テーブル: role_allowed_domains { role_id: int, domain: str }
async def get_role_allowed_domains() -> dict:
    """role_id -> [domain, ...] の辞書を返す"""
    rows = await sb_get("role_allowed_domains", "select=role_id,domain")
    result = defaultdict(list)
    if rows:
        for r in rows:
            result[r["role_id"]].append(r["domain"])
    return dict(result)

async def add_role_allowed_domain(role_id: int, domain: str):
    await sb_upsert("role_allowed_domains", {"role_id": role_id, "domain": domain})

async def remove_role_allowed_domain(role_id: int, domain: str):
    await sb_delete("role_allowed_domains", f"role_id=eq.{role_id}&domain=eq.{domain}")

# ===== 警告ランクロール設定（Supabase config） =====
async def get_warn_role_id(rank: str):
    """rank: 'caution' or 'danger'"""
    return await get_config(f"warn_role_{rank}")

async def set_warn_role_id(rank: str, role_id: int):
    await set_config(f"warn_role_{rank}", role_id)

# ===== 警告ログ Supabase CRUD =====
# テーブル: warn_logs { id, user_id, rank, points, expire_at, created_at }

async def db_add_warn_log(user_id: int, rank: str, points: int, expire_at) -> int:
    """警告ログを1件追加してIDを返す"""
    expire_str = expire_at.isoformat() if expire_at else None
    rows = await sb_upsert("warn_logs", {
        "user_id": user_id,
        "rank": rank,
        "points": points,
        "expire_at": expire_str
    })
    if rows and len(rows) > 0:
        return rows[0].get("id")
    return None

async def db_remove_warn_log(log_id: int):
    """警告ログを1件削除"""
    await sb_delete("warn_logs", f"id=eq.{log_id}")

async def db_remove_all_warn_logs(user_id: int):
    """ユーザーの全警告ログを削除"""
    await sb_delete("warn_logs", f"user_id=eq.{user_id}")

async def db_load_warn_logs() -> dict:
    """起動時にDBから全ログを読み込む
    返り値: { user_id: [ {id, rank, points, expire_at}, ... ] }
    """
    rows = await sb_get("warn_logs", "select=id,user_id,rank,points,expire_at&order=created_at.asc")
    result = {}
    if rows:
        for r in rows:
            expire_at = None
            if r.get("expire_at"):
                try:
                    expire_at = datetime.fromisoformat(r["expire_at"].replace("Z", "+00:00"))
                    if expire_at.tzinfo is None:
                        expire_at = expire_at.replace(tzinfo=timezone.utc)
                except Exception:
                    expire_at = None
            uid = r["user_id"]
            if uid not in result:
                result[uid] = []
            result[uid].append({
                "id": r["id"],
                "rank": r["rank"],
                "points": r["points"],
                "expire_at": expire_at
            })
    return result

# warn_logsのメモリキャッシュ
# { user_id: [ {id, rank, points, expire_at}, ... ] }
warn_logs_cache: dict = {}

async def get_active_warn_points(user_id: int) -> int:
    """有効な警告ログの合計ポイントを返す"""
    logs = warn_logs_cache.get(user_id, [])
    return sum(log["points"] for log in logs)

async def get_current_rank(user_id: int) -> str:
    """有効なログの中で最も重いランクを返す"""
    logs = warn_logs_cache.get(user_id, [])
    if not logs:
        return "none"
    ranks = [log["rank"] for log in logs]
    if "danger" in ranks:
        return "danger"
    if "caution" in ranks:
        return "caution"
    return "light"

# 後方互換性のためのラッパー（旧コードから呼ばれる箇所用）
async def db_set_warn_timer(user_id: int, rank: str, expire_at):
    pass  # warn_logsで管理するため不要

async def db_remove_warn_timer(user_id: int):
    pass  # warn_logsで管理するため不要

# ===== 許可するURLドメイン =====
ALLOWED_DOMAINS = [
    "youtube.com",
    "youtu.be",
]

# ロール別許可ドメイン（起動時にSupabaseから読み込む）
ROLE_ALLOWED_DOMAINS: dict = {}  # { role_id: [domain, ...] }

# 警告ランクタイマーは warn_logs_cache で管理（後方互換）
warn_role_timers: dict = {}  # 後方互換用（実際はwarn_logs_cacheを使用）
BLOCKED_SERVERS: set = set()  # 荒らしサーバーIDリスト（起動時に読み込む）

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

spam_tracker = defaultdict(list)
same_msg_cache = defaultdict(lambda: {"content": "", "count": 0})
content_spam_cache = []
join_tracker = []

BAD_WORDS = []
SPAM_IGNORE_IDS: set = set()
EXEMPT_ROLE_IDS: set = set()


# ===========================
# ===== AutoMod ヘルパー =====
# ===========================

def is_similar(a: str, b: str) -> bool:
    if SequenceMatcher(None, a, b).ratio() >= CONTENT_SPAM_RATIO:
        return True
    if len(a) >= CONTENT_SPAM_PREFIX and len(b) >= CONTENT_SPAM_PREFIX:
        if a[:CONTENT_SPAM_PREFIX] == b[:CONTENT_SPAM_PREFIX]:
            return True
    return False

async def punish_automod(member: discord.Member, guild: discord.Guild, channel: discord.TextChannel, reason: str, detail: str):
    from datetime import timedelta
    timeout_until = discord.utils.utcnow() + timedelta(minutes=TIMEOUT_MINUTES)
    try:
        await member.timeout(timeout_until, reason=reason)
    except (discord.errors.Forbidden, discord.errors.HTTPException):
        pass
    try:
        await member.send(
            f"⚠️ **{guild.name}** で自動タイムアウトされました。\n"
            f"理由: {detail}\n"
            f"タイムアウト時間: {TIMEOUT_MINUTES}分"
        )
    except (discord.errors.Forbidden, discord.errors.HTTPException):
        pass
    await log_action(guild, f"🔨 自動タイムアウト {TIMEOUT_MINUTES}分", member, f"{detail} | 実行者: AutoMod")


def get_message_text(message: discord.Message) -> str:
    """通常メッセージ＋転送メッセージのテキストを結合して返す"""
    parts = [message.content or ""]
    # 転送（フォワード）メッセージのスナップショット
    if hasattr(message, "message_snapshots") and message.message_snapshots:
        for snap in message.message_snapshots:
            if hasattr(snap, "content") and snap.content:
                parts.append(snap.content)
    return "\n".join(p for p in parts if p)


def is_forwarded(message: discord.Message) -> bool:
    """転送メッセージかどうか判定"""
    return bool(getattr(message, "message_snapshots", None))


# ===========================
# ===== AutoMod =====
# ===========================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # DMメッセージはスキップ
    if not message.guild:
        await bot.process_commands(message)
        return

    if message.author.guild_permissions.administrator:
        await bot.process_commands(message)
        return

    member_role_ids = {r.id for r in message.author.roles}
    skip_role_ids = {ADMIN_ROLE_ID, SUB_ROLE_ID} | EXEMPT_ROLE_IDS
    if member_role_ids & skip_role_ids:
        await bot.process_commands(message)
        return

    # 転送メッセージかどうか
    forwarded = is_forwarded(message)

    # テキスト取得（転送元含む）
    text = get_message_text(message).strip()

    _channel_id = message.channel.id
    _thread_parent_id = getattr(message.channel, "parent_id", None)
    _is_spam_ignored = _channel_id in SPAM_IGNORE_IDS or (_thread_parent_id and _thread_parent_id in SPAM_IGNORE_IDS)

    # ロール別許可ドメインを考慮したURL許可チェック
    def is_url_allowed_for_member(domain: str) -> bool:
        domain = domain.lower().replace("www.", "")
        # 全員許可ドメイン
        if any(domain == a or domain.endswith("." + a) for a in ALLOWED_DOMAINS):
            return True
        # ロール別許可ドメイン
        for role_id, domains in ROLE_ALLOWED_DOMAINS.items():
            if role_id in member_role_ids:
                if any(domain == d or domain.endswith("." + d) for d in domains):
                    return True
        return False

    import re as _re
    _urls = _re.findall(r'https?://([^\s/]+)', text)
    _is_allowed_url_only = bool(_urls) and all(is_url_allowed_for_member(d) for d in _urls)

    if text and not _is_allowed_url_only:
        if not _is_spam_ignored:
            stripped = text.replace(" ", "").replace("\n", "").replace("\u3000", "")
            if len(stripped) > MAX_MESSAGE_LENGTH or len(text) > MAX_MESSAGE_LENGTH:
                try:
                    await message.channel.purge(limit=10, check=lambda m: m.author.id == message.author.id, bulk=True)
                except Exception:
                    pass
                detail = "転送メッセージによる長文スパムを検知しました。" if forwarded else "長文スパムを検知しました。"
                await punish_automod(message.author, message.guild, message.channel, "長文スパム検知", detail)
                return

            if text.count("\n") >= MAX_NEWLINES:
                try:
                    await message.channel.purge(limit=10, check=lambda m: m.author.id == message.author.id, bulk=True)
                except Exception:
                    pass
                detail = "転送メッセージによる改行スパムを検知しました。" if forwarded else "改行スパムを検知しました。"
                await punish_automod(message.author, message.guild, message.channel, "改行スパム検知", detail)
                return

        if len(text) >= 6:
            cache = same_msg_cache[message.author.id]
            if text == cache["content"]:
                cache["count"] += 1
            else:
                cache["content"] = text
                cache["count"] = 1
            if cache["count"] >= SPAM_COUNT:
                same_msg_cache[message.author.id] = {"content": "", "count": 0}
                try:
                    await message.channel.purge(limit=10, check=lambda m: m.author.id == message.author.id, bulk=True)
                except Exception:
                    pass
                await punish_automod(message.author, message.guild, message.channel, "連続スパム検知", "同じメッセージを連続で送信しています。")
                return
        else:
            same_msg_cache[message.author.id] = {"content": "", "count": 0}

        now = time.time()
        content_spam_cache[:] = [(uid, t, ts) for uid, t, ts in content_spam_cache if now - ts < CONTENT_SPAM_SECONDS]
        similar = [(uid, t, ts) for uid, t, ts in content_spam_cache if uid != message.author.id and is_similar(text, t)]
        content_spam_cache.append((message.author.id, text, now))
        if len(similar) + 1 >= CONTENT_SPAM_USERS:
            guilty_ids = {uid for uid, _, _ in similar} | {message.author.id}
            content_spam_cache[:] = [(uid, t, ts) for uid, t, ts in content_spam_cache if uid not in guilty_ids]
            from datetime import timedelta
            for uid in guilty_ids:
                m = message.guild.get_member(uid)
                if m and not m.guild_permissions.administrator:
                    try:
                        await m.timeout(discord.utils.utcnow() + timedelta(minutes=TIMEOUT_MINUTES), reason="複数アカウントスパム検知")
                    except Exception:
                        pass
                    try:
                        await m.send(
                            f"⚠️ **{message.guild.name}** で自動タイムアウトされました。\n"
                            f"理由: 複数アカウントによるスパムを検知しました。\n"
                            f"タイムアウト時間: {TIMEOUT_MINUTES}分"
                        )
                    except Exception:
                        pass
                    try:
                        await message.channel.purge(limit=20, check=lambda msg, u=uid: msg.author.id == u, bulk=True)
                    except Exception:
                        pass
            await log_action(message.guild, "🚨 複数アカウントスパム検知", message.author, f"対象ID: {guilty_ids} | 実行者: AutoMod")
            return

    # URLフィルター（転送含む全テキストに適用）
    url_pattern = re.compile(r'https?://([^\s/]+)')
    urls = url_pattern.findall(text)
    for domain in urls:
        if not is_url_allowed_for_member(domain):
            await message.delete()
            await message.channel.send(
                f"🔗 {message.author.mention} このリンクは許可されていません。",
                delete_after=5
            )
            await log_action(message.guild, "🔗 不正URLブロック", message.author, f"URL: `{domain}`{'（転送）' if forwarded else ''}")
            return

    # 禁止ワードフィルター（転送含む全テキストに適用）
    for word in BAD_WORDS:
        if word in text:
            await message.delete()
            count = await get_warns(message.author.id) + 1
            await set_warns(message.author.id, count)

            # 軽度ランク（+1）を自動適用・warn_logsに記録
            from datetime import timedelta as _td
            _now = datetime.now(timezone.utc)
            _expire_at = _now + _td(days=14)
            _log_id = await db_add_warn_log(message.author.id, "light", 1, _expire_at)
            if message.author.id not in warn_logs_cache:
                warn_logs_cache[message.author.id] = []
            warn_logs_cache[message.author.id].append({
                "id": _log_id,
                "rank": "light",
                "points": 1,
                "expire_at": _expire_at
            })

            # チャンネルに一瞬表示
            await message.channel.send(
                f"⚠️ {message.author.mention} 禁止ワードが含まれています。(警告 {count}回目 / 軽度)",
                delete_after=5
            )
            # DMに通知
            try:
                await message.author.send(
                    f"⚠️ **{message.guild.name}** にて禁止ワードを使用されたため、警告（軽度）が付与されました。\n"
                    f"累計警告数: **{count}回**\n\n"
                    f"2週間ルールを守ってご利用いただければ、自動的に解除されます。\n"
                    f"なお、新たに警告を受けた場合はタイマーがリセットされますのでご注意ください。"
                )
            except Exception:
                pass

            await log_action(message.guild, "🚫 禁止ワード検知（軽度）", message.author, f"内容: ||{text}|| | 警告{count}回目{'（転送）' if forwarded else ''}")
            await auto_punish(message.author, message.guild, count)
            return

    # スパムフィルター
    now = datetime.now(timezone.utc).timestamp()
    user_id = message.author.id
    spam_tracker[user_id] = [t for t in spam_tracker[user_id] if now - t < SPAM_INTERVAL]
    spam_tracker[user_id].append(now)

    if len(spam_tracker[user_id]) >= SPAM_LIMIT:
        spam_tracker[user_id] = []
        count = await get_warns(user_id) + 1
        await set_warns(user_id, count)
        await message.channel.send(
            f"⚠️ {message.author.mention} スパムを検知しました。(警告 {count}回目)",
            delete_after=5
        )
        await log_action(message.guild, "🚫 スパム検知", message.author, f"警告{count}回目")
        await auto_punish(message.author, message.guild, count)
        return

    await bot.process_commands(message)


async def auto_punish(member: discord.Member, guild: discord.Guild, count: int):
    from datetime import timedelta
    if count == 3:
        try:
            await member.timeout(discord.utils.utcnow() + timedelta(minutes=5), reason="警告3回（AutoMod）")
            await log_action(guild, "⏱️ タイムアウト5分", member, "警告3回に達したため | 実行者: AutoMod")
        except (discord.errors.Forbidden, discord.errors.HTTPException):
            await log_action(guild, "⚠️ タイムアウト失敗", member, "権限不足のためタイムアウトできませんでした | 実行者: AutoMod")
    elif count == 5:
        try:
            await member.timeout(discord.utils.utcnow() + timedelta(minutes=30), reason="警告5回（AutoMod）")
            await log_action(guild, "⏱️ タイムアウト30分", member, "警告5回に達したため | 実行者: AutoMod")
        except (discord.errors.Forbidden, discord.errors.HTTPException):
            await log_action(guild, "⚠️ タイムアウト失敗", member, "権限不足のためタイムアウトできませんでした | 実行者: AutoMod")
    elif count >= 7:
        try:
            await member.timeout(discord.utils.utcnow() + timedelta(hours=1), reason="警告7回以上（AutoMod）")
            await log_action(guild, "⏱️ タイムアウト1時間", member, "警告7回以上に達したため | 実行者: AutoMod")
        except (discord.errors.Forbidden, discord.errors.HTTPException):
            await log_action(guild, "⚠️ タイムアウト失敗", member, "権限不足のためタイムアウトできませんでした | 実行者: AutoMod")


# ===========================
# ===== メッセージ編集検知 =====
# ===========================

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """編集後のメッセージにもAutoModを適用する"""
    if after.author.bot:
        return
    # 内容が変わっていない場合はスキップ
    if before.content == after.content:
        return
    # on_messageと同じ処理を走らせる
    await on_message(after)


# ===========================
# ===== レイド検知 =====
# ===========================

@bot.event
async def on_member_join(member: discord.Member):
    now = datetime.now(timezone.utc).timestamp()

    # ===== 荒らしサーバーチェック =====
    if BLOCKED_SERVERS:
        for blocked_guild_id in BLOCKED_SERVERS:
            blocked_guild = bot.get_guild(blocked_guild_id)
            if blocked_guild is None:
                continue
            if blocked_guild.get_member(member.id) is not None:
                # 荒らしサーバーに参加中 → DM送信してキック
                try:
                    await member.send(
                        f"⚠️ **{member.guild.name}** への参加が拒否されました。\n"
                        f"荒らしサーバーに参加しているためキックされました。\n"
                        f"ご不明な点がございましたらサーバー管理者までお問い合わせください。"
                    )
                except Exception:
                    pass
                try:
                    await member.kick(reason=f"荒らしサーバー（ID:{blocked_guild_id}）に参加中")
                    await log_action(member.guild, "🚫 荒らしサーバー検知キック", member,
                                     f"参加中の荒らしサーバー: {blocked_guild.name} (`{blocked_guild_id}`)")
                except Exception:
                    pass
                return

    join_tracker.append(now)
    join_tracker[:] = [t for t in join_tracker if now - t < RAID_JOIN_INTERVAL]

    account_age = (datetime.now(timezone.utc) - member.created_at).days
    if account_age < RAID_ACCOUNT_AGE_DAYS:
        await log_action(
            member.guild,
            "🆕 新規アカウント参加",
            member,
            f"アカウント作成から {account_age} 日 | 要注意ユーザーの可能性があります"
        )

    if len(join_tracker) >= RAID_JOIN_LIMIT:
        join_tracker.clear()
        log_ch_id = await get_log_channel_id()
        log_channel = member.guild.get_channel(log_ch_id) if log_ch_id else None
        if log_channel:
            embed = discord.Embed(
                title="🚨 レイド警告！",
                description=f"{RAID_JOIN_INTERVAL}秒以内に{RAID_JOIN_LIMIT}人以上が参加しました。\nレイドの可能性があります！",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            staff_role = member.guild.get_role(ADMIN_ROLE_ID)
            await log_channel.send(content=staff_role.mention if staff_role else "", embed=embed)


# ===========================
# ===== ログ送信 =====
# ===========================

async def log_action(guild: discord.Guild, action: str, user, detail: str = ""):
    ch_id = await get_mod_log_channel_id()
    log_channel = guild.get_channel(ch_id) if ch_id else None
    if not log_channel:
        return
    embed = discord.Embed(
        title=action,
        description=f"**ユーザー:** {user.mention if hasattr(user, 'mention') else user}\n{detail}",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )
    await log_channel.send(embed=embed)


async def log_ticket(guild: discord.Guild, embed: discord.Embed, file=None):
    ch_id = await get_log_channel_id()
    log_channel = guild.get_channel(ch_id) if ch_id else None
    if not log_channel:
        return
    await log_channel.send(embed=embed, file=file)


# ===========================
# ===== 警告システム =====
# ===========================

# ===== 警告ランク モーダル =====
class WarnModal(discord.ui.Modal):
    def __init__(self, member, reason, base_count, rank, min_val, max_val):
        rank_label = {"light": "軽度", "caution": "中度", "danger": "重度"}[rank]
        super().__init__(title=f"警告ランク: {rank_label}")
        self.member = member
        self.reason = reason
        self.base_count = base_count
        self.rank = rank
        self.min_val = min_val
        self.max_val = max_val
        self.points_input = discord.ui.TextInput(
            label=f"加算する警告回数（{min_val}〜{max_val}）",
            placeholder=f"{min_val}〜{max_val}の数字を入力",
            min_length=1,
            max_length=2,
            required=True
        )
        self.add_item(self.points_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            total_add = int(self.points_input.value)
        except ValueError:
            await interaction.response.send_message("❌ 数字を入力してください。", ephemeral=True)
            return
        if not (self.min_val <= total_add <= self.max_val):
            await interaction.response.send_message(
                f"❌ {self.min_val}〜{self.max_val}の数字を入力してください。", ephemeral=True)
            return
        await self.view._apply(interaction, self.rank, total_add)


class WarnSeverity(discord.ui.View):
    def __init__(self, member, reason, base_count):
        super().__init__(timeout=60)
        self.member = member
        self.reason = reason
        self.base_count = base_count

    async def _apply(self, interaction: discord.Interaction, rank: str, total_add: int):
        guild = interaction.guild
        member = self.member
        reason = self.reason
        from datetime import timedelta

        extra = total_add - 1
        if extra > 0:
            current = await get_warns(member.id)
            await set_warns(member.id, current + extra)
        count = await get_warns(member.id)

        rank_label = {"light": "軽度", "caution": "中度", "danger": "重度"}[rank]
        rank_emoji = {"light": "🟡", "caution": "🟠", "danger": "🔴"}[rank]
        rank_color = {"light": discord.Color.yellow(), "caution": discord.Color.orange(), "danger": discord.Color.red()}[rank]
        now = datetime.now(timezone.utc)

        # warn_logsに記録
        if rank == "light":
            expire_at = now + timedelta(days=14)
        elif rank == "caution":
            expire_at = now + timedelta(days=30)
        else:  # danger
            expire_at = None

        log_id = await db_add_warn_log(member.id, rank, total_add, expire_at)
        if member.id not in warn_logs_cache:
            warn_logs_cache[member.id] = []
        warn_logs_cache[member.id].append({
            "id": log_id,
            "rank": rank,
            "points": total_add,
            "expire_at": expire_at
        })

        # 現在の最重ランクのロールを付与（全ランクロールを一旦外してから付与）
        for rank_key in ["caution", "danger"]:
            _rid = await get_warn_role_id(rank_key)
            if _rid:
                _role = guild.get_role(_rid)
                if _role and _role in member.roles:
                    try:
                        await member.remove_roles(_role, reason="警告ランク更新")
                    except Exception:
                        pass
        if rank in ["caution", "danger"]:
            role_id = await get_warn_role_id(rank)
            if role_id:
                role = guild.get_role(role_id)
                if role:
                    try:
                        await member.add_roles(role, reason=f"警告ランク: {rank_label}")
                    except Exception:
                        pass

        # 10回でBAN
        if count >= 10:
            # まず警告embedを全員に表示
            warn_embed = discord.Embed(
                title=f"{rank_emoji} 警告発行（{rank_label}）",
                color=rank_color,
                timestamp=now
            )
            warn_embed.add_field(name="対象", value=member.mention, inline=True)
            warn_embed.add_field(name="累計警告", value=f"**{count}回**", inline=True)
            warn_embed.add_field(name="加算", value=f"+{total_add}回", inline=True)
            warn_embed.add_field(name="理由", value=reason, inline=False)
            warn_embed.set_footer(text="累計警告が10回に達したためBANを実行しました。")
            try:
                await interaction.response.edit_message(content="✅ 処理完了", embed=None, view=None)
            except Exception:
                pass
            try:
                await interaction.followup.send(embed=warn_embed)
                await interaction.followup.send(content=f"🔨 {member.mention} の警告が **{count}回** に達したためBANしました。")
            except Exception:
                await interaction.channel.send(embed=warn_embed)
                await interaction.channel.send(content=f"🔨 {member.mention} の警告が **{count}回** に達したためBANしました。")
            # BAN実行
            try:
                await member.send(f"🔨 **{guild.name}** にて警告が **{count}回** に達したためBANされました。")
            except Exception:
                pass
            try:
                await member.ban(reason=f"警告が{count}回に達したためBAN")
            except Exception:
                pass
            await log_action(guild, "🔨 BAN（警告上限）", member, f"警告{count}回 | 実行者: {interaction.user}")
            self.stop()
            return

        embed = discord.Embed(
            title=f"{rank_emoji} 警告発行（{rank_label}）",
            color=rank_color,
            timestamp=now
        )
        embed.add_field(name="対象", value=member.mention, inline=True)
        embed.add_field(name="累計警告", value=f"**{count}回**", inline=True)
        embed.add_field(name="加算", value=f"+{total_add}回", inline=True)
        embed.add_field(name="理由", value=reason, inline=False)
        footer = {"light": "2週間後に自動解除（新たな警告でリセット）",
                  "caution": "1ヶ月後に自動解除（新たな警告でリセット）",
                  "danger": "永続（自動解除なし）"}[rank]
        embed.set_footer(text=footer)

        try:
            await interaction.response.edit_message(content="✅ 警告を発行しました。", embed=None, view=None)
        except Exception:
            pass
        try:
            await interaction.followup.send(embed=embed)
        except Exception:
            await interaction.channel.send(embed=embed)

        # DMに通知（全員に送る）
        dm_footer = {
            "light": "2週間ルールを守ってご利用いただければ、自動的に解除されます。\nなお、新たに警告を受けた場合はタイマーがリセットされますのでご注意ください。",
            "caution": "1ヶ月間ルールを守ってご利用いただければ、自動的に解除されます。\nなお、新たに警告を受けた場合はタイマーがリセットされますのでご注意ください。",
            "danger": "本警告は永続となります。今後のご利用にはくれぐれもご注意ください。"
        }[rank]
        try:
            await member.send(
                f"{rank_emoji} **{guild.name}** にて警告（{rank_label}）が付与されました。\n"
                f"累計警告数: **{count}回**\n"
                f"理由: {reason}\n\n"
                f"{dm_footer}"
            )
        except Exception:
            pass

        await log_action(guild, f"{rank_emoji} 警告（{rank_label}）", member,
                         f"理由: {reason} | 累計{count}回(+{total_add}) | 実行者: {interaction.user}")
        self.stop()

    @discord.ui.button(label="🟡 軽度（1〜2）", style=discord.ButtonStyle.secondary, custom_id="ws_light")
    async def light(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = WarnModal(self.member, self.reason, self.base_count, "light", 1, 2)
        modal.view = self
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🟠 中度（3〜6）", style=discord.ButtonStyle.primary, custom_id="ws_caution")
    async def caution(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = WarnModal(self.member, self.reason, self.base_count, "caution", 3, 6)
        modal.view = self
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🔴 重度（7〜10）", style=discord.ButtonStyle.danger, custom_id="ws_danger")
    async def danger(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = WarnModal(self.member, self.reason, self.base_count, "danger", 7, 10)
        modal.view = self
        await interaction.response.send_modal(modal)


@bot.tree.command(name="warn", description="ユーザーに警告を出します（スタッフのみ）")
@staff_check()
@app_commands.describe(member="警告するユーザー", reason="理由")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "理由なし"):
    count = await get_warns(member.id) + 1
    await set_warns(member.id, count)

    embed = discord.Embed(
        title="⚠️ 警告ランクを選択してください",
        description=(
            f"対象: {member.mention}\n"
            f"理由: {reason}\n"
            f"現在の累計警告: **{count}回**\n\n"
            f"ランクボタンを押すと回数入力欄が出ます。"
        ),
        color=discord.Color.yellow(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="🟡 軽度", value="1〜2回 | 2週間で自動解除", inline=False)
    embed.add_field(name="🟠 中度", value="3〜6回 | 注意人物ロール付与 | 1ヶ月で自動解除", inline=False)
    embed.add_field(name="🔴 重度", value="7〜10回 | 要注意人物ロール付与 | 永続", inline=False)

    view = WarnSeverity(member, reason, count)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="warnlist", description="全員の警告数一覧を表示します（スタッフのみ）")
@staff_check()
async def warnlist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = await sb_get("warns", "count=gt.0&order=count.desc")
    if not rows:
        await interaction.followup.send("📋 警告のあるユーザーはいません。", ephemeral=True)
        return
    lines = []
    for row in rows:
        user_id = row["user_id"]
        count = row["count"]
        member = interaction.guild.get_member(int(user_id))
        name = member.mention if member else f"ID: {user_id}（退出済み）"
        lines.append(f"{name} → **{count}回**")
    pages = paginate(lines, 20)
    view = PageView(pages, f"⚠️ 警告数一覧（全{len(lines)}人）", discord.Color.orange())
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


@bot.tree.command(name="warns", description="ユーザーの警告数を確認します")
@staff_check()
@app_commands.describe(member="確認するユーザー")
async def warns(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    count = await get_warns(member.id)
    await interaction.followup.send(f"📋 {member.mention} の警告数: **{count}回**", ephemeral=True)


@bot.tree.command(name="clearwarn", description="ユーザーの警告を減らします（スタッフのみ）省略時はリセット")
@staff_check()
@app_commands.describe(member="対象ユーザー", count="減らす回数（省略時はリセット）")
async def clearwarn(interaction: discord.Interaction, member: discord.Member, count: int = None):
    await interaction.response.defer(ephemeral=True)
    current = await get_warns(member.id)
    if count is None:
        await reset_warns(member.id)
        await interaction.followup.send(f"✅ {member.mention} の警告をリセットしました。({current}回 → 0回)", ephemeral=True)
        await log_action(interaction.guild, "🔄 警告リセット", member, f"実行者: {interaction.user} | {current}回 → 0回")
    else:
        if count <= 0:
            await interaction.followup.send("❌ 1以上の回数を指定してください。", ephemeral=True)
            return
        new_count = max(current - count, 0)
        await set_warns(member.id, new_count)
        await interaction.followup.send(f"✅ {member.mention} の警告を {count}回 減らしました。({current}回 → {new_count}回)", ephemeral=True)
        await log_action(interaction.guild, "🔽 警告減算", member, f"実行者: {interaction.user} | {current}回 → {new_count}回（{count}回減算）")


@bot.tree.command(name="set-warn-role", description="警告ランクに付与するロールを設定します（管理者のみ）")
@staff_check()
@app_commands.describe(rank="ランク（中度=注意人物 / 重度=要注意人物）", role="付与するロール")
@app_commands.choices(rank=[
    app_commands.Choice(name="中度（注意人物）", value="caution"),
    app_commands.Choice(name="重度（要注意人物）", value="danger"),
])
async def set_warn_role(interaction: discord.Interaction, rank: str, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    await set_warn_role_id(rank, role.id)
    rank_label = "注意人物（中度）" if rank == "caution" else "要注意人物（重度）"
    await interaction.followup.send(f"✅ **{rank_label}** ロールを {role.mention} に設定しました。", ephemeral=True)
    await log_action(interaction.guild, "⚙️ 警告ランクロール設定", interaction.user, f"ランク: {rank_label} | ロール: {role.name}")


@bot.tree.command(name="kick", description="ユーザーをキックします（スタッフのみ）")
@staff_check()
@app_commands.describe(member="キックするユーザー", reason="理由")
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "理由なし"):
    await interaction.response.defer()
    await member.kick(reason=reason)
    await interaction.followup.send(f"👢 {member.mention} をキックしました。\n理由: {reason}")
    await log_action(interaction.guild, "👢 キック", member, f"理由: {reason} | 実行者: {interaction.user}")


@bot.tree.command(name="ban", description="ユーザーをBANします（スタッフのみ）")
@staff_check()
@app_commands.describe(member="BANするユーザー", reason="理由")
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "理由なし"):
    await interaction.response.defer()
    await member.ban(reason=reason)
    await interaction.followup.send(f"🔨 {member.mention} をBANしました。\n理由: {reason}")
    await log_action(interaction.guild, "🔨 BAN", member, f"理由: {reason} | 実行者: {interaction.user}")


@bot.tree.command(name="unban", description="ユーザーのBANを解除します（スタッフのみ）")
@staff_check()
@app_commands.describe(user_id="解除するユーザーのID")
async def unban(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer()
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        await interaction.followup.send(f"✅ {user} のBANを解除しました。")
        await log_action(interaction.guild, "✅ BAN解除", user, f"実行者: {interaction.user}")
    except Exception:
        await interaction.followup.send("❌ ユーザーが見つからないか、BANされていません。", ephemeral=True)


@bot.tree.command(name="banlist", description="BANされているユーザーの一覧を表示します（スタッフのみ）")
@staff_check()
async def banlist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    bans = []
    async for ban_entry in interaction.guild.bans():
        bans.append(ban_entry)
    if not bans:
        await interaction.followup.send("📋 BANされているユーザーはいません。", ephemeral=True)
        return
    lines = []
    for entry in bans:
        reason = entry.reason or "理由なし"
        lines.append(f"**{entry.user}** (`{entry.user.id}`) - {reason}")
    pages = paginate(lines, 15)
    view = PageView(pages, f"🔨 BAN一覧（全{len(lines)}人）", discord.Color.red())
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


@bot.tree.command(name="timeout", description="ユーザーをタイムアウトします（スタッフのみ）")
@staff_check()
@app_commands.describe(member="対象ユーザー", minutes="タイムアウト時間（分）", reason="理由")
async def timeout_cmd(interaction: discord.Interaction, member: discord.Member, minutes: int = 10, reason: str = "理由なし"):
    await interaction.response.defer()
    until = discord.utils.utcnow() + __import__("datetime").timedelta(minutes=minutes)
    await member.timeout(until, reason=reason)
    await interaction.followup.send(f"⏱️ {member.mention} を{minutes}分タイムアウトしました。\n理由: {reason}")
    await log_action(interaction.guild, f"⏱️ タイムアウト {minutes}分", member, f"理由: {reason} | 実行者: {interaction.user}")


# ===========================
# ===== URL管理コマンド =====
# ===========================

@bot.tree.command(name="url-add", description="許可するURLドメインを追加します（スタッフのみ）")
@staff_check()
@app_commands.describe(domain="追加するドメイン（例: twitter.com）")
async def url_add(interaction: discord.Interaction, domain: str):
    await interaction.response.defer(ephemeral=True)
    domain = domain.lower().replace("www.", "").strip()
    if domain in ALLOWED_DOMAINS:
        await interaction.followup.send(f"⚠️ `{domain}` はすでに許可されています。", ephemeral=True)
        return
    ALLOWED_DOMAINS.append(domain)
    await interaction.followup.send(f"✅ `{domain}` を許可リストに追加しました。", ephemeral=True)
    await log_action(interaction.guild, "🔗 許可URL追加", interaction.user, f"ドメイン: `{domain}`")


@bot.tree.command(name="url-remove", description="許可するURLドメインを削除します（スタッフのみ）")
@staff_check()
@app_commands.describe(domain="削除するドメイン（例: twitter.com）")
async def url_remove(interaction: discord.Interaction, domain: str):
    await interaction.response.defer(ephemeral=True)
    domain = domain.lower().replace("www.", "").strip()
    if domain not in ALLOWED_DOMAINS:
        await interaction.followup.send(f"❌ `{domain}` は登録されていません。", ephemeral=True)
        return
    ALLOWED_DOMAINS.remove(domain)
    await interaction.followup.send(f"✅ `{domain}` を許可リストから削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ 許可URL削除", interaction.user, f"ドメイン: `{domain}`")


@bot.tree.command(name="url-list", description="許可されているURLドメイン一覧を表示します（スタッフのみ）")
@staff_check()
async def url_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(title="🔗 許可URLドメイン一覧", color=discord.Color.green())
    # 全員許可
    global_list = "\n".join([f"・{d}" for d in ALLOWED_DOMAINS]) if ALLOWED_DOMAINS else "なし"
    embed.add_field(name="全員許可", value=global_list, inline=False)
    # ロール別許可
    if ROLE_ALLOWED_DOMAINS:
        for role_id, domains in ROLE_ALLOWED_DOMAINS.items():
            role = interaction.guild.get_role(role_id)
            role_name = role.name if role else f"ID:{role_id}"
            domain_str = "\n".join([f"・{d}" for d in domains]) if domains else "なし"
            embed.add_field(name=f"🎭 {role_name}", value=domain_str, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ===== ロール別許可URL管理コマンド =====

@bot.tree.command(name="role-url-add", description="特定ロールにURLドメインの投稿を許可します（スタッフのみ）")
@staff_check()
@app_commands.describe(role="対象ロール", domain="許可するドメイン（例: twitter.com）")
async def role_url_add(interaction: discord.Interaction, role: discord.Role, domain: str):
    await interaction.response.defer(ephemeral=True)
    global ROLE_ALLOWED_DOMAINS
    domain = domain.lower().replace("www.", "").strip()
    role_domains = ROLE_ALLOWED_DOMAINS.get(role.id, [])
    if domain in role_domains:
        await interaction.followup.send(f"⚠️ {role.mention} にはすでに `{domain}` が許可されています。", ephemeral=True)
        return
    await add_role_allowed_domain(role.id, domain)
    if role.id not in ROLE_ALLOWED_DOMAINS:
        ROLE_ALLOWED_DOMAINS[role.id] = []
    ROLE_ALLOWED_DOMAINS[role.id].append(domain)
    await interaction.followup.send(f"✅ {role.mention} に `{domain}` の投稿を許可しました。", ephemeral=True)
    await log_action(interaction.guild, "🔗 ロール別許可URL追加", interaction.user, f"ロール: {role.name} | ドメイン: `{domain}`")


@bot.tree.command(name="role-url-remove", description="特定ロールの許可URLドメインを削除します（スタッフのみ）")
@staff_check()
@app_commands.describe(role="対象ロール", domain="削除するドメイン（例: twitter.com）")
async def role_url_remove(interaction: discord.Interaction, role: discord.Role, domain: str):
    await interaction.response.defer(ephemeral=True)
    global ROLE_ALLOWED_DOMAINS
    domain = domain.lower().replace("www.", "").strip()
    role_domains = ROLE_ALLOWED_DOMAINS.get(role.id, [])
    if domain not in role_domains:
        await interaction.followup.send(f"❌ {role.mention} に `{domain}` は登録されていません。", ephemeral=True)
        return
    await remove_role_allowed_domain(role.id, domain)
    ROLE_ALLOWED_DOMAINS[role.id].remove(domain)
    if not ROLE_ALLOWED_DOMAINS[role.id]:
        del ROLE_ALLOWED_DOMAINS[role.id]
    await interaction.followup.send(f"✅ {role.mention} から `{domain}` の許可を削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ ロール別許可URL削除", interaction.user, f"ロール: {role.name} | ドメイン: `{domain}`")


@bot.tree.command(name="role-url-list", description="ロール別許可URLドメイン一覧を表示します（スタッフのみ）")
@staff_check()
async def role_url_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not ROLE_ALLOWED_DOMAINS:
        await interaction.followup.send("📋 ロール別許可ドメインは登録されていません。", ephemeral=True)
        return
    embed = discord.Embed(title="🎭 ロール別許可URLドメイン一覧", color=discord.Color.blurple())
    for role_id, domains in ROLE_ALLOWED_DOMAINS.items():
        role = interaction.guild.get_role(role_id)
        role_name = role.mention if role else f"ID:{role_id}（削除済み）"
        domain_str = "\n".join([f"・{d}" for d in domains]) if domains else "なし"
        embed.add_field(name=role_name, value=domain_str, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ===========================
# ===== 禁止ワード管理コマンド =====
# ===========================

@bot.tree.command(name="badword-add", description="禁止ワードを追加します（スタッフのみ）")
@staff_check()
@app_commands.describe(word="追加する禁止ワード")
async def badword_add(interaction: discord.Interaction, word: str):
    await interaction.response.defer(ephemeral=True)
    if word in BAD_WORDS:
        await interaction.followup.send(f"⚠️ `{word}` はすでに登録されています。", ephemeral=True)
        return
    await add_bad_word_db(word)
    BAD_WORDS.append(word)
    await interaction.followup.send(f"✅ `{word}` を禁止ワードに追加しました。", ephemeral=True)
    await log_action(interaction.guild, "🚫 禁止ワード追加", interaction.user, f"追加ワード: `{word}`")


@bot.tree.command(name="badword-remove", description="禁止ワードを削除します（スタッフのみ）")
@staff_check()
@app_commands.describe(word="削除する禁止ワード")
async def badword_remove(interaction: discord.Interaction, word: str):
    await interaction.response.defer(ephemeral=True)
    if word not in BAD_WORDS:
        await interaction.followup.send(f"❌ `{word}` は登録されていません。", ephemeral=True)
        return
    await remove_bad_word_db(word)
    BAD_WORDS.remove(word)
    await interaction.followup.send(f"✅ `{word}` を禁止ワードから削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ 禁止ワード削除", interaction.user, f"削除ワード: `{word}`")


@bot.tree.command(name="badword-list", description="禁止ワード一覧を表示します（スタッフのみ）")
@staff_check()
async def badword_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not BAD_WORDS:
        await interaction.followup.send("📋 禁止ワードは登録されていません。", ephemeral=True)
        return
    lines = [f"・{w}" for w in BAD_WORDS]
    pages = paginate(lines, 20)
    view = PageView(pages, f"🚫 禁止ワード一覧（全{len(lines)}件）", discord.Color.red())
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


# ===========================
# ===== 長文スパム除外チャンネル管理 =====
# ===========================

@bot.tree.command(name="spam-ignore-add", description="長文スパム検知を無効にするチャンネル/スレッドを追加します（スタッフのみ）")
@staff_check()
@app_commands.describe(channel="除外するチャンネルまたはスレッド")
async def spam_ignore_add(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    await interaction.response.defer(ephemeral=True)
    global SPAM_IGNORE_IDS
    if channel.id in SPAM_IGNORE_IDS:
        await interaction.followup.send(f"⚠️ {channel.mention} はすでに除外リストに登録されています。", ephemeral=True)
        return
    await add_spam_ignore_id(channel.id)
    SPAM_IGNORE_IDS.add(channel.id)
    await interaction.followup.send(f"✅ {channel.mention} を長文スパム検知の除外リストに追加しました。", ephemeral=True)
    await log_action(interaction.guild, "📋 スパム除外追加", interaction.user, f"チャンネル: {channel.mention} (`{channel.id}`)")


@bot.tree.command(name="spam-ignore-remove", description="長文スパム検知の除外リストからチャンネル/スレッドを削除します（スタッフのみ）")
@staff_check()
@app_commands.describe(channel="除外リストから外すチャンネルまたはスレッド")
async def spam_ignore_remove(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    await interaction.response.defer(ephemeral=True)
    global SPAM_IGNORE_IDS
    if channel.id not in SPAM_IGNORE_IDS:
        await interaction.followup.send(f"❌ {channel.mention} は除外リストに登録されていません。", ephemeral=True)
        return
    await remove_spam_ignore_id(channel.id)
    SPAM_IGNORE_IDS.discard(channel.id)
    await interaction.followup.send(f"✅ {channel.mention} を除外リストから削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ スパム除外削除", interaction.user, f"チャンネル: {channel.mention} (`{channel.id}`)")


@bot.tree.command(name="spam-ignore-list", description="長文スパム検知の除外リストを表示します（スタッフのみ）")
@staff_check()
async def spam_ignore_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not SPAM_IGNORE_IDS:
        await interaction.followup.send("📋 除外リストにチャンネルはありません。", ephemeral=True)
        return
    lines = []
    for cid in SPAM_IGNORE_IDS:
        ch = interaction.guild.get_channel(cid)
        lines.append(f"・{ch.mention} (`{cid}`)" if ch else f"・不明なチャンネル (`{cid}`)")
    pages = paginate(lines, 20)
    view = PageView(pages, f"📋 長文スパム除外リスト（全{len(lines)}件）", discord.Color.blurple())
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


# ===========================
# ===== AutoMod除外ロール管理 =====
# ===========================

@bot.tree.command(name="exempt-role-add", description="AutoMod検知対象外にするロールを追加します（スタッフのみ）")
@staff_check()
@app_commands.describe(role="除外するロール")
async def exempt_role_add(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    global EXEMPT_ROLE_IDS
    if role.id in EXEMPT_ROLE_IDS:
        await interaction.followup.send(f"⚠️ {role.mention} はすでに除外リストに登録されています。", ephemeral=True)
        return
    await add_exempt_role_id(role.id)
    EXEMPT_ROLE_IDS.add(role.id)
    await interaction.followup.send(f"✅ {role.mention} をAutoMod除外ロールに追加しました。", ephemeral=True)
    await log_action(interaction.guild, "🛡️ AutoMod除外ロール追加", interaction.user, f"ロール: {role.name} (`{role.id}`)")


@bot.tree.command(name="exempt-role-remove", description="AutoMod除外ロールを解除します（スタッフのみ）")
@staff_check()
@app_commands.describe(role="除外を解除するロール")
async def exempt_role_remove(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    global EXEMPT_ROLE_IDS
    if role.id not in EXEMPT_ROLE_IDS:
        await interaction.followup.send(f"❌ {role.mention} は除外リストに登録されていません。", ephemeral=True)
        return
    await remove_exempt_role_id(role.id)
    EXEMPT_ROLE_IDS.discard(role.id)
    await interaction.followup.send(f"✅ {role.mention} をAutoMod除外ロールから削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ AutoMod除外ロール削除", interaction.user, f"ロール: {role.name} (`{role.id}`)")


@bot.tree.command(name="exempt-role-list", description="AutoMod除外ロール一覧を表示します（スタッフのみ）")
@staff_check()
async def exempt_role_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not EXEMPT_ROLE_IDS:
        await interaction.followup.send("📋 除外ロールは登録されていません。", ephemeral=True)
        return
    lines = []
    for rid in EXEMPT_ROLE_IDS:
        role = interaction.guild.get_role(rid)
        lines.append(f"・{role.mention} (`{rid}`)" if role else f"・不明なロール (`{rid}`)")
    pages = paginate(lines, 20)
    view = PageView(pages, f"🛡️ AutoMod除外ロール一覧（全{len(lines)}件）", discord.Color.blurple())
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


# ===========================
# ===== 荒らしサーバーブラックリスト管理 =====
# ===========================

@bot.tree.command(name="blocked-server-add", description="荒らしサーバーをブラックリストに追加します（管理者のみ）")
@admin_check()
@app_commands.describe(guild_id="ブロックするサーバーのID")
async def blocked_server_add(interaction: discord.Interaction, guild_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        gid = int(guild_id)
    except ValueError:
        await interaction.followup.send("❌ 正しいサーバーIDを入力してください。", ephemeral=True)
        return
    if gid in BLOCKED_SERVERS:
        await interaction.followup.send(f"⚠️ `{gid}` はすでに登録されています。", ephemeral=True)
        return
    await add_blocked_server(gid)
    BLOCKED_SERVERS.add(gid)
    guild_obj = bot.get_guild(gid)
    name = guild_obj.name if guild_obj else "（Botが未参加のサーバー）"
    await interaction.followup.send(f"✅ `{name}` (`{gid}`) をブラックリストに追加しました。", ephemeral=True)
    await log_action(interaction.guild, "🚫 荒らしサーバー追加", interaction.user, f"サーバーID: `{gid}` | {name}")


@bot.tree.command(name="blocked-server-remove", description="荒らしサーバーをブラックリストから削除します（管理者のみ）")
@admin_check()
@app_commands.describe(guild_id="削除するサーバーのID")
async def blocked_server_remove(interaction: discord.Interaction, guild_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        gid = int(guild_id)
    except ValueError:
        await interaction.followup.send("❌ 正しいサーバーIDを入力してください。", ephemeral=True)
        return
    if gid not in BLOCKED_SERVERS:
        await interaction.followup.send(f"❌ `{gid}` は登録されていません。", ephemeral=True)
        return
    await remove_blocked_server(gid)
    BLOCKED_SERVERS.discard(gid)
    await interaction.followup.send(f"✅ `{gid}` をブラックリストから削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ 荒らしサーバー削除", interaction.user, f"サーバーID: `{gid}`")


@bot.tree.command(name="blocked-server-list", description="荒らしサーバーのブラックリストを表示します（管理者のみ）")
@admin_check()
async def blocked_server_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not BLOCKED_SERVERS:
        await interaction.followup.send("📋 ブラックリストにサーバーはありません。", ephemeral=True)
        return
    lines = []
    for gid in BLOCKED_SERVERS:
        guild_obj = bot.get_guild(gid)
        name = guild_obj.name if guild_obj else "（未参加）"
        lines.append(f"・{name} (`{gid}`)")
    pages = paginate(lines, 20)
    view = PageView(pages, f"🚫 荒らしサーバーブラックリスト（{len(lines)}件）", discord.Color.red())
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


# ===========================
# ===== セットアップコマンド =====
# ===========================

@bot.tree.command(name="setup", description="ボットの設定を一括で行います（管理者のみ）")
@admin_check()
@app_commands.describe(
    auth_category="認証チケット用カテゴリ",
    ticket_log="チケットログチャンネル",
    mod_log="モデレーションログチャンネル",
    backup_channel="自動バックアップ送信チャンネル"
)
async def setup(
    interaction: discord.Interaction,
    auth_category: discord.CategoryChannel = None,
    ticket_log: discord.TextChannel = None,
    mod_log: discord.TextChannel = None,
    backup_channel: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)
    if not any([auth_category, ticket_log, mod_log, backup_channel]):
        guild = interaction.guild
        auth_cat_id = await get_auth_category_id()
        log_id = await get_log_channel_id()
        mod_id = await get_mod_log_channel_id()
        bk_id = await get_backup_channel_id()
        auth_cat = guild.get_channel(auth_cat_id) if auth_cat_id else None
        log_ch = guild.get_channel(log_id) if log_id else None
        mod_ch = guild.get_channel(mod_id) if mod_id else None
        bk_ch = guild.get_channel(bk_id) if bk_id else None
        embed = discord.Embed(title="⚙️ 現在の設定", color=discord.Color.blurple())
        embed.add_field(name="🔑 認証チケットカテゴリ", value=auth_cat.name if auth_cat else "❌ 未設定", inline=False)
        embed.add_field(name="📋 チケットログ", value=log_ch.mention if log_ch else "❌ 未設定", inline=False)
        embed.add_field(name="🔨 モデレーションログ", value=mod_ch.mention if mod_ch else "⚠️ 未設定（チケットログと共用）", inline=False)
        embed.add_field(name="💾 バックアップチャンネル", value=bk_ch.mention if bk_ch else "⚠️ 未設定", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    changed = []
    if auth_category:
        await set_config("auth_category_id", auth_category.id)
        changed.append(f"🔑 認証カテゴリ → **{auth_category.name}**")
    if ticket_log:
        await set_config("log_channel_id", ticket_log.id)
        changed.append(f"📋 チケットログ → {ticket_log.mention}")
    if mod_log:
        await set_config("mod_log_channel_id", mod_log.id)
        changed.append(f"🔨 モデレーションログ → {mod_log.mention}")
    if backup_channel:
        await set_config("backup_channel_id", backup_channel.id)
        changed.append(f"💾 バックアップチャンネル → {backup_channel.mention}")
        data = await create_backup(interaction.guild)
        filename = f"backup-{interaction.guild.id}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        file = discord.File(fp=io.BytesIO(json_bytes), filename=filename)
        embed_bk = discord.Embed(
            title="💾 バックアップ開始",
            description="バックアップチャンネルを設定しました。これより1時間ごとに自動バックアップを送信します。",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        await backup_channel.send(embed=embed_bk, file=file)

    embed = discord.Embed(title="✅ 設定を更新しました", description="\n".join(changed), color=discord.Color.green())
    await interaction.followup.send(embed=embed, ephemeral=True)


# ===========================
# ===== チケット機能 =====
# ===========================

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="❓ サポート・質問", style=discord.ButtonStyle.success, custom_id="ticket_support")
    async def ticket_support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "support", "サポート・質問")

    @discord.ui.button(label="📩 その他・お問い合わせ", style=discord.ButtonStyle.secondary, custom_id="ticket_other")
    async def ticket_other(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "inquiry", "その他・お問い合わせ")


class InquiryPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📩 お問い合わせ", style=discord.ButtonStyle.secondary, custom_id="ticket_inquiry")
    async def ticket_inquiry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "inquiry", "お問い合わせ")


class AuthPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔑 認証リクエスト", style=discord.ButtonStyle.primary, custom_id="ticket_auth")
    async def ticket_auth(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "auth-request", "認証リクエスト", auth=True)


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 チケットを閉じる", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        admin_role = interaction.guild.get_role(ADMIN_ROLE_ID)
        is_admin = interaction.user.guild_permissions.administrator
        has_admin_role = admin_role in interaction.user.roles if admin_role else False
        if not is_admin and not has_admin_role:
            await interaction.response.send_message("❌ 管理者のみチケットを閉じられます。", ephemeral=True)
            return

        await interaction.response.send_message("🔒 チケットを閉じています。ログを保存中...")

        log_text = f"=== チケットログ: {channel.name} ===\n"
        log_text += f"クローズ日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        log_text += f"クローズ者: {interaction.user} ({interaction.user.id})\n\n"

        async for msg in channel.history(limit=None, oldest_first=True):
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            log_text += f"[{timestamp}] {msg.author}: {msg.content}\n"
            for attachment in msg.attachments:
                log_text += f"  [添付ファイル: {attachment.url}]\n"

        log_ch_id = await get_log_channel_id()
        log_channel = interaction.guild.get_channel(log_ch_id) if log_ch_id else None
        if log_channel:
            log_file = discord.File(fp=io.BytesIO(log_text.encode("utf-8")), filename=f"{channel.name}-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt")
            embed = discord.Embed(title="📋 チケットログ", description=f"チャンネル: `{channel.name}`\nクローズ者: {interaction.user.mention}", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
            await log_ticket(interaction.guild, embed, log_file)

        await asyncio.sleep(3)
        await channel.delete(reason=f"チケットクローズ by {interaction.user}")


async def create_ticket(interaction: discord.Interaction, ticket_type: str, label: str, auth: bool = False):
    guild = interaction.guild
    member = interaction.user
    if auth:
        cat_id = await get_auth_category_id()
        category = guild.get_channel(cat_id) if cat_id else None
        if category is None:
            await interaction.response.send_message("❌ 認証チケット用カテゴリが未設定です。`/setup` で設定してください。", ephemeral=True)
            return
    elif ticket_type == "inquiry":
        cat_id = await get_config("inquiry_category_id")
        if not cat_id:
            cat_id = await get_ticket_category_id()
        category = guild.get_channel(cat_id) if cat_id else None
        if category is None:
            await interaction.response.send_message("❌ お問い合わせ用カテゴリが未設定です。`/ticket-panel` で設定してください。", ephemeral=True)
            return
    else:
        cat_id = await get_ticket_category_id()
        category = guild.get_channel(cat_id) if cat_id else None
        if category is None:
            await interaction.response.send_message("❌ チケット用カテゴリが未設定です。`/setup` で設定してください。", ephemeral=True)
            return

    existing = discord.utils.get(category.channels, name=f"{ticket_type}-{member.name.lower()}")
    if existing:
        await interaction.response.send_message(f"❌ すでにチケットがあります: {existing.mention}", ephemeral=True)
        return

    admin_role = guild.get_role(ADMIN_ROLE_ID)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    channel = await guild.create_text_channel(name=f"{ticket_type}-{member.name.lower()}", category=category, overwrites=overwrites, topic=f"{label} | {member} ({member.id})")

    if auth:
        desc = (
            f"{member.mention} 認証リクエストを受け付けました。\n\n"
            f"⚠️ このチケットは管理者が管理しているためロールが着くまで遅くなる可能性があります。\n\n"
            f"チケットを閉じる場合は下のボタンを押してください（管理者のみ）。"
        )
    else:
        desc = (
            f"{member.mention} さん、チケットを作成しました！\n\n"
            f"**内容を詳しく教えてください。**\nスタッフが確認次第、対応いたします。\n\n"
            f"チケットを閉じる場合は下のボタンを押してください（管理者のみ）。"
        )
    embed = discord.Embed(title=f"🎫 {label}", description=desc, color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"チケットID: {channel.id}")
    await channel.send(content=member.mention, embed=embed, view=TicketView())
    await interaction.response.send_message(f"✅ チケットを作成しました: {channel.mention}", ephemeral=True)


@bot.tree.command(name="auth-panel", description="認証リクエストパネルを送信します（管理者のみ）")
@staff_check()
@app_commands.describe(category="認証チケット用カテゴリ（省略で現在の設定を使用）")
async def send_auth_panel(interaction: discord.Interaction, category: discord.CategoryChannel = None):
    await interaction.response.defer(ephemeral=True)
    if category:
        await set_config("auth_category_id", category.id)
    embed = discord.Embed(
        title="🔑 認証リクエスト",
        description="認証ができない方はボタンを押してリクエストを送ってください。\n\n🔑 **認証リクエスト** — 認証ができない方向けのサポート",
        color=discord.Color.gold()
    )
    await interaction.channel.send(embed=embed, view=AuthPanelView())
    await interaction.followup.send("✅ 認証リクエストパネルを送信しました。", ephemeral=True)


@bot.tree.command(name="ticket-panel", description="サポート＆お問い合わせパネルを送信します（管理者のみ）")
@staff_check()
@app_commands.describe(
    support_category="サポート・質問チケット用カテゴリ",
    inquiry_category="お問い合わせチケット用カテゴリ（省略するとサポートカテゴリと同じになります）"
)
async def send_panel(interaction: discord.Interaction, support_category: discord.CategoryChannel = None, inquiry_category: discord.CategoryChannel = None):
    await interaction.response.defer(ephemeral=True)
    if support_category:
        await set_config("ticket_category_id", support_category.id)
    if inquiry_category:
        await set_config("inquiry_category_id", inquiry_category.id)

    s_cat_id = await get_ticket_category_id()
    i_cat_id = await get_config("inquiry_category_id")
    s_cat = interaction.guild.get_channel(s_cat_id) if s_cat_id else None
    i_cat = interaction.guild.get_channel(i_cat_id) if i_cat_id else None

    embed = discord.Embed(
        title="🎫 サポートチケット",
        description="❓ **サポート・質問** — サーバーに関する質問・サポート\n📩 **お問い合わせ** — その他のお問い合わせ",
        color=discord.Color.blurple()
    )
    await interaction.channel.send(embed=embed, view=TicketPanelView())

    info = []
    info.append(f"✅ サポートカテゴリ: **{s_cat.name}**" if s_cat else "⚠️ サポートカテゴリ: **未設定**")
    info.append(f"✅ お問い合わせカテゴリ: **{i_cat.name}**" if i_cat else "⚠️ お問い合わせカテゴリ: **未設定**")
    await interaction.followup.send("✅ パネルを送信しました。\n" + "\n".join(info), ephemeral=True)


@bot.tree.command(name="inquiry-panel", description="お問い合わせパネルを送信します（管理者のみ）")
@staff_check()
@app_commands.describe(category="お問い合わせチケット用カテゴリ（省略で現在の設定を使用）")
async def send_inquiry_panel(interaction: discord.Interaction, category: discord.CategoryChannel = None):
    if category:
        await set_config("inquiry_category_id", category.id)
    embed = discord.Embed(title="📩 お問い合わせ", description="📩 **お問い合わせ** — その他のお問い合わせはこちら", color=discord.Color.blurple())
    await interaction.channel.send(embed=embed, view=InquiryPanelView())
    await interaction.followup.send("✅ お問い合わせパネルを送信しました。", ephemeral=True)


@bot.tree.command(name="botstatus", description="Botの現在の設定を表示します（スタッフのみ）")
@admin_check()
async def botstatus(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    cat_id = await get_ticket_category_id()
    auth_cat_id = await get_auth_category_id()
    inq_cat_id = await get_config("inquiry_category_id")
    log_id = await get_log_channel_id()
    mod_log_id = await get_mod_log_channel_id()
    category = guild.get_channel(cat_id) if cat_id else None
    auth_cat = guild.get_channel(auth_cat_id) if auth_cat_id else None
    inq_cat = guild.get_channel(inq_cat_id) if inq_cat_id else None
    log_ch = guild.get_channel(log_id) if log_id else None
    mod_log_ch = guild.get_channel(mod_log_id) if mod_log_id else None

    # 警告ランクロール取得
    caution_role_id = await get_warn_role_id("caution")
    danger_role_id = await get_warn_role_id("danger")
    caution_role = guild.get_role(caution_role_id) if caution_role_id else None
    danger_role = guild.get_role(danger_role_id) if danger_role_id else None

    # ===== 設定状況 =====
    embed = discord.Embed(title="🤖 Bot設定状況 & コマンド一覧", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

    # チケット設定
    embed.add_field(name="🎫 サポートカテゴリ", value=category.name if category else "❌ 未設定", inline=True)
    embed.add_field(name="📩 お問い合わせカテゴリ", value=inq_cat.name if inq_cat else "❌ 未設定", inline=True)
    embed.add_field(name="🔑 認証カテゴリ", value=auth_cat.name if auth_cat else "❌ 未設定", inline=True)
    embed.add_field(name="📋 チケットログ", value=log_ch.mention if log_ch else "❌ 未設定", inline=True)
    embed.add_field(name="🔨 モデレーションログ", value=mod_log_ch.mention if mod_log_ch else "チケットログと共用", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # AutoMod設定
    embed.add_field(
        name="🛡️ AutoMod設定",
        value=(
            f"長文: **{MAX_MESSAGE_LENGTH}文字**以上\n"
            f"改行: **{MAX_NEWLINES}回**以上\n"
            f"連続スパム: **{SPAM_COUNT}回**\n"
            f"複数垢スパム: **{CONTENT_SPAM_SECONDS}秒**以内に**{CONTENT_SPAM_USERS}人**\n"
            f"自動TO: **{TIMEOUT_MINUTES}分**\n"
            f"転送メッセージ: **検知対象**"
        ),
        inline=False
    )

    # URL設定
    domain_list = "\n".join([f"・{d}" for d in ALLOWED_DOMAINS]) if ALLOWED_DOMAINS else "なし"
    embed.add_field(name="🔗 許可URL（全員）", value=domain_list, inline=True)
    role_url_info = ""
    if ROLE_ALLOWED_DOMAINS:
        for role_id, domains in ROLE_ALLOWED_DOMAINS.items():
            role = guild.get_role(role_id)
            role_url_info += f"**{role.name if role else role_id}**: {', '.join(domains)}\n"
    embed.add_field(name="🎭 ロール別許可URL", value=role_url_info or "なし", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # 禁止ワード
    word_list = "　".join([f"`{w}`" for w in BAD_WORDS]) if BAD_WORDS else "なし"
    embed.add_field(name="🚫 禁止ワード", value=word_list, inline=False)

    # 警告ランク設定
    embed.add_field(
        name="⚠️ 警告ランク制度",
        value=(
            f"🟡 **軽度**（+1〜2回）: 2週間で自動解除\n"
            f"🟠 **中度**（+3〜6回）: {caution_role.mention if caution_role else '❌ ロール未設定'} | 1ヶ月で自動解除\n"
            f"🔴 **重度**（+7〜10回）: {danger_role.mention if danger_role else '❌ ロール未設定'} | 永続\n"
            f"累計**10回**でBAN自動実行"
        ),
        inline=False
    )

    await interaction.followup.send(embed=embed)

    # ===== コマンド一覧（別embed） =====
    embed2 = discord.Embed(title="📖 コマンド一覧", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed2.add_field(
        name="⚠️ 警告・モデレーション",
        value=(
            "`/warn @ユーザー 理由` — 警告発行（ランク選択UI）\n"
            "`/warns @ユーザー` — 警告数確認\n"
            "`/warnlist` — 全員の警告数一覧\n"
            "`/clearwarn @ユーザー 回数` — 警告リセット/減算\n"
            "`/set-warn-role rank role` — 警告ランクロール設定\n"
            "`/kick @ユーザー 理由` — キック\n"
            "`/ban @ユーザー 理由` — BAN\n"
            "`/unban ユーザーID` — BAN解除\n"
            "`/banlist` — BAN一覧\n"
            "`/timeout @ユーザー 分数 理由` — タイムアウト"
        ),
        inline=False
    )
    embed2.add_field(
        name="🔗 URL管理",
        value=(
            "`/url-add ドメイン` — 全員許可URLを追加\n"
            "`/url-remove ドメイン` — 全員許可URLを削除\n"
            "`/url-list` — 全員許可URL一覧\n"
            "`/role-url-add @ロール ドメイン` — ロール別許可URLを追加\n"
            "`/role-url-remove @ロール ドメイン` — ロール別許可URLを削除\n"
            "`/role-url-list` — ロール別許可URL一覧"
        ),
        inline=False
    )
    embed2.add_field(
        name="🚫 禁止ワード",
        value=(
            "`/badword-add ワード` — 禁止ワード追加\n"
            "`/badword-remove ワード` — 禁止ワード削除\n"
            "`/badword-list` — 禁止ワード一覧"
        ),
        inline=False
    )
    embed2.add_field(
        name="🛡️ AutoMod除外設定",
        value=(
            "`/spam-ignore-add #チャンネル` — 長文検知除外に追加\n"
            "`/spam-ignore-remove #チャンネル` — 長文検知除外から削除\n"
            "`/spam-ignore-list` — 長文検知除外一覧\n"
            "`/exempt-role-add @ロール` — AutoMod除外ロール追加\n"
            "`/exempt-role-remove @ロール` — AutoMod除外ロール削除\n"
            "`/exempt-role-list` — AutoMod除外ロール一覧"
        ),
        inline=False
    )
    embed2.add_field(
        name="🎫 チケット・パネル",
        value=(
            "`/auth-panel` — 認証リクエストパネルを送信\n"
            "`/ticket-panel` — サポートパネルを送信\n"
            "`/inquiry-panel` — お問い合わせパネルを送信"
        ),
        inline=False
    )
    embed2.add_field(
        name="⚙️ セットアップ・管理",
        value=(
            "`/setup` — Bot設定（カテゴリ・ログch等）\n"
            "`/botstatus` — 設定確認＆コマンド一覧\n"
            "`/backup` — サーバー構成をバックアップ\n"
            "`/restore` — バックアップから復元"
        ),
        inline=False
    )
    await interaction.followup.send(embed=embed2)


# ===========================
# ===== バックアップ・復元 =====
# ===========================

@bot.tree.command(name="backup", description="サーバーのチャンネル構成とロールをバックアップします（管理者のみ）")
@admin_check()
async def backup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    roles = []
    for role in guild.roles:
        if role.is_default():
            continue
        roles.append({"name": role.name, "color": role.color.value, "hoist": role.hoist, "mentionable": role.mentionable, "permissions": role.permissions.value, "position": role.position})
    categories = []
    no_category_channels = []
    for category in guild.categories:
        channels = []
        for ch in category.channels:
            channels.append({"name": ch.name, "type": str(ch.type), "position": ch.position, "topic": getattr(ch, "topic", None), "nsfw": getattr(ch, "nsfw", False), "slowmode_delay": getattr(ch, "slowmode_delay", 0)})
        categories.append({"name": category.name, "position": category.position, "channels": channels})
    for ch in guild.channels:
        if ch.category is None and not isinstance(ch, discord.CategoryChannel):
            no_category_channels.append({"name": ch.name, "type": str(ch.type), "position": ch.position})
    backup_data = {"guild_name": guild.name, "backup_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "roles": sorted(roles, key=lambda r: r["position"]), "categories": sorted(categories, key=lambda c: c["position"]), "no_category_channels": no_category_channels}
    filename = f"backup-{guild.id}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    json_bytes = json.dumps(backup_data, ensure_ascii=False, indent=2).encode("utf-8")
    file = discord.File(fp=io.BytesIO(json_bytes), filename=filename)
    embed = discord.Embed(title="✅ バックアップ完了", description=f"**サーバー:** {guild.name}\n**ロール数:** {len(roles)}\n**カテゴリ数:** {len(categories)}\n**日時:** {backup_data['backup_at']}", color=discord.Color.green())
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)


@bot.tree.command(name="restore", description="バックアップからチャンネル構成とロールを復元します（管理者のみ）")
@admin_check()
@app_commands.describe(file="backupコマンドで生成したJSONファイル")
async def restore(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if not file.filename.endswith(".json"):
        await interaction.followup.send("❌ JSONファイルを添付してください。", ephemeral=True)
        return
    async with aiohttp.ClientSession() as session:
        async with session.get(file.url) as resp:
            raw = await resp.text()
    try:
        data = json.loads(raw)
    except Exception:
        await interaction.followup.send("❌ ファイルの読み込みに失敗しました。", ephemeral=True)
        return
    results = []
    existing_role_names = {r.name for r in guild.roles}
    created_roles = 0
    for role_data in sorted(data.get("roles", []), key=lambda r: r["position"]):
        if role_data["name"] in existing_role_names:
            continue
        try:
            await guild.create_role(name=role_data["name"], color=discord.Color(role_data["color"]), hoist=role_data["hoist"], mentionable=role_data["mentionable"], permissions=discord.Permissions(role_data["permissions"]))
            created_roles += 1
            await asyncio.sleep(0.5)
        except Exception:
            pass
    results.append(f"✅ ロール: {created_roles}個作成")
    existing_channel_names = {c.name for c in guild.channels}
    created_categories = 0
    created_channels = 0
    for cat_data in sorted(data.get("categories", []), key=lambda c: c["position"]):
        if cat_data["name"] not in existing_channel_names:
            try:
                category = await guild.create_category(name=cat_data["name"])
                created_categories += 1
                await asyncio.sleep(0.5)
            except Exception:
                continue
        else:
            category = discord.utils.get(guild.categories, name=cat_data["name"])
        if category is None:
            continue
        for ch_data in sorted(cat_data.get("channels", []), key=lambda c: c["position"]):
            if ch_data["name"] in existing_channel_names:
                continue
            try:
                if ch_data["type"] == "text":
                    await guild.create_text_channel(name=ch_data["name"], category=category, topic=ch_data.get("topic"), nsfw=ch_data.get("nsfw", False), slowmode_delay=ch_data.get("slowmode_delay", 0))
                elif ch_data["type"] == "voice":
                    await guild.create_voice_channel(name=ch_data["name"], category=category)
                created_channels += 1
                await asyncio.sleep(0.5)
            except Exception:
                pass
    results.append(f"✅ カテゴリ: {created_categories}個作成")
    results.append(f"✅ チャンネル: {created_channels}個作成")
    results.append("⚠️ すでに存在するロール・チャンネルはスキップしました")
    results.append("⚠️ メッセージ履歴は復元できません")
    embed = discord.Embed(title="✅ 復元完了", description="\n".join(results), color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    await interaction.followup.send(embed=embed, ephemeral=True)


# ===========================
# ===== エラーハンドラ =====
# ===========================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.TransformerError):
        msg = "❌ ユーザーが見つかりません。サーバーにいるメンバーを指定してください。"
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "❌ 権限が不足しています。"
    elif isinstance(error, app_commands.CheckFailure):
        return
    else:
        msg = f"❌ エラーが発生しました: {type(error).__name__}"
        print(f"[AppCommandError] {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# ===========================
# ===== Bot起動 =====
# ===========================

@bot.event
async def on_ready():
    global BAD_WORDS, SPAM_IGNORE_IDS, EXEMPT_ROLE_IDS, ROLE_ALLOWED_DOMAINS, warn_role_timers, warn_logs_cache, BLOCKED_SERVERS
    BAD_WORDS = await load_bad_words_db()
    SPAM_IGNORE_IDS = set(await get_spam_ignore_ids())
    EXEMPT_ROLE_IDS = set(await get_exempt_role_ids())
    ROLE_ALLOWED_DOMAINS = await get_role_allowed_domains()
    warn_logs_cache.update(await db_load_warn_logs())
    BLOCKED_SERVERS.update(await get_blocked_servers())
    check_auth_tickets.start()
    check_warn_role_expire.start()
    auto_backup.start()
    bot.add_view(TicketPanelView())
    bot.add_view(InquiryPanelView())
    bot.add_view(AuthPanelView())
    bot.add_view(TicketView())
    try:
        # ギルド固有コマンドをクリア（重複解消のため一度だけ実行）
        guild_obj = discord.Object(id=1471075951445278903)
        bot.tree.clear_commands(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
        print("✅ ギルドコマンドをクリアしました")
        # グローバル同期
        synced = await bot.tree.sync()
        print(f"✅ スラッシュコマンドを同期しました ({len(synced)}個)")
    except Exception as e:
        print(f"❌ 同期エラー: {e}")
    print(f"✅ {bot.user} としてログインしました")


# ===========================
# ===== 認証チケット自動削除 =====
# ===========================

@tasks.loop(minutes=10)
async def check_warn_role_expire():
    """警告ログを1件ずつ期限チェックして削除・ロール更新する"""
    now = datetime.now(timezone.utc)
    for uid in list(warn_logs_cache.keys()):
        logs = warn_logs_cache.get(uid, [])
        expired_logs = [l for l in logs if l["expire_at"] is not None and l["expire_at"] <= now]
        if not expired_logs:
            continue
        for log in expired_logs:
            await db_remove_warn_log(log["id"])
            warn_logs_cache[uid] = [l for l in warn_logs_cache.get(uid, []) if l["id"] != log["id"]]
            # warns テーブルも減算
            current = await get_warns(uid)
            new_count = max(current - log["points"], 0)
            await set_warns(uid, new_count)

        # ロールを現在の最重ランクに更新
        remaining = warn_logs_cache.get(uid, [])
        new_rank = "none"
        if remaining:
            ranks = [l["rank"] for l in remaining]
            if "danger" in ranks:
                new_rank = "danger"
            elif "caution" in ranks:
                new_rank = "caution"
            else:
                new_rank = "light"

        # 全ランクロールを一旦外す
        for guild in bot.guilds:
            member = guild.get_member(uid)
            if not member:
                continue
            for rank_key in ["caution", "danger"]:
                role_id = await get_warn_role_id(rank_key)
                if role_id:
                    role = guild.get_role(role_id)
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="警告ログ期限切れ")
                        except Exception:
                            pass
            # 新しいランクのロールを付与
            if new_rank in ["caution", "danger"]:
                role_id = await get_warn_role_id(new_rank)
                if role_id:
                    role = guild.get_role(role_id)
                    if role:
                        try:
                            await member.add_roles(role, reason=f"警告ランク更新: {new_rank}")
                        except Exception:
                            pass
            rank_label = {"light": "軽度", "caution": "中度", "danger": "重度", "none": "なし"}[new_rank]
            expired_points = sum(l["points"] for l in expired_logs)
            await log_action(guild, "✅ 警告ログ自動削除", member,
                             f"削除: {len(expired_logs)}件(-{expired_points}回) | 残り: {len(remaining)}件 | 現ランク: {rank_label}")
        if not remaining:
            warn_logs_cache.pop(uid, None)


@check_warn_role_expire.before_loop
async def before_warn_expire():
    await bot.wait_until_ready()


@tasks.loop(minutes=2)
async def check_auth_tickets():
    print(f"[AutoDelete] チェック開始: {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
    auth_cat_id = await get_auth_category_id()
    ticket_cat_id = await get_ticket_category_id()
    inquiry_cat_id = await get_config("inquiry_category_id")

    for guild in bot.guilds:
        if auth_cat_id:
            category = guild.get_channel(auth_cat_id)
            if category:
                for channel in list(category.text_channels):
                    if not channel.name.startswith("auth-request-"):
                        continue
                    await _auto_delete_ticket(guild, channel, minutes=5)
        if ticket_cat_id:
            category = guild.get_channel(ticket_cat_id)
            if category:
                for channel in list(category.text_channels):
                    await _auto_delete_ticket(guild, channel, minutes=5)
        if inquiry_cat_id and inquiry_cat_id != ticket_cat_id:
            category = guild.get_channel(inquiry_cat_id)
            if category:
                for channel in list(category.text_channels):
                    await _auto_delete_ticket(guild, channel, minutes=5)


@check_auth_tickets.before_loop
async def before_check_auth_tickets():
    await bot.wait_until_ready()


@check_auth_tickets.error
async def check_auth_tickets_error(error):
    print(f"❌ check_auth_tickets エラー: {error}")
    if not check_auth_tickets.is_running():
        check_auth_tickets.restart()


async def _auto_delete_ticket(guild, channel, minutes: int):
    from datetime import timezone as tz
    try:
        now = datetime.now(tz.utc)
        elapsed = (now - channel.created_at).total_seconds()
        if elapsed < minutes * 60:
            print(f"[AutoDelete] スキップ（時間未達）: #{channel.name} ({int(elapsed)}秒経過 / {minutes*60}秒必要)")
            return
        async for msg in channel.history(limit=None):
            if not msg.author.bot:
                print(f"[AutoDelete] スキップ（人間メッセージあり）: #{channel.name}")
                return
        print(f"[AutoDelete] 削除実行: #{channel.name}")
        await _delete_and_log(guild, channel, minutes)
    except Exception as e:
        print(f"[AutoDelete] エラー: #{channel.name} → {e}")


async def _delete_and_log(guild, channel, minutes: int):
    ch_id = await get_log_channel_id()
    log_channel = guild.get_channel(ch_id) if ch_id else None
    if log_channel:
        embed = discord.Embed(
            title="🗑️ チケット自動削除",
            description=f"チャンネル: `{channel.name}`\n{minutes}分間応答がなかったため自動削除しました。",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        await log_channel.send(embed=embed)
    await channel.delete(reason=f"{minutes}分無応答のため自動削除")


# ===========================
# ===== バックアップ機能 =====
# ===========================

async def create_backup(guild: discord.Guild) -> dict:
    backup = {"guild_name": guild.name, "guild_id": guild.id, "timestamp": datetime.now(timezone.utc).isoformat(), "roles": [], "categories": []}
    for role in guild.roles:
        if role.name == "@everyone":
            continue
        backup["roles"].append({"name": role.name, "color": role.color.value, "hoist": role.hoist, "mentionable": role.mentionable, "permissions": role.permissions.value, "position": role.position})
    for category in guild.categories:
        cat_data = {"name": category.name, "position": category.position, "channels": []}
        for channel in category.channels:
            cat_data["channels"].append({"name": channel.name, "type": str(channel.type), "position": channel.position, "topic": getattr(channel, "topic", None), "nsfw": getattr(channel, "nsfw", False), "slowmode": getattr(channel, "slowmode_delay", 0)})
        backup["categories"].append(cat_data)
    no_category = {"name": "（カテゴリなし）", "position": -1, "channels": []}
    for channel in guild.channels:
        if channel.category is None and not isinstance(channel, discord.CategoryChannel):
            no_category["channels"].append({"name": channel.name, "type": str(channel.type), "position": channel.position, "topic": getattr(channel, "topic", None), "nsfw": getattr(channel, "nsfw", False), "slowmode": getattr(channel, "slowmode_delay", 0)})
    if no_category["channels"]:
        backup["categories"].append(no_category)
    return backup


@tasks.loop(hours=1)
async def auto_backup():
    for guild in bot.guilds:
        data = await create_backup(guild)
        ch_id = await get_backup_channel_id() or await get_mod_log_channel_id()
        log_ch = guild.get_channel(ch_id) if ch_id else None
        if log_ch:
            filename = f"auto-backup-{guild.id}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
            json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            file = discord.File(fp=io.BytesIO(json_bytes), filename=filename)
            embed = discord.Embed(title="💾 自動バックアップ完了", description=f"サーバー: **{guild.name}**\nロール・チャンネル構成を保存しました。", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
            await log_ch.send(embed=embed, file=file)


bot.run(TOKEN)
