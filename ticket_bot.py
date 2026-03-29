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
ADMIN_ROLE_ID = 1486603522748317737

SPAM_LIMIT = 5
SPAM_INTERVAL = 5
RAID_ACCOUNT_AGE_DAYS = 7
RAID_JOIN_LIMIT = 5
RAID_JOIN_INTERVAL = 10
# 追加AutoMod設定
MAX_MESSAGE_LENGTH = 140   # 長文検知: 文字数上限
MAX_NEWLINES = 10          # 改行スパム: 改行数上限
TIMEOUT_MINUTES = 5        # 自動タイムアウト時間（分）
SPAM_COUNT = 3             # 連続同一メッセージ: 何回でタイムアウト
CONTENT_SPAM_USERS = 4    # 複数アカウントスパム: 何人でタイムアウト
CONTENT_SPAM_SECONDS = 10  # 複数アカウントスパム: 何秒以内
CONTENT_SPAM_RATIO = 0.80  # 複数アカウントスパム: 類似度しきい値
CONTENT_SPAM_PREFIX = 8    # 複数アカウントスパム: 前半一致文字数
# ================

# ===== サーバー設定をJSONで管理 =====
CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "ticket_category_id": None,
        "auth_category_id": None,
        "log_channel_id": None,
        "mod_log_channel_id": None,
    }

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()

def get_ticket_category_id():
    return config.get("ticket_category_id")

def get_auth_category_id():
    return config.get("auth_category_id")

def get_log_channel_id():
    return config.get("log_channel_id")

def get_mod_log_channel_id():
    # 未設定の場合はチケットログチャンネルにフォールバック
    return config.get("mod_log_channel_id") or config.get("log_channel_id")

# ===== 禁止ワードをJSONで管理 =====
BAD_WORDS_FILE = "bad_words.json"

