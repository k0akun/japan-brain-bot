import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime, timedelta
import os
import sqlite3
import re

# ===== 設定 =====
TOKEN = os.environ.get("TOKEN")
MAX_MESSAGE_LENGTH = 140   # これ以上の文字数で削除
TIMEOUT_MINUTES = 5        # タイムアウト時間（分）
SPAM_COUNT = 3             # 同じ内容を何回送ったらタイムアウトか
# ================

# ===== スパム検知用キャッシュ =====
from collections import defaultdict
import time
from difflib import SequenceMatcher
spam_cache = defaultdict(lambda: {"content": "", "count": 0})
# 内容ベーススパム検知: [(user_id, content, timestamp), ...]
content_spam_cache = []
CONTENT_SPAM_USERS = 4     # 何人が似た内容を送ったらタイムアウト
CONTENT_SPAM_SECONDS = 10  # 何秒以内にカウント
CONTENT_SPAM_RATIO = 0.80  # 類似度しきい値（80%以上で一致とみなす）
CONTENT_SPAM_PREFIX = 8    # 前半この文字数以上一致したら同じ文とみなす
MAX_NEWLINES = 10          # これ以上の改行で削除＋タイムアウト

# ===== データベース初期化 =====
def init_db():
    conn = sqlite3.connect("servers.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS server_config (
            guild_id INTEGER PRIMARY KEY,
            ticket_category_id INTEGER,
            log_channel_id INTEGER,
            staff_role_id INTEGER,
            mod_log_channel_id INTEGER
        )
    """)
    # 既存テーブルにカラムがなければ追加
    try:
        c.execute("ALTER TABLE server_config ADD COLUMN mod_log_channel_id INTEGER")
    except Exception:
        pass
    conn.commit()
    conn.close()

def get_config(guild_id: int):
    conn = sqlite3.connect("servers.db")
    c = conn.cursor()
    c.execute("SELECT ticket_category_id, log_channel_id, staff_role_id FROM server_config WHERE guild_id = ?", (guild_id,))
    row = c.fetchone()
    conn.close()
    return row  # (ticket_category_id, log_channel_id, staff_role_id) or None

def get_mod_log(guild_id: int):
    conn = sqlite3.connect("servers.db")
    c = conn.cursor()
    c.execute("SELECT mod_log_channel_id FROM server_config WHERE guild_id = ?", (guild_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_mod_log(guild_id: int, channel_id: int):
    conn = sqlite3.connect("servers.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO server_config (guild_id, mod_log_channel_id) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET mod_log_channel_id = excluded.mod_log_channel_id",
        (guild_id, channel_id)
    )
    conn.commit()
    conn.close()

def set_config(guild_id: int, ticket_category_id: int, log_channel_id: int, staff_role_id: int):
    conn = sqlite3.connect("servers.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO server_config (guild_id, ticket_category_id, log_channel_id, staff_role_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            ticket_category_id = excluded.ticket_category_id,
            log_channel_id = excluded.log_channel_id,
            staff_role_id = excluded.staff_role_id
    """, (guild_id, ticket_category_id, log_channel_id, staff_role_id))
    conn.commit()
    conn.close()


intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ===== チケットパネルのビュー =====
class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📋 モデレーター応募", style=discord.ButtonStyle.primary, custom_id="ticket_mod")
    async def ticket_mod(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "mod-application", "モデレーター応募")

    @discord.ui.button(label="❓ サポート・質問", style=discord.ButtonStyle.success, custom_id="ticket_support")
    async def ticket_support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "support", "サポート・質問")

    @discord.ui.button(label="📩 その他・お問い合わせ", style=discord.ButtonStyle.secondary, custom_id="ticket_other")
    async def ticket_other(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "inquiry", "その他・お問い合わせ")


# ===== チケット内のビュー（閉じるボタン） =====
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 チケットを閉じる", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        config = get_config(interaction.guild.id)

        if not config:
            await interaction.response.send_message("❌ このサーバーはまだ設定されていません。`/setup` を実行してください。", ephemeral=True)
            return

        _, log_channel_id, staff_role_id = config
        staff_role = interaction.guild.get_role(staff_role_id)

        if staff_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ スタッフのみチケットを閉じられます。", ephemeral=True)
            return

        await interaction.response.send_message("🔒 チケットを閉じています。ログを保存中...", ephemeral=False)

        log_text = f"=== チケットログ: {channel.name} ===\n"
        log_text += f"クローズ日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        log_text += f"クローズ者: {interaction.user} ({interaction.user.id})\n\n"

        async for msg in channel.history(limit=500, oldest_first=True):
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            log_text += f"[{timestamp}] {msg.author}: {msg.content}\n"
            for attachment in msg.attachments:
                log_text += f"  [添付ファイル: {attachment.url}]\n"

        log_channel = interaction.guild.get_channel(log_channel_id)
        if log_channel:
            log_file = discord.File(
                fp=__import__("io").StringIO(log_text),
                filename=f"{channel.name}-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
            )
            embed = discord.Embed(
                title="📋 チケットログ",
                description=f"チャンネル: `{channel.name}`\nクローズ者: {interaction.user.mention}",
                color=discord.Color.orange(),
                timestamp=datetime.now()
            )
            await log_channel.send(embed=embed, file=log_file)

        await asyncio.sleep(3)
        await channel.delete(reason=f"チケットクローズ by {interaction.user}")


