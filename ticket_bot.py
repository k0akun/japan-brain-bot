import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime, timezone
import os
import json
from collections import defaultdict

# ===== 設定 =====
TOKEN = os.environ.get("TOKEN")
ADMIN_ROLE_ID = 1486603522748317737

SPAM_LIMIT = 5
SPAM_INTERVAL = 5
RAID_ACCOUNT_AGE_DAYS = 7
RAID_JOIN_LIMIT = 5
RAID_JOIN_INTERVAL = 10
# ================

# ===== サーバー設定をJSONで管理 =====
CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "ticket_category_id": None,
        "log_channel_id": None,
    }

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()

def get_ticket_category_id():
    return config.get("ticket_category_id")

def get_log_channel_id():
    return config.get("log_channel_id")

# ===== 禁止ワードをJSONで管理 =====
BAD_WORDS_FILE = "bad_words.json"

def load_bad_words():
    if os.path.exists(BAD_WORDS_FILE):
        with open(BAD_WORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return ["死ね", "殺す", "クソ", "バカ", "アホ"]

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

# レイド検知用メモリ
join_tracker = []

# 警告数メモリ
warn_tracker = defaultdict(int)


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

    # URLフィルター
    import re
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
            warn_tracker[message.author.id] += 1
            count = warn_tracker[message.author.id]
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
        warn_tracker[user_id] += 1
        count = warn_tracker[user_id]
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
    elif count == 5:
        await member.kick(reason="警告5回（AutoMod）")
        await log_action(guild, "👢 キック", member, "警告5回に達したため")
    elif count >= 7:
        await member.ban(reason="警告7回（AutoMod）")
        await log_action(guild, "🔨 BAN", member, "警告7回に達したため")


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
    warn_tracker[member.id] += 1
    count = warn_tracker[member.id]
    await interaction.response.send_message(f"⚠️ {member.mention} に警告を出しました。({count}回目)\n理由: {reason}")
    await log_action(interaction.guild, "⚠️ 警告", member, f"理由: {reason} | 合計{count}回")
    await auto_punish(member, interaction.guild, count)


@bot.tree.command(name="warns", description="ユーザーの警告数を確認します")
@app_commands.describe(member="確認するユーザー")
@app_commands.checks.has_permissions(administrator=True)
async def warns(interaction: discord.Interaction, member: discord.Member):
    count = warn_tracker[member.id]
    await interaction.response.send_message(f"📋 {member.mention} の警告数: **{count}回**", ephemeral=True)


@bot.tree.command(name="clearwarn", description="ユーザーの警告をリセットします（スタッフのみ）")
@app_commands.describe(member="リセットするユーザー")
@app_commands.checks.has_permissions(administrator=True)
async def clearwarn(interaction: discord.Interaction, member: discord.Member):
    warn_tracker[member.id] = 0
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
    log_channel = guild.get_channel(get_log_channel_id())
    if not log_channel:
        return
    embed = discord.Embed(
        title=action,
        description=f"**ユーザー:** {user.mention if hasattr(user, 'mention') else user}\n{detail}",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )
    await log_channel.send(embed=embed)


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

@bot.tree.command(name="setup-ticket-category", description="チケットを作るカテゴリを設定します（管理者のみ）")
@app_commands.describe(category="チケット用カテゴリ")
@app_commands.checks.has_permissions(administrator=True)
async def setup_ticket_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    config["ticket_category_id"] = category.id
    save_config(config)
    await interaction.response.send_message(f"✅ チケットカテゴリを **{category.name}** に設定しました！", ephemeral=True)

@bot.tree.command(name="setup-log-channel", description="ログを送るチャンネルを設定します（管理者のみ）")
@app_commands.describe(channel="ログチャンネル")
@app_commands.checks.has_permissions(administrator=True)
async def setup_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    config["log_channel_id"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ ログチャンネルを **{channel.name}** に設定しました！", ephemeral=True)

@bot.tree.command(name="setup-check", description="現在の設定を確認します（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
async def setup_check(interaction: discord.Interaction):
    guild = interaction.guild
    cat_id = get_ticket_category_id()
    log_id = get_log_channel_id()
    category = guild.get_channel(cat_id) if cat_id else None
    log_ch = guild.get_channel(log_id) if log_id else None
    embed = discord.Embed(title="⚙️ 現在の設定", color=discord.Color.blurple())
    embed.add_field(name="🎫 チケットカテゴリ", value=category.name if category else "❌ 未設定", inline=False)
    embed.add_field(name="📋 ログチャンネル", value=log_ch.mention if log_ch else "❌ 未設定", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ===========================
# ===== チケット機能 =====
# ===========================

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
            await log_channel.send(embed=embed, file=log_file)

        await asyncio.sleep(3)
        await channel.delete(reason=f"チケットクローズ by {interaction.user}")


async def create_ticket(interaction: discord.Interaction, ticket_type: str, label: str):
    guild = interaction.guild
    member = interaction.user
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
    mention_text = f"{member.mention} {admin_role.mention}" if admin_role else member.mention
    embed = discord.Embed(title=f"🎫 {label}", description=f"{member.mention} さん、チケットを作成しました！\n\n**内容を詳しく教えてください。**\nスタッフが確認次第、対応いたします。\n\nチケットを閉じる場合は下のボタンを押してください（管理者のみ）。", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"チケットID: {channel.id}")
    await channel.send(content=mention_text, embed=embed, view=TicketView())
    await interaction.response.send_message(f"✅ チケットを作成しました: {channel.mention}", ephemeral=True)


@bot.tree.command(name="ticket-panel", description="チケットパネルを送信します（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
async def send_panel(interaction: discord.Interaction):
    embed = discord.Embed(title="🎫 サポートチケット", description="お問い合わせ内容に合わせてボタンを押してチケットを作成してください。\n\n📋 **モデレーター応募** — モデレーターに応募したい方\n❓ **サポート・質問** — サーバーに関する質問・サポート\n📩 **その他・お問い合わせ** — その他のお問い合わせ", color=discord.Color.blurple())
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message("✅ パネルを送信しました。", ephemeral=True)


# ===========================
# ===== Bot起動時 =====
# ===========================

@bot.event
async def on_ready():
    bot.add_view(TicketPanelView())
    bot.add_view(TicketView())
    try:
        synced = await bot.tree.sync()
        print(f"✅ スラッシュコマンドを同期しました ({len(synced)}個)")
    except Exception as e:
        print(f"❌ 同期エラー: {e}")
    print(f"✅ {bot.user} としてログインしました")


bot.run(TOKEN)
