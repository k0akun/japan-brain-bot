import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime
import os
import sqlite3

# ===== 設定 =====
TOKEN = os.environ.get("TOKEN")
# ================

# ===== データベース初期化 =====
def init_db():
    conn = sqlite3.connect("servers.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS server_config (
            guild_id INTEGER PRIMARY KEY,
            ticket_category_id INTEGER,
            log_channel_id INTEGER,
            staff_role_id INTEGER
        )
    """)
    conn.commit()
    conn.close()

def get_config(guild_id: int):
    conn = sqlite3.connect("servers.db")
    c = conn.cursor()
    c.execute("SELECT ticket_category_id, log_channel_id, staff_role_id FROM server_config WHERE guild_id = ?", (guild_id,))
    row = c.fetchone()
    conn.close()
    return row  # (ticket_category_id, log_channel_id, staff_role_id) or None

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