# ===== チケット作成処理 =====
async def create_ticket(interaction: discord.Interaction, ticket_type: str, label: str):
    guild = interaction.guild
    member = interaction.user
    config = get_config(guild.id)

    if not config:
        await interaction.response.send_message("❌ このサーバーはまだ設定されていません。管理者に `/setup` の実行を依頼してください。", ephemeral=True)
        return

    ticket_category_id, _, staff_role_id = config
    category = guild.get_channel(ticket_category_id)

    if not category:
        await interaction.response.send_message("❌ チケットカテゴリが見つかりません。管理者に `/setup` の再設定を依頼してください。", ephemeral=True)
        return

    existing = discord.utils.get(category.channels, name=f"{ticket_type}-{member.name.lower()}")
    if existing:
        await interaction.response.send_message(f"❌ すでにチケットがあります: {existing.mention}", ephemeral=True)
        return

    staff_role = guild.get_role(staff_role_id)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }

    channel_name = f"{ticket_type}-{member.name.lower()}"
    channel = await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        topic=f"{label} | {member} ({member.id})"
    )

    embed = discord.Embed(
        title=f"🎫 {label}",
        description=(
            f"{member.mention} さん、チケットを作成しました！\n\n"
            f"**内容を詳しく教えてください。**\nスタッフが確認次第、対応いたします。\n\n"
            f"チケットを閉じる場合は下のボタンを押してください（スタッフのみ）。"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.set_footer(text=f"チケットID: {channel.id}")

    await channel.send(content=f"{member.mention} {staff_role.mention}", embed=embed, view=TicketView())
    await interaction.response.send_message(f"✅ チケットを作成しました: {channel.mention}", ephemeral=True)


# ===== スラッシュコマンド: セットアップ =====
@bot.tree.command(name="setup", description="このサーバーのチケットBot設定を行います（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    ticket_category="チケットを作成するカテゴリ",
    log_channel="ログを送信するチャンネル",
    staff_role="スタッフロール"
)
async def setup(
    interaction: discord.Interaction,
    ticket_category: discord.CategoryChannel,
    log_channel: discord.TextChannel,
    staff_role: discord.Role
):
    set_config(interaction.guild.id, ticket_category.id, log_channel.id, staff_role.id)
    embed = discord.Embed(
        title="✅ セットアップ完了",
        description=(
            f"**チケットカテゴリ:** {ticket_category.name}\n"
            f"**ログチャンネル:** {log_channel.mention}\n"
            f"**スタッフロール:** {staff_role.mention}"
        ),
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ===== スラッシュコマンド: モデレーターログ設定 =====
@bot.tree.command(name="set-mod-log", description="荒らし対策のログチャンネルを設定します（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="ログを送信するチャンネル")
async def set_mod_log_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    set_mod_log(interaction.guild.id, channel.id)
    embed = discord.Embed(
        title="✅ モデレーターログ設定完了",
        description=f"荒らし対策のログチャンネルを {channel.mention} に設定しました。",
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ===== スラッシュコマンド: パネル送信 =====
@bot.tree.command(name="ticket-panel", description="チケットパネルを送信します（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
async def send_panel(interaction: discord.Interaction):
    config = get_config(interaction.guild.id)
    if not config:
        await interaction.response.send_message("❌ まず `/setup` でこのサーバーの設定を行ってください。", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎫 サポートチケット",
        description=(
            "お問い合わせ内容に合わせてボタンを押してチケットを作成してください。\n\n"
            "📋 **モデレーター応募** — モデレーターに応募したい方\n"
            "❓ **サポート・質問** — サーバーに関する質問・サポート\n"
            "📩 **その他・お問い合わせ** — その他のお問い合わせ"
        ),
        color=discord.Color.blurple()
    )
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message("✅ パネルを送信しました。", ephemeral=True)


# ===== 荒らし対策 =====
URL_PATTERN = re.compile(r"https?://[^\s]+|discord\.gg/[^\s]+")
ALLOWED_DOMAINS = ("youtube.com", "youtu.be", "www.youtube.com")

def contains_blocked_link(text: str) -> bool:
    urls = URL_PATTERN.findall(text)
    for url in urls:
        if not any(domain in url for domain in ALLOWED_DOMAINS):
            return True
    return False

def is_similar(a: str, b: str) -> bool:
    """類似度80%以上 OR 前半8文字以上一致で同じ文とみなす"""
    if SequenceMatcher(None, a, b).ratio() >= CONTENT_SPAM_RATIO:
        return True
    prefix_len = CONTENT_SPAM_PREFIX
    if len(a) >= prefix_len and len(b) >= prefix_len:
        if a[:prefix_len] == b[:prefix_len]:
            return True
    return False

async def punish(message: discord.Message, reason: str, notify: str):
    # タイムアウトを先に実行
    timeout_until = discord.utils.utcnow() + timedelta(minutes=TIMEOUT_MINUTES)
    try:
        await message.author.timeout(timeout_until, reason=reason)
    except (discord.errors.Forbidden, discord.errors.HTTPException):
        pass

    # そのユーザーの直近メッセージを一括削除（レートリミット対策）
    try:
        def is_target(m):
            return m.author.id == message.author.id
        await message.channel.purge(limit=10, check=is_target, bulk=True)
    except (discord.errors.Forbidden, discord.errors.HTTPException):
        pass

    # 本人のみに通知（ephemeral風にDM送信）
    try:
        await message.author.send(
            f"⚠️ **{message.guild.name}** で自動タイムアウトされました。\n"
            f"理由: {notify}\n"
            f"タイムアウト時間: {TIMEOUT_MINUTES}分"
        )
    except (discord.errors.Forbidden, discord.errors.HTTPException):
        pass

    # モデレーターログに記録
    mod_log_id = get_mod_log(message.guild.id)
    if mod_log_id:
        log_channel = message.guild.get_channel(mod_log_id)
        if log_channel:
            embed = discord.Embed(
                title="🔨 自動タイムアウト",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            embed.add_field(name="ユーザー", value=f"{message.author.mention} ({message.author.id})", inline=False)
            embed.add_field(name="理由", value=notify, inline=False)
            embed.add_field(name="チャンネル", value=message.channel.mention, inline=False)
            embed.add_field(name="内容", value=message.content[:300] or "（空）", inline=False)
            embed.add_field(name="タイムアウト時間", value=f"{TIMEOUT_MINUTES}分", inline=False)
            embed.set_thumbnail(url=message.author.display_avatar.url)
            try:
                await log_channel.send(embed=embed)
            except (discord.errors.Forbidden, discord.errors.HTTPException):
                pass

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.author.guild_permissions.administrator:
        return

    # 添付ファイルのみ（テキストなし）はスキップ
    text = message.content.strip()

    if text:
        # 長文チェック
        content_stripped = text.replace(" ", "").replace("\n", "").replace("\u3000", "")
        if len(content_stripped) > MAX_MESSAGE_LENGTH or len(text) > MAX_MESSAGE_LENGTH:
            await punish(message, "長文スパム検知", "長文スパムを検知しました。")
            return

        # 改行スパムチェック
        if text.count("\n") >= MAX_NEWLINES:
            await punish(message, "改行スパム検知", "改行スパムを検知しました。")
            return

        # 連続同一メッセージチェック（6文字以上のみ対象）
        if len(text) >= 6:
            cache = spam_cache[message.author.id]
            if text == cache["content"]:
                cache["count"] += 1
            else:
                cache["content"] = text
                cache["count"] = 1
            if cache["count"] >= SPAM_COUNT:
                spam_cache[message.author.id] = {"content": "", "count": 0}
                await punish(message, "連続スパム検知", "同じメッセージを連続で送信しています。")
                return
        else:
            spam_cache[message.author.id] = {"content": "", "count": 0}

    # リンクチェック（YouTube以外をブロック）
    if contains_blocked_link(message.content):
        await punish(message, "不正リンク検知", "リンクの送信は禁止されています。")
        return

    # 内容ベーススパム検知（類似度80%以上で複数人が送ったらアウト）
    now = time.time()
    text = message.content.strip()
    if text:
        # 古いエントリを削除
        content_spam_cache[:] = [
            (uid, t, ts) for uid, t, ts in content_spam_cache
            if now - ts < CONTENT_SPAM_SECONDS
        ]

        # 類似するエントリを探す
        similar = [
            (uid, t, ts) for uid, t, ts in content_spam_cache
            if uid != message.author.id and SequenceMatcher(None, text, t).ratio() >= CONTENT_SPAM_RATIO
        ]

        content_spam_cache.append((message.author.id, text, now))

        if len(similar) + 1 >= CONTENT_SPAM_USERS:
            guilty = similar + [(message.author.id, text, now)]
            # キャッシュから削除
            guilty_ids = {uid for uid, _, _ in guilty}
            content_spam_cache[:] = [
                (uid, t, ts) for uid, t, ts in content_spam_cache
                if uid not in guilty_ids
            ]
            for uid in guilty_ids:
                member = message.guild.get_member(uid)
                if member and not member.guild_permissions.administrator:
                    try:
                        timeout_until = discord.utils.utcnow() + timedelta(minutes=TIMEOUT_MINUTES)
                        await member.timeout(timeout_until, reason="複数アカウントスパム検知")
                    except (discord.errors.Forbidden, discord.errors.HTTPException):
                        pass
            await punish(message, "複数アカウントスパム検知", "複数アカウントによるスパムを検知しました。")
            return

    await bot.process_commands(message)


# ===== Bot起動時 =====
@bot.event
async def on_ready():
    init_db()
    bot.add_view(TicketPanelView())
    bot.add_view(TicketView())

    try:
        synced = await bot.tree.sync()
        print(f"✅ スラッシュコマンドを同期しました ({len(synced)}個)")
    except Exception as e:
        print(f"❌ 同期エラー: {e}")

    print(f"✅ {bot.user} としてログインしました")


bot.run(TOKEN)