def load_bad_words():
    if os.path.exists(BAD_WORDS_FILE):
        with open(BAD_WORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return ["死ね", "殺す", "アホ", "シコシコ", "クンニ", "障害者", "しこしこ", "うんち", "うんこ", "だまれ", "sex"]

def save_bad_words(words):
    with open(BAD_WORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=2)

BAD_WORDS = load_bad_words()

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
# 追加AutoMod用キャッシュ
same_msg_cache = defaultdict(lambda: {"content": "", "count": 0})
content_spam_cache = []  # [(user_id, text, timestamp)]

# レイド検知用メモリ
join_tracker = []

# 警告数をJSONで永続化
WARN_FILE = "warns.json"

def load_warns():
    if os.path.exists(WARN_FILE):
        with open(WARN_FILE, "r", encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}

def save_warns():
    with open(WARN_FILE, "w", encoding="utf-8") as f:
        json.dump(warn_tracker, f, ensure_ascii=False, indent=2)

warn_tracker = load_warns()


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

    await log_action(guild, f"🔨 自動タイムアウト {TIMEOUT_MINUTES}分", member, detail)

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

    # 許可URLのみのメッセージは長文・複数人スパム検知をスキップ
    import re as _re
    _urls = _re.findall(r'https?://([^\s/]+)', text)
    _is_allowed_url_only = bool(_urls) and all(
        any(d.lower().replace("www.", "") == a or d.lower().replace("www.", "").endswith("." + a) for a in ALLOWED_DOMAINS)
        for d in _urls
    )

    # 添付ファイルのみはスキップ
    if text and not _is_allowed_url_only:
        # 長文チェック
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
            for uid in guilty_ids:
                m = message.guild.get_member(uid)
                if m and not m.guild_permissions.administrator:
                    from datetime import timedelta
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
                await message.channel.purge(limit=10, check=lambda m: m.author.id == message.author.id, bulk=True)
            except Exception:
                pass
            await log_action(message.guild, "🚨 複数アカウントスパム検知", message.author, f"対象ID: {guilty_ids}")
            return

    # URLフィルター（http/https + discord.gg + 埋め込みリンク）
    url_pattern = re.compile(r'https?://([^\s/]+)')
    invite_pattern = re.compile(r'discord\.gg/[^\s]+|discord\.com/invite/[^\s]+', re.IGNORECASE)
    markdown_url_pattern = re.compile(r'\[.+?\]\(https?://([^\s/\)]+)')

    urls = url_pattern.findall(message.content)
    markdown_urls = markdown_url_pattern.findall(message.content)
    has_invite = bool(invite_pattern.search(message.content))

    blocked = False
    blocked_domain = ""

    # 通常URL
    for domain in urls + markdown_urls:
        domain = domain.lower().replace("www.", "")
        if not any(domain == a or domain.endswith("." + a) for a in ALLOWED_DOMAINS):
            blocked = True
            blocked_domain = domain
            break

    # Discord招待リンク
    if has_invite:
        blocked = True
        blocked_domain = "discord.gg"

    if blocked:
        try:
            await message.delete()
        except Exception:
            pass
        await log_action(message.guild, "🔗 不正URLブロック", message.author, f"URL: `{blocked_domain}`")
        return

    # 悪言フィルター
    for word in BAD_WORDS:
        if word in message.content:
            await message.delete()
            warn_tracker[message.author.id] = warn_tracker.get(message.author.id, 0) + 1
            count = warn_tracker[message.author.id]
            save_warns()
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
        warn_tracker[user_id] = warn_tracker.get(user_id, 0) + 1
        count = warn_tracker[user_id]
        save_warns()
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
    if count == 3:
        await member.timeout(discord.utils.utcnow() + __import__("datetime").timedelta(minutes=10), reason="警告3回（AutoMod）")
        await log_action(guild, "⏱️ タイムアウト10分", member, "警告3回に達したため")


# ===========================
# ===== メッセージ編集検知 =====
# ===========================

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author.bot:
        return
    if not after.guild:
        return

    staff_role = after.guild.get_role(ADMIN_ROLE_ID)
    if staff_role in after.author.roles or after.author.guild_permissions.administrator:
        return

    text = after.content.strip()
    url_pattern = re.compile(r'https?://([^\s/]+)')
    invite_pattern = re.compile(r'discord\.gg/[^\s]+|discord\.com/invite/[^\s]+', re.IGNORECASE)
    markdown_url_pattern = re.compile(r'\[.+?\]\(https?://([^\s/\)]+)')

    urls = url_pattern.findall(text)
    markdown_urls = markdown_url_pattern.findall(text)
    has_invite = bool(invite_pattern.search(text))

    blocked = False
    blocked_domain = ""

    for domain in urls + markdown_urls:
        domain = domain.lower().replace("www.", "")
        if not any(domain == a or domain.endswith("." + a) for a in ALLOWED_DOMAINS):
            blocked = True
            blocked_domain = domain
            break

    if has_invite:
        blocked = True
        blocked_domain = "discord.gg"

    if blocked:
        try:
            await after.delete()
        except Exception:
            pass
        await log_action(after.guild, "🔗 編集による不正URLブロック", after.author, f"URL: `{blocked_domain}`")

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
        log_channel = member.guild.get_channel(get_log_channel_id())
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
@app_commands.checks.has_permissions(administrator=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "理由なし"):
    warn_tracker[member.id] = warn_tracker.get(member.id, 0) + 1
    count = warn_tracker[member.id]
    save_warns()
    await interaction.response.send_message(f"⚠️ {member.mention} に警告を出しました。({count}回目)\n理由: {reason}")
    await log_action(interaction.guild, "⚠️ 警告", member, f"理由: {reason} | 合計{count}回")
    await auto_punish(member, interaction.guild, count)


@bot.tree.command(name="warns", description="ユーザーの警告数を確認します")
@app_commands.describe(member="確認するユーザー")
@app_commands.checks.has_permissions(administrator=True)
async def warns(interaction: discord.Interaction, member: discord.Member):
    count = warn_tracker.get(member.id, 0)
    await interaction.response.send_message(f"📋 {member.mention} の警告数: **{count}回**", ephemeral=True)


@bot.tree.command(name="clearwarn", description="ユーザーの警告をリセットします（スタッフのみ）")
@app_commands.describe(member="リセットするユーザー")
@app_commands.checks.has_permissions(administrator=True)
async def clearwarn(interaction: discord.Interaction, member: discord.Member):
    warn_tracker[member.id] = 0
    save_warns()
    await interaction.response.send_message(f"✅ {member.mention} の警告をリセットしました。", ephemeral=True)
    await log_action(interaction.guild, "🔄 警告リセット", member, f"実行者: {interaction.user}")


@bot.tree.command(name="kick", description="ユーザーをキックします（スタッフのみ）")
@app_commands.describe(member="キックするユーザー", reason="理由")
@app_commands.checks.has_permissions(administrator=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "理由なし"):
    await member.kick(reason=reason)
    await interaction.response.send_message(f"👢 {member.mention} をキックしました。\n理由: {reason}")
    await log_action(interaction.guild, "👢 キック", member, f"理由: {reason} | 実行者: {interaction.user}")


@bot.tree.command(name="ban", description="ユーザーをBANします（スタッフのみ）")
@app_commands.describe(member="BANするユーザー", reason="理由")
@app_commands.checks.has_permissions(administrator=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "理由なし"):
    await member.ban(reason=reason)
    await interaction.response.send_message(f"🔨 {member.mention} をBANしました。\n理由: {reason}")
    await log_action(interaction.guild, "🔨 BAN", member, f"理由: {reason} | 実行者: {interaction.user}")


@bot.tree.command(name="unban", description="ユーザーのBANを解除します（スタッフのみ）")
@app_commands.describe(user_id="解除するユーザーのID")
@app_commands.checks.has_permissions(administrator=True)
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        await interaction.response.send_message(f"✅ {user} のBANを解除しました。")
        await log_action(interaction.guild, "✅ BAN解除", user, f"実行者: {interaction.user}")
    except Exception:
        await interaction.response.send_message("❌ ユーザーが見つからないか、BANされていません。", ephemeral=True)


@bot.tree.command(name="timeout", description="ユーザーをタイムアウトします（スタッフのみ）")
@app_commands.describe(member="対象ユーザー", minutes="タイムアウト時間（分）", reason="理由")
@app_commands.checks.has_permissions(administrator=True)
async def timeout_cmd(interaction: discord.Interaction, member: discord.Member, minutes: int = 10, reason: str = "理由なし"):
    until = discord.utils.utcnow() + __import__("datetime").timedelta(minutes=minutes)
    await member.timeout(until, reason=reason)
    await interaction.response.send_message(f"⏱️ {member.mention} を{minutes}分タイムアウトしました。\n理由: {reason}")
    await log_action(interaction.guild, f"⏱️ タイムアウト {minutes}分", member, f"理由: {reason} | 実行者: {interaction.user}")


# ===========================
# ===== ログ送信 =====
# ===========================

async def log_action(guild: discord.Guild, action: str, user, detail: str = ""):
    """モデレーションログ（警告・キック・BAN・AutoModなど）"""
    ch_id = get_mod_log_channel_id()
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
    """チケットログ専用"""
    ch_id = get_log_channel_id()
    log_channel = guild.get_channel(ch_id) if ch_id else None
    if not log_channel:
        return
    await log_channel.send(embed=embed, file=file)


# ===========================
# ===== 禁止ワード管理コマンド =====
# ===========================

@bot.tree.command(name="url-add", description="許可するURLドメインを追加します（スタッフのみ）")
@app_commands.describe(domain="追加するドメイン（例: twitter.com）")
@app_commands.checks.has_permissions(administrator=True)
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
@app_commands.checks.has_permissions(administrator=True)
async def url_remove(interaction: discord.Interaction, domain: str):
    domain = domain.lower().replace("www.", "").strip()
    if domain not in ALLOWED_DOMAINS:
        await interaction.response.send_message(f"❌ `{domain}` は登録されていません。", ephemeral=True)
        return
    ALLOWED_DOMAINS.remove(domain)
    await interaction.response.send_message(f"✅ `{domain}` を許可リストから削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ 許可URL削除", interaction.user, f"ドメイン: `{domain}`")


@bot.tree.command(name="url-list", description="許可されているURLドメイン一覧を表示します（スタッフのみ）")
@app_commands.checks.has_permissions(administrator=True)
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
@app_commands.checks.has_permissions(administrator=True)
async def badword_add(interaction: discord.Interaction, word: str):
    if word in BAD_WORDS:
        await interaction.response.send_message(f"⚠️ `{word}` はすでに登録されています。", ephemeral=True)
        return
    BAD_WORDS.append(word)
    save_bad_words(BAD_WORDS)
    await interaction.response.send_message(f"✅ `{word}` を禁止ワードに追加しました。", ephemeral=True)
    await log_action(interaction.guild, "🚫 禁止ワード追加", interaction.user, f"追加ワード: `{word}`")


@bot.tree.command(name="badword-remove", description="禁止ワードを削除します（スタッフのみ）")
@app_commands.describe(word="削除する禁止ワード")
@app_commands.checks.has_permissions(administrator=True)
async def badword_remove(interaction: discord.Interaction, word: str):
    if word not in BAD_WORDS:
        await interaction.response.send_message(f"❌ `{word}` は登録されていません。", ephemeral=True)
        return
    BAD_WORDS.remove(word)
    save_bad_words(BAD_WORDS)
    await interaction.response.send_message(f"✅ `{word}` を禁止ワードから削除しました。", ephemeral=True)
    await log_action(interaction.guild, "🗑️ 禁止ワード削除", interaction.user, f"削除ワード: `{word}`")


@bot.tree.command(name="badword-list", description="禁止ワード一覧を表示します（スタッフのみ）")
@app_commands.checks.has_permissions(administrator=True)
async def badword_list(interaction: discord.Interaction):
    if not BAD_WORDS:
        await interaction.response.send_message("📋 禁止ワードは登録されていません。", ephemeral=True)
        return
    word_list = "\n".join([f"・{w}" for w in BAD_WORDS])
    embed = discord.Embed(
        title="🚫 禁止ワード一覧",
        description=word_list,
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ===========================
# ===== セットアップコマンド =====
# ===========================

@bot.tree.command(name="setup", description="ボットの設定を一括で行います（管理者のみ）")
@app_commands.describe(
    ticket_category="チケット用カテゴリ",
    auth_category="認証チケット用カテゴリ",
    ticket_log="チケットログチャンネル",
    mod_log="モデレーションログチャンネル"
)
@app_commands.checks.has_permissions(administrator=True)
async def setup(
    interaction: discord.Interaction,
    ticket_category: discord.CategoryChannel = None,
    auth_category: discord.CategoryChannel = None,
    ticket_log: discord.TextChannel = None,
    mod_log: discord.TextChannel = None
):
    if not any([ticket_category, auth_category, ticket_log, mod_log]):
        # 何も指定されていなければ現在の設定を表示
        guild = interaction.guild
        cat = guild.get_channel(config.get("ticket_category_id")) if config.get("ticket_category_id") else None
        auth_cat = guild.get_channel(config.get("auth_category_id")) if config.get("auth_category_id") else None
        log_ch = guild.get_channel(config.get("log_channel_id")) if config.get("log_channel_id") else None
        mod_ch = guild.get_channel(config.get("mod_log_channel_id")) if config.get("mod_log_channel_id") else None
        embed = discord.Embed(title="⚙️ 現在の設定", color=discord.Color.blurple())
        embed.add_field(name="🎫 チケットカテゴリ", value=cat.name if cat else "❌ 未設定", inline=False)
        embed.add_field(name="🔑 認証チケットカテゴリ", value=auth_cat.name if auth_cat else "❌ 未設定", inline=False)
        embed.add_field(name="📋 チケットログ", value=log_ch.mention if log_ch else "❌ 未設定", inline=False)
        embed.add_field(name="🔨 モデレーションログ", value=mod_ch.mention if mod_ch else "⚠️ 未設定（チケットログと共用）", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    changed = []
    if ticket_category:
        config["ticket_category_id"] = ticket_category.id
        changed.append(f"🎫 チケットカテゴリ → **{ticket_category.name}**")
    if auth_category:
        config["auth_category_id"] = auth_category.id
        changed.append(f"🔑 認証カテゴリ → **{auth_category.name}**")
    if ticket_log:
        config["log_channel_id"] = ticket_log.id
        changed.append(f"📋 チケットログ → {ticket_log.mention}")
    if mod_log:
        config["mod_log_channel_id"] = mod_log.id
        changed.append(f"🔨 モデレーションログ → {mod_log.mention}")

    save_config(config)
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

        log_channel = interaction.guild.get_channel(get_log_channel_id())
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
        category = guild.get_channel(get_auth_category_id())
        if category is None:
            await interaction.response.send_message(
                "❌ 認証チケット用カテゴリが未設定です。管理者が `/setup-auth-category` で設定してください。",
                ephemeral=True
            )
            return
    else:
        category = guild.get_channel(get_ticket_category_id())
        if category is None:
            await interaction.response.send_message(
                "❌ チケット用カテゴリが未設定です。管理者が `/setup-ticket-category` で設定してください。",
                ephemeral=True
            )
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
        mention_text = f"{member.mention} {admin_role.mention}" if admin_role else member.mention
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
@app_commands.checks.has_permissions(administrator=True)
async def send_auth_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔑 認証リクエスト",
        description=(
            "認証ができない方はボタンを押してリクエストを送ってください。\n\n"
            "🔑 **認証リクエスト** — 認証ができない方向けのサポート"
        ),
        color=discord.Color.gold()
    )
    await interaction.channel.send(embed=embed, view=AuthPanelView())
    await interaction.response.send_message("✅ 認証リクエストパネルを送信しました。", ephemeral=True)


@bot.tree.command(name="ticket-panel", description="チケットパネルを送信します（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
async def send_panel(interaction: discord.Interaction):
    embed = discord.Embed(title="🎫 サポートチケット", description="お問い合わせ内容に合わせてボタンを押してチケットを作成してください。\n\n❓ **サポート・質問** — サーバーに関する質問・サポート\n📩 **その他・お問い合わせ** — その他のお問い合わせ", color=discord.Color.blurple())
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message("✅ パネルを送信しました。", ephemeral=True)


# ===========================
# ===========================
# ===== バックアップ・復元 =====
# ===========================

@bot.tree.command(name="backup", description="サーバーのチャンネル構成とロールをバックアップします（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
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
@app_commands.checks.has_permissions(administrator=True)
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
    check_auth_tickets.start()
    auto_backup.start()
    bot.add_view(TicketPanelView())
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
    """チケットを10分応答がなければ自動削除"""
    from datetime import timedelta, timezone as tz
    auth_cat_id = config.get("auth_category_id")
    ticket_cat_id = config.get("ticket_category_id")

    for guild in bot.guilds:
        # 認証チケット（10分）
        if auth_cat_id:
            category = guild.get_channel(auth_cat_id)
            if category:
                for channel in list(category.text_channels):
                    if not channel.name.startswith("auth-request-"):
                        continue
                    # 人間のメッセージがあれば削除しない
                    has_human_msg = False
                    async for msg in channel.history(limit=50):
                        if not msg.author.bot:
                            has_human_msg = True
                            break
                    if has_human_msg:
                        continue
                    await _auto_delete_ticket(guild, channel, minutes=5)

        # 通常チケット（5分）
        if ticket_cat_id:
            category = guild.get_channel(ticket_cat_id)
            if category:
                for channel in list(category.text_channels):
                    await _auto_delete_ticket(guild, channel, minutes=5)


async def _auto_delete_ticket(guild, channel, minutes: int):
    """ユーザーのメッセージが一切なく、チャンネル作成から指定分数経過したら削除"""
    from datetime import timezone as tz
    try:
        now = datetime.now(tz.utc)
        # チャンネル作成から指定分数経過していなければスキップ
        if (now - channel.created_at).total_seconds() < minutes * 60:
            return

        # 人間のメッセージが1件でもあれば削除しない
        async for msg in channel.history(limit=50):
            if not msg.author.bot:
                return  # 人間のメッセージあり → 削除しない

        # Bot発言のみ & 時間経過 → 削除
        await _delete_and_log(guild, channel, minutes)
    except Exception:
        pass


async def _delete_and_log(guild, channel, minutes: int):
    log_channel = guild.get_channel(config.get("log_channel_id"))
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


bot.run(TOKEN)
