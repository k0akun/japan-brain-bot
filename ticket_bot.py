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
CONTENT_SPAM_SECONDS = 5   # 複数アカウントスパム: 何秒以内
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
    """管理者権限 または スタッフロール（ADMIN_ROLE_ID / SUB_ROLE_ID）を持つか確認"""
    member = interaction.user
    if member.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in member.roles}
    return ADMIN_ROLE_ID in role_ids or SUB_ROLE_ID in role_ids

def staff_check():
    """app_commands用スタッフ権限チェックデコレータ"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_staff(interaction):
            await interaction.response.send_message("❌ このコマンドはスタッフのみ使用できます。", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

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
    # 初回：デフォルトワードを登録
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

# ===== 許可するURLドメイン =====
ALLOWED_DOMAINS = [
    "youtube.com",
    "youtu.be",
]

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# スパム検知用メモリ
spam_tracker = defaultdict(list)
same_msg_cache = defaultdict(lambda: {"content": "", "count": 0})
content_spam_cache = []
join_tracker = []

# 禁止ワードはBot起動時にSupabaseから読み込む
BAD_WORDS = []
# 長文スパム除外チャンネル/スレッドIDはBot起動時に読み込む
SPAM_IGNORE_IDS: set = set()


# ===========================
# ===== 追加AutoMod ヘルパー =====
# ===========================

def is_similar(a: str, b: str) -> bool:
    if SequenceMatcher(None, a, b).ratio() >= CONTENT_SPAM_RATIO:
        return True
    if len(a) >= CONTENT_SPAM_PREFIX and len(b) >= CONTENT_SPAM_PREFIX:
        if a[:CONTENT_SPAM_PREFIX] == b[:CONTENT_SPAM_PREFIX]:
            return True
    return False

async def punish_automod(member: discord.Member, guild: discord.Guild, channel: discord.TextChannel, reason: str, detail: str):
    """タイムアウト＋DM通知＋ログ"""
    from datetime import timedelta
    timeout_until = discord.utils.utcnow() + timedelta(minutes=TIMEOUT_MINUTES)
    try:
        await member.timeout(timeout_until, reason=reason)
    except (discord.errors.Forbidden, discord.errors.HTTPException):
        pass
    # DM通知
    try:
        await member.send(
            f"⚠️ **{guild.name}** で自動タイムアウトされました。\n"
            f"理由: {detail}\n"
            f"タイムアウト時間: {TIMEOUT_MINUTES}分"
        )
    except (discord.errors.Forbidden, discord.errors.HTTPException):
        pass

    await log_action(guild, f"🔨 自動タイムアウト {TIMEOUT_MINUTES}分", member, f"{detail} | 実行者: AutoMod")

# ===========================
# ===== AutoMod =====
# ===========================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # スタッフは全てスキップ
    staff_role = message.guild.get_role(ADMIN_ROLE_ID)
    if staff_role in message.author.roles or message.author.guild_permissions.administrator:
        await bot.process_commands(message)
        return

    text = message.content.strip()

    # 長文スパム除外チャンネル/スレッドかチェック
    _channel_id = message.channel.id
    _thread_parent_id = getattr(message.channel, "parent_id", None)
    _is_spam_ignored = _channel_id in SPAM_IGNORE_IDS or (_thread_parent_id and _thread_parent_id in SPAM_IGNORE_IDS)

    # 許可URLのみのメッセージは長文・複数人スパム検知をスキップ
    import re as _re
    _urls = _re.findall(r'https?://([^\s/]+)', text)
    _is_allowed_url_only = bool(_urls) and all(
        any(d.lower().replace("www.", "") == a or d.lower().replace("www.", "").endswith("." + a) for a in ALLOWED_DOMAINS)
        for d in _urls
    )

    # 添付ファイルのみはスキップ
    if text and not _is_allowed_url_only:
        # 長文チェック（除外チャンネルはスキップ）
        if not _is_spam_ignored:
            stripped = text.replace(" ", "").replace("\n", "").replace("\u3000", "")
            if len(stripped) > MAX_MESSAGE_LENGTH or len(text) > MAX_MESSAGE_LENGTH:
                try:
                    await message.channel.purge(limit=10, check=lambda m: m.author.id == message.author.id, bulk=True)
                except Exception:
                    pass
                await punish_automod(message.author, message.guild, message.channel, "長文スパム検知", "長文スパムを検知しました。")
                return

            # 改行スパムチェック
            if text.count("\n") >= MAX_NEWLINES:
                try:
                    await message.channel.purge(limit=10, check=lambda m: m.author.id == message.author.id, bulk=True)
                except Exception:
                    pass
                await punish_automod(message.author, message.guild, message.channel, "改行スパム検知", "改行スパムを検知しました。")
                return

        # 連続同一メッセージチェック（6文字以上のみ）
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

        # 複数アカウントスパム検知
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
                    # 全員分のメッセージを削除
                    try:
                        await message.channel.purge(limit=20, check=lambda msg, u=uid: msg.author.id == u, bulk=True)
                    except Exception:
                        pass
            await log_action(message.guild, "🚨 複数アカウントスパム検知", message.author, f"対象ID: {guilty_ids} | 実行者: AutoMod")
            return

    # URLフィルター
    url_pattern = re.compile(r'https?://([^\s/]+)')
    urls = url_pattern.findall(message.content)
    for domain in urls:
        domain = domain.lower().replace("www.", "")
        if not any(domain == a or domain.endswith("." + a) for a in ALLOWED_DOMAINS):
            await message.delete()
            await message.channel.send(
                f"🔗 {message.author.mention} このリンクは許可されていません。",
                delete_after=5
            )
            await log_action(message.guild, "🔗 不正URLブロック", message.author, f"URL: `{domain}`")
            return

    # 悪言フィルター
    for word in BAD_WORDS:
        if word in message.content:
            await message.delete()
            count = await get_warns(message.author.id) + 1
            await set_warns(message.author.id, count)
            await message.channel.send(
                f"⚠️ {message.author.mention} 禁止ワードが含まれています。(警告 {count}回目)",
                delete_after=5
            )
            await log_action(message.guild, f"🚫 禁止ワード検知", message.author, f"内容: ||{message.content}|| | 警告{count}回目")
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
    """警告回数に応じて自動処罰"""
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
# ===== レイド検知 =====
# ===========================

@bot.event
async def on_member_join(member: discord.Member):
    now = datetime.now(timezone.utc).timestamp()
    join_tracker.append(now)

    # 古いエントリを削除
    join_tracker[:] = [t for t in join_tracker if now - t < RAID_JOIN_INTERVAL]

    # 新規アカウント検知
    account_age = (datetime.now(timezone.utc) - member.created_at).days
    if account_age < RAID_ACCOUNT_AGE_DAYS:
        await log_action(
            member.guild,
            "🆕 新規アカウント参加",
            member,
            f"アカウント作成から {account_age} 日 | 要注意ユーザーの可能性があります"
        )

    # レイド検知
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
# ===== 警告システム =====
# ===========================

@bot.tree.command(name="warn", description="ユーザーに警告を出します（スタッフのみ）")
@app_commands.describe(member="警告するユーザー", reason="理由")
@staff_check()
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "理由なし"):
    count = await get_warns(member.id) + 1
    await set_warns(member.id, count)
    await interaction.response.send_message(f"⚠️ {member.mention} に警告を出しました。({count}回目)\n理由: {reason}")
    await log_action(interaction.guild, "⚠️ 警告", member, f"理由: {reason} | 合計{count}回 | 実行者: {interaction.user}")
    await auto_punish(member, interaction.guild, count)


@bot.tree.command(name="warnlist", description="全員の警告数一覧を表示します（スタッフのみ）")
@staff_check()
async def warnlist(interaction: discord.Interaction):
    rows = await sb_get("warns", "count=gt.0&order=count.desc")

    if not rows:
        await interaction.response.send_message("📋 警告のあるユーザーはいません。", ephemeral=True)
        return

    embed = discord.Embed(
        title="⚠️ 警告数一覧",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )

    lines = []
    for row in rows:
        user_id = row["user_id"]
        count = row["count"]
        member = interaction.guild.get_member(int(user_id))
        name = member.mention if member else f"ID: {user_id}（退出済み）"
        lines.append(f"{name} → **{count}回**")

    # 25件ずつ分割（embedの文字数制限対策）
    chunk = "\n".join(lines[:25])
    embed.description = chunk
    if len(lines) > 25:
        embed.set_footer(text=f"他 {len(lines) - 25} 人")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="warns", description="ユーザーの警告数を確認します")
@app_commands.describe(member="確認するユーザー")
@staff_check()
async def warns(interaction: discord.Interaction, member: discord.Member):
    count = await get_warns(member.id)
    await interaction.response.send_message(f"📋 {member.mention} の警告数: **{count}回**", ephemeral=True)


@bot.tree.command(name="clearwarn", description="ユーザーの警告を減らします（スタッフのみ）省略時はリセット")
@app_commands.describe(member="対象ユーザー", count="減らす回数（省略時はリセット）")
@staff_check()
async def clearwarn(interaction: discord.Interaction, member: discord.Member, count: int = None):
    current = await get_warns(member.id)
    if count is None:
        # 省略時：完全リセット
        await reset_warns(member.id)
        await interaction.response.send_message(
            f"✅ {member.mention} の警告をリセットしました。({current}回 → 0回)",
            ephemeral=True
        )
        await log_action(interaction.guild, "🔄 警告リセット", member,
                         f"実行者: {interaction.user} | {current}回 → 0回")
    else:
        if count <= 0:
            await interaction.response.send_message("❌ 1以上の回数を指定してください。", ephemeral=True)
            return
        new_count = max(current - count, 0)
        await set_warns(member.id, new_count)
        await interaction.response.send_message(
            f"✅ {member.mention} の警告を {count}回 減らしました。({current}回 → {new_count}回)",
            ephemeral=True
        )
        await log_action(interaction.guild, "🔽 警告減算", member,
                         f"実行者: {interaction.user} | {current}回 → {new_count}回（{count}回減算）")


@bot.tree.command(name="kick", description="ユーザーをキックします（スタッフのみ）")
@app_commands.describe(member="キックするユーザー", reason="理由")
@staff_check()
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "理由なし"):
    await member.kick(reason=reason)
    await interaction.response.send_message(f"👢 {member.mention} をキックしました。\n理由: {reason}")
    await log_action(interaction.guild, "👢 キック", member, f"理由: {reason} | 実行者: {interaction.user}")


@bot.tree.command(name="ban", description="ユーザーをBANします（スタッフのみ）")
@app_commands.describe(member="BANするユーザー", reason="理由")
@staff_check()
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "理由なし"):
    await member.ban(reason=reason)
    await interaction.response.send_message(f"🔨 {member.mention} をBANしました。\n理由: {reason}")
    await log_action(interaction.guild, "🔨 BAN", member, f"理由: {reason} | 実行者: {interaction.user}")


@bot.tree.command(name="unban", description="ユーザーのBANを解除します（スタッフのみ）")
@app_commands.describe(user_id="解除するユーザーのID")
@staff_check()
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        await interaction.response.send_message(f"✅ {user} のBANを解除しました。")
        await log_action(interaction.guild, "✅ BAN解除", user, f"実行者: {interaction.user}")
    except Exception:
        await interaction.response.send_message("❌ ユーザーが見つからないか、BANされていません。", ephemeral=True)


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

    embed = discord.Embed(
        title="🔨 BAN一覧",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"合計: {len(bans)} 人")

    lines = []
    for entry in bans[:25]:
        reason = entry.reason or "理由なし"
        lines.append(f"**{entry.user}** (`{entry.user.id}`) - {reason}")

    embed.description = "\n".join(lines)
    if len(bans) > 25:
        embed.add_field(name="⚠️", value=f"他 {len(bans) - 25} 人（最初の25人を表示）", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="timeout", description="ユーザーをタイムアウトします（スタッフのみ）")
@app_commands.describe(member="対象ユーザー", minutes="タイムアウト時間（分）", reason="理由")
@staff_check()
async def timeout_cmd(interaction: discord.Interaction, member: discord.Member, minutes: int = 10, reason: str = "理由なし"):
    until = discord.utils.utcnow() + __import__("datetime").timedelta(minutes=minutes)
    await member.timeout(until, reason=reason)
    await interaction.response.send_message(f"⏱️ {member.mention} を{minutes}分タイムアウトしました。\n理由: {reason}")
    await log_action(interaction.guild, f"⏱️ タイムアウト {minutes}分", member, f"理由: {reason} | 実行者: {interaction.user}")


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
# ===== 禁止ワード管理コマンド =====
# ===========================

@bot.tree.command(name="url-add", description="許可するURLドメインを追加します（スタッフのみ）")
@app_commands.describe(domain="追加するドメイン（例: twitter.com）")
@staff_check()
async def url_add(interaction: discord.Interaction, domain: str):
    domain = domain.lower().replace("www.", "").strip()
    if domain in ALLOWED_DOMAINS:
        await interaction.response.send_message(f"⚠️ `{domain}` はすでに許可されています。", ephemeral=True)
        return
    ALLOWED_DOMAINS.append(domain)
    await interaction.response.send_message(f"✅ `{domain}` を許可リストに追加しました。", ephemeral=True)
    await log_action(interaction.guild, "🔗 許可URL追加", interaction.user, f"ドメイン: `{domain}`")


@bot.tree.command(name="url-remove", description="許可するURLドメインを削除します（スタッフのみ）")
@app_commands.describe(domain="削除するドメイン（例: twitter.com）")
@staff_check()
async def url_remove(interaction: discord.Interaction, domain: str):
    domain = domain.lower().replace("www.", "").strip()
    if domain not in ALLOWED_DOMAINS:
        await interaction.response.send_message(f"❌ `{domain}` は登録されていません。", ephemeral=True)
        return
    ALLOWED_DOMAINS.remove(domain)
    await interaction.response.send_message(f"✅ `{domain}` を許可リストから削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ 許可URL削除", interaction.user, f"ドメイン: `{domain}`")


@bot.tree.command(name="url-list", description="許可されているURLドメイン一覧を表示します（スタッフのみ）")
@staff_check()
async def url_list(interaction: discord.Interaction):
    if not ALLOWED_DOMAINS:
        await interaction.response.send_message("📋 許可されているドメインはありません。", ephemeral=True)
        return
    domain_list = "\n".join([f"・{d}" for d in ALLOWED_DOMAINS])
    embed = discord.Embed(
        title="🔗 許可URLドメイン一覧",
        description=domain_list,
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="badword-add", description="禁止ワードを追加します（スタッフのみ）")
@app_commands.describe(word="追加する禁止ワード")
@staff_check()
async def badword_add(interaction: discord.Interaction, word: str):
    if word in BAD_WORDS:
        await interaction.response.send_message(f"⚠️ `{word}` はすでに登録されています。", ephemeral=True)
        return
    await add_bad_word_db(word)
    BAD_WORDS.append(word)
    await interaction.response.send_message(f"✅ `{word}` を禁止ワードに追加しました。", ephemeral=True)
    await log_action(interaction.guild, "🚫 禁止ワード追加", interaction.user, f"追加ワード: `{word}`")


@bot.tree.command(name="badword-remove", description="禁止ワードを削除します（スタッフのみ）")
@app_commands.describe(word="削除する禁止ワード")
@staff_check()
async def badword_remove(interaction: discord.Interaction, word: str):
    if word not in BAD_WORDS:
        await interaction.response.send_message(f"❌ `{word}` は登録されていません。", ephemeral=True)
        return
    await remove_bad_word_db(word)
    BAD_WORDS.remove(word)
    await interaction.response.send_message(f"✅ `{word}` を禁止ワードから削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ 禁止ワード削除", interaction.user, f"削除ワード: `{word}`")


@bot.tree.command(name="badword-list", description="禁止ワード一覧を表示します（全員閲覧可能）")
async def badword_list(interaction: discord.Interaction):
    if not BAD_WORDS:
        await interaction.response.send_message("📋 禁止ワードは登録されていません。")
        return
    word_list = "\n".join([f"・{w}" for w in BAD_WORDS])
    embed = discord.Embed(
        title="🚫 禁止ワード一覧",
        description=word_list,
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed)


# ===========================
# ===== 長文スパム除外チャンネル管理 =====
# ===========================

@bot.tree.command(name="spam-ignore-add", description="長文スパム検知を無効にするチャンネル/スレッドを追加します（スタッフのみ）")
@app_commands.describe(channel="除外するチャンネルまたはスレッド")
@staff_check()
async def spam_ignore_add(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    global SPAM_IGNORE_IDS
    if channel.id in SPAM_IGNORE_IDS:
        await interaction.response.send_message(f"⚠️ {channel.mention} はすでに除外リストに登録されています。", ephemeral=True)
        return
    await add_spam_ignore_id(channel.id)
    SPAM_IGNORE_IDS.add(channel.id)
    await interaction.response.send_message(f"✅ {channel.mention} を長文スパム検知の除外リストに追加しました。", ephemeral=True)
    await log_action(interaction.guild, "📋 スパム除外追加", interaction.user, f"チャンネル: {channel.mention} (`{channel.id}`)")


@bot.tree.command(name="spam-ignore-remove", description="長文スパム検知の除外リストからチャンネル/スレッドを削除します（スタッフのみ）")
@app_commands.describe(channel="除外リストから外すチャンネルまたはスレッド")
@staff_check()
async def spam_ignore_remove(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    global SPAM_IGNORE_IDS
    if channel.id not in SPAM_IGNORE_IDS:
        await interaction.response.send_message(f"❌ {channel.mention} は除外リストに登録されていません。", ephemeral=True)
        return
    await remove_spam_ignore_id(channel.id)
    SPAM_IGNORE_IDS.discard(channel.id)
    await interaction.response.send_message(f"✅ {channel.mention} を除外リストから削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ スパム除外削除", interaction.user, f"チャンネル: {channel.mention} (`{channel.id}`)")


@bot.tree.command(name="spam-ignore-list", description="長文スパム検知の除外リストを表示します（スタッフのみ）")
@staff_check()
async def spam_ignore_list(interaction: discord.Interaction):
    if not SPAM_IGNORE_IDS:
        await interaction.response.send_message("📋 除外リストにチャンネルはありません。", ephemeral=True)
        return
    lines = []
    for cid in SPAM_IGNORE_IDS:
        ch = interaction.guild.get_channel(cid)
        if ch:
            lines.append(f"・{ch.mention} (`{cid}`)")
        else:
            lines.append(f"・不明なチャンネル (`{cid}`)")
    embed = discord.Embed(
        title="📋 長文スパム検知 除外リスト",
        description="\n".join(lines),
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ===========================
# ===== セットアップコマンド =====
# ===========================

@bot.tree.command(name="setup", description="ボットの設定を一括で行います（管理者のみ）")
@app_commands.describe(
    auth_category="認証チケット用カテゴリ",
    ticket_log="チケットログチャンネル",
    mod_log="モデレーションログチャンネル",
    backup_channel="自動バックアップ送信チャンネル"
)
@staff_check()
async def setup(
    interaction: discord.Interaction,
    auth_category: discord.CategoryChannel = None,
    ticket_log: discord.TextChannel = None,
    mod_log: discord.TextChannel = None,
    backup_channel: discord.TextChannel = None
):
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
        await interaction.response.send_message(embed=embed, ephemeral=True)
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
        # 設定した瞬間に即バックアップを送信
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
    await interaction.response.send_message(embed=embed, ephemeral=True)


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

        async for msg in channel.history(limit=500, oldest_first=True):
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            log_text += f"[{timestamp}] {msg.author}: {msg.content}\n"
            for attachment in msg.attachments:
                log_text += f"  [添付ファイル: {attachment.url}]\n"

        log_ch_id = await get_log_channel_id()
        log_channel = interaction.guild.get_channel(log_ch_id) if log_ch_id else None
        if log_channel:
            import io
            log_file = discord.File(fp=io.StringIO(log_text), filename=f"{channel.name}-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt")
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
        mention_text = member.mention  # 認証チケットはスタッフメンションなし
    else:
        mention_text = member.mention
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
    await channel.send(content=mention_text, embed=embed, view=TicketView())
    await interaction.response.send_message(f"✅ チケットを作成しました: {channel.mention}", ephemeral=True)


@bot.tree.command(name="auth-panel", description="認証リクエストパネルを送信します（管理者のみ）")
@app_commands.describe(category="認証チケット用カテゴリ（省略で現在の設定を使用）")
@staff_check()
async def send_auth_panel(interaction: discord.Interaction, category: discord.CategoryChannel = None):
    if category:
        await set_config("auth_category_id", category.id)
    embed = discord.Embed(
        title="🔑 認証リクエスト",
        description="認証ができない方はボタンを押してリクエストを送ってください。\n\n🔑 **認証リクエスト** — 認証ができない方向けのサポート",
        color=discord.Color.gold()
    )
    await interaction.channel.send(embed=embed, view=AuthPanelView())
    await interaction.response.send_message("✅ 認証リクエストパネルを送信しました。", ephemeral=True)


@bot.tree.command(name="ticket-panel", description="サポート＆お問い合わせパネルを送信します（管理者のみ）")
@app_commands.describe(
    support_category="サポート・質問チケット用カテゴリ",
    inquiry_category="お問い合わせチケット用カテゴリ（省略するとサポートカテゴリと同じになります）"
)
@staff_check()
async def send_panel(interaction: discord.Interaction, support_category: discord.CategoryChannel = None, inquiry_category: discord.CategoryChannel = None):
    if support_category:
        await set_config("ticket_category_id", support_category.id)
    if inquiry_category:
        await set_config("inquiry_category_id", inquiry_category.id)
    elif support_category:
        # inquiry_categoryが省略された場合、support_categoryと同じにはせず警告を出す
        pass

    # 現在の設定を確認して表示
    s_cat_id = await get_ticket_category_id()
    i_cat_id = await get_config("inquiry_category_id")
    s_cat = interaction.guild.get_channel(s_cat_id) if s_cat_id else None
    i_cat = interaction.guild.get_channel(i_cat_id) if i_cat_id else None

    embed = discord.Embed(
        title="🎫 サポートチケット",
        description=(
            "❓ **サポート・質問** — サーバーに関する質問・サポート\n"
            "📩 **お問い合わせ** — その他のお問い合わせ"
        ),
        color=discord.Color.blurple()
    )
    await interaction.channel.send(embed=embed, view=TicketPanelView())

    info = []
    if s_cat:
        info.append(f"✅ サポートカテゴリ: **{s_cat.name}**")
    else:
        info.append("⚠️ サポートカテゴリ: **未設定**")
    if i_cat:
        info.append(f"✅ お問い合わせカテゴリ: **{i_cat.name}**")
    else:
        info.append("⚠️ お問い合わせカテゴリ: **未設定**（`/ticket-panel inquiry_category:カテゴリ名` で設定してください）")

    await interaction.response.send_message("✅ パネルを送信しました。\n" + "\n".join(info), ephemeral=True)


@bot.tree.command(name="inquiry-panel", description="お問い合わせパネルを送信します（管理者のみ）")
@app_commands.describe(category="お問い合わせチケット用カテゴリ（省略で現在の設定を使用）")
@staff_check()
async def send_inquiry_panel(interaction: discord.Interaction, category: discord.CategoryChannel = None):
    if category:
        await set_config("inquiry_category_id", category.id)
    embed = discord.Embed(title="📩 お問い合わせ", description="📩 **お問い合わせ** — その他のお問い合わせはこちら", color=discord.Color.blurple())
    await interaction.channel.send(embed=embed, view=InquiryPanelView())
    await interaction.response.send_message("✅ お問い合わせパネルを送信しました。", ephemeral=True)


@bot.tree.command(name="botstatus", description="Botの現在の設定を全員に表示します")
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
    embed = discord.Embed(title="🤖 Bot設定状況", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="🎫 サポートカテゴリ", value=category.name if category else "❌ 未設定", inline=True)
    embed.add_field(name="📩 お問い合わせカテゴリ", value=inq_cat.name if inq_cat else "❌ 未設定", inline=True)
    embed.add_field(name="🔑 認証カテゴリ", value=auth_cat.name if auth_cat else "❌ 未設定", inline=True)
    embed.add_field(name="📋 チケットログ", value=log_ch.mention if log_ch else "❌ 未設定", inline=True)
    embed.add_field(name="🔨 モデレーションログ", value=mod_log_ch.mention if mod_log_ch else "チケットログと共用", inline=True)
    embed.add_field(name="🛡️ AutoMod設定", value=f"長文: **{MAX_MESSAGE_LENGTH}文字**以上\n改行: **{MAX_NEWLINES}回**以上\n連続スパム: **{SPAM_COUNT}回**\n複数垢スパム: **{CONTENT_SPAM_SECONDS}秒**以内に**{CONTENT_SPAM_USERS}人**\n自動TO: **{TIMEOUT_MINUTES}分**", inline=False)
    domain_list = "\n".join([f"・{d}" for d in ALLOWED_DOMAINS]) if ALLOWED_DOMAINS else "なし"
    embed.add_field(name="🔗 許可URL", value=domain_list, inline=False)
    word_list = "　".join([f"`{w}`" for w in BAD_WORDS]) if BAD_WORDS else "なし"
    embed.add_field(name="🚫 禁止ワード", value=word_list, inline=False)
    embed.add_field(name="⚠️ 警告", value="3回→5分TO / 5回→30分TO / 7回以上→1時間TO", inline=False)
    await interaction.followup.send(embed=embed)



# ===========================
# ===========================
# ===== バックアップ・復元 =====
# ===========================

@bot.tree.command(name="backup", description="サーバーのチャンネル構成とロールをバックアップします（管理者のみ）")
@staff_check()
async def backup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # ロール保存
    roles = []
    for role in guild.roles:
        if role.is_default():
            continue
        roles.append({
            "name": role.name,
            "color": role.color.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "permissions": role.permissions.value,
            "position": role.position,
        })

    # カテゴリ・チャンネル保存
    categories = []
    no_category_channels = []

    for category in guild.categories:
        channels = []
        for ch in category.channels:
            channels.append({
                "name": ch.name,
                "type": str(ch.type),
                "position": ch.position,
                "topic": getattr(ch, "topic", None),
                "nsfw": getattr(ch, "nsfw", False),
                "slowmode_delay": getattr(ch, "slowmode_delay", 0),
            })
        categories.append({
            "name": category.name,
            "position": category.position,
            "channels": channels,
        })

    for ch in guild.channels:
        if ch.category is None and not isinstance(ch, discord.CategoryChannel):
            no_category_channels.append({
                "name": ch.name,
                "type": str(ch.type),
                "position": ch.position,
            })

    backup_data = {
        "guild_name": guild.name,
        "backup_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "roles": sorted(roles, key=lambda r: r["position"]),
        "categories": sorted(categories, key=lambda c: c["position"]),
        "no_category_channels": no_category_channels,
    }

    # JSONファイルとして保存
    filename = f"backup-{guild.id}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    json_bytes = json.dumps(backup_data, ensure_ascii=False, indent=2).encode("utf-8")
    file = discord.File(fp=io.BytesIO(json_bytes), filename=filename)

    embed = discord.Embed(
        title="✅ バックアップ完了",
        description=(
            f"**サーバー:** {guild.name}\n"
            f"**ロール数:** {len(roles)}\n"
            f"**カテゴリ数:** {len(categories)}\n"
            f"**日時:** {backup_data['backup_at']}\n\n"
            f"⚠️ このファイルを大切に保管してください。\n"
            f"復元するには `/restore` でこのファイルを添付してください。"
        ),
        color=discord.Color.green()
    )
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)


@bot.tree.command(name="restore", description="バックアップからチャンネル構成とロールを復元します（管理者のみ）")
@app_commands.describe(file="backupコマンドで生成したJSONファイル")
@staff_check()
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

    # ロール復元
    existing_role_names = {r.name for r in guild.roles}
    created_roles = 0
    for role_data in sorted(data.get("roles", []), key=lambda r: r["position"]):
        if role_data["name"] in existing_role_names:
            continue
        try:
            await guild.create_role(
                name=role_data["name"],
                color=discord.Color(role_data["color"]),
                hoist=role_data["hoist"],
                mentionable=role_data["mentionable"],
                permissions=discord.Permissions(role_data["permissions"]),
            )
            created_roles += 1
            await asyncio.sleep(0.5)
        except Exception:
            pass
    results.append(f"✅ ロール: {created_roles}個作成")

    # カテゴリ・チャンネル復元
    existing_channel_names = {c.name for c in guild.channels}
    created_categories = 0
    created_channels = 0

    for cat_data in sorted(data.get("categories", []), key=lambda c: c["position"]):
        # カテゴリ作成
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

        # チャンネル作成
        for ch_data in sorted(cat_data.get("channels", []), key=lambda c: c["position"]):
            if ch_data["name"] in existing_channel_names:
                continue
            try:
                if ch_data["type"] == "text":
                    await guild.create_text_channel(
                        name=ch_data["name"],
                        category=category,
                        topic=ch_data.get("topic"),
                        nsfw=ch_data.get("nsfw", False),
                        slowmode_delay=ch_data.get("slowmode_delay", 0),
                    )
                elif ch_data["type"] == "voice":
                    await guild.create_voice_channel(
                        name=ch_data["name"],
                        category=category,
                    )
                created_channels += 1
                await asyncio.sleep(0.5)
            except Exception:
                pass

    results.append(f"✅ カテゴリ: {created_categories}個作成")
    results.append(f"✅ チャンネル: {created_channels}個作成")
    results.append(f"⚠️ すでに存在するロール・チャンネルはスキップしました")
    results.append(f"⚠️ メッセージ履歴は復元できません")

    embed = discord.Embed(
        title="✅ 復元完了",
        description="\n".join(results),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ===== Bot起動時 =====
# ===========================

# ===========================
# ===== 認証チケット自動削除 =====
# ===========================

TICKET_TIMEOUT_MINUTES = 5   # 通常チケット: 5分
AUTH_TICKET_TIMEOUT_HOURS = 0.083  # 認証チケット: 5分（=5/60時間）

@bot.event
async def on_ready():
    global BAD_WORDS, SPAM_IGNORE_IDS
    BAD_WORDS = await load_bad_words_db()
    SPAM_IGNORE_IDS = set(await get_spam_ignore_ids())
    check_auth_tickets.start()
    auto_backup.start()
    bot.add_view(TicketPanelView())
    bot.add_view(InquiryPanelView())
    bot.add_view(AuthPanelView())
    bot.add_view(TicketView())
    try:
        synced = await bot.tree.sync()
        print(f"✅ スラッシュコマンドを同期しました ({len(synced)}個)")
    except Exception as e:
        print(f"❌ 同期エラー: {e}")
    print(f"✅ {bot.user} としてログインしました")


@tasks.loop(minutes=2)
async def check_auth_tickets():
    auth_cat_id = await get_auth_category_id()
    ticket_cat_id = await get_ticket_category_id()
    inquiry_cat_id = await get_config("inquiry_category_id")

    for guild in bot.guilds:
        # 認証チケット（5分・人間のメッセージがなければ削除）
        if auth_cat_id:
            category = guild.get_channel(auth_cat_id)
            if category:
                for channel in list(category.text_channels):
                    if not channel.name.startswith("auth-request-"):
                        continue
                    has_human_msg = False
                    async for msg in channel.history(limit=None):
                        if not msg.author.bot:
                            has_human_msg = True
                            break
                    if has_human_msg:
                        continue
                    await _auto_delete_ticket(guild, channel, minutes=5)

        # サポート・質問チケット（5分）
        if ticket_cat_id:
            category = guild.get_channel(ticket_cat_id)
            if category:
                for channel in list(category.text_channels):
                    has_human_msg = False
                    async for msg in channel.history(limit=None):
                        if not msg.author.bot:
                            has_human_msg = True
                            break
                    if has_human_msg:
                        continue
                    await _auto_delete_ticket(guild, channel, minutes=5)

        # お問い合わせチケット（5分）※サポートと別カテゴリの場合のみ対象
        if inquiry_cat_id and inquiry_cat_id != ticket_cat_id:
            category = guild.get_channel(inquiry_cat_id)
            if category:
                for channel in list(category.text_channels):
                    has_human_msg = False
                    async for msg in channel.history(limit=None):
                        if not msg.author.bot:
                            has_human_msg = True
                            break
                    if has_human_msg:
                        continue
                    await _auto_delete_ticket(guild, channel, minutes=5)


async def _auto_delete_ticket(guild, channel, minutes: int):
    """Botのメッセージのみ かつ チャンネル作成から指定分数経過した場合のみ削除"""
    from datetime import timezone as tz
    try:
        now = datetime.now(tz.utc)
        # チャンネル作成から指定分数経過していなければスキップ
        if (now - channel.created_at).total_seconds() < minutes * 60:
            return

        # 全履歴をチェック（人間のメッセージが1件でもあれば絶対に削除しない）
        async for msg in channel.history(limit=None):
            if not msg.author.bot:
                return  # 人間のメッセージあり → 削除しない

        # Bot発言のみ & 時間経過 → 削除
        await _delete_and_log(guild, channel, minutes)
    except Exception:
        pass


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

BACKUP_FILE = "backup.json"
BACKUP_INTERVAL_HOURS = 24  # 自動バックアップの間隔（時間）

def save_backup(data: dict):
    with open(BACKUP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def create_backup(guild: discord.Guild) -> dict:
    """サーバーのチャンネル構成とロールをバックアップ"""
    backup = {
        "guild_name": guild.name,
        "guild_id": guild.id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "roles": [],
        "categories": [],
    }

    # ロール一覧（@everyoneを除く）
    for role in guild.roles:
        if role.name == "@everyone":
            continue
        backup["roles"].append({
            "name": role.name,
            "color": role.color.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "permissions": role.permissions.value,
            "position": role.position,
        })

    # カテゴリ＆チャンネル構成
    for category in guild.categories:
        cat_data = {
            "name": category.name,
            "position": category.position,
            "channels": []
        }
        for channel in category.channels:
            cat_data["channels"].append({
                "name": channel.name,
                "type": str(channel.type),
                "position": channel.position,
                "topic": getattr(channel, "topic", None),
                "nsfw": getattr(channel, "nsfw", False),
                "slowmode": getattr(channel, "slowmode_delay", 0),
            })
        backup["categories"].append(cat_data)

    # カテゴリなしチャンネル
    no_category = {
        "name": "（カテゴリなし）",
        "position": -1,
        "channels": []
    }
    for channel in guild.channels:
        if channel.category is None and not isinstance(channel, discord.CategoryChannel):
            no_category["channels"].append({
                "name": channel.name,
                "type": str(channel.type),
                "position": channel.position,
                "topic": getattr(channel, "topic", None),
                "nsfw": getattr(channel, "nsfw", False),
                "slowmode": getattr(channel, "slowmode_delay", 0),
            })
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
            embed = discord.Embed(
                title="💾 自動バックアップ完了",
                description=f"サーバー: **{guild.name}**\nロール・チャンネル構成を保存しました。",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            await log_ch.send(embed=embed, file=file)

bot.run(TOKEN)
