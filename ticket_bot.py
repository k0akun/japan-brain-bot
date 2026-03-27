import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime
import os

# ===== 設定 =====
TOKEN = os.environ.get("TOKEN")"
GUILD_ID = 1408888613961339022          
TICKET_CATEGORY_ID = 1431152459023122533
LOG_CHANNEL_ID = 1408890176926908519
STAFF_ROLE_ID = 1426318103469621398
# ================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ===== チケットパネルのビュー（ボタン一覧） =====
class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📋 モデレーター応募",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_mod"
    )
    async def ticket_mod(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "mod-application", "モデレーター応募")

    @discord.ui.button(
        label="❓ サポート・質問",
        style=discord.ButtonStyle.success,
        custom_id="ticket_support"
    )
    async def ticket_support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "support", "サポート・質問")

    @discord.ui.button(
        label="📩 その他・お問い合わせ",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket_other"
    )
    async def ticket_other(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction, "inquiry", "その他・お問い合わせ")


# ===== チケット内のビュー（閉じるボタン） =====
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔒 チケットを閉じる",
        style=discord.ButtonStyle.danger,
        custom_id="close_ticket"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel

        # スタッフロール確認
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        if staff_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ スタッフのみチケットを閉じられます。", ephemeral=True)
            return

        await interaction.response.send_message("🔒 チケットを閉じています。ログを保存中...", ephemeral=False)

        # ログ収集
        log_text = f"=== チケットログ: {channel.name} ===\n"
        log_text += f"クローズ日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        log_text += f"クローズ者: {interaction.user} ({interaction.user.id})\n\n"

        async for msg in channel.history(limit=500, oldest_first=True):
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            log_text += f"[{timestamp}] {msg.author}: {msg.content}\n"
            for attachment in msg.attachments:
                log_text += f"  [添付ファイル: {attachment.url}]\n"

        # ログチャンネルに送信
        log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
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

    # 既存チケットチェック
    category = guild.get_channel(TICKET_CATEGORY_ID)
    existing = discord.utils.get(category.channels, name=f"{ticket_type}-{member.name.lower()}")
    if existing:
        await interaction.response.send_message(
            f"❌ すでにチケットがあります: {existing.mention}", ephemeral=True
        )
        return

    # パーミッション設定
    staff_role = guild.get_role(STAFF_ROLE_ID)
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


# ===== スラッシュコマンド: パネル送信 =====
@bot.tree.command(name="ticket-panel", description="チケットパネルを送信します（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
async def send_panel(interaction: discord.Interaction):
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
    # Viewを永続化（再起動後もボタンが機能する）
    bot.add_view(TicketPanelView())
    bot.add_view(TicketView())

    try:
        synced = await bot.tree.sync()
        print(f"✅ スラッシュコマンドを同期しました ({len(synced)}個)")
    except Exception as e:
        print(f"❌ 同期エラー: {e}")

    print(f"✅ {bot.user} としてログインしました")


bot.run(TOKEN)
