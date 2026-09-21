import asyncio
import os

import discord
from discord import app_commands

TOKEN = os.environ["DISCORD_TOKEN"]
PREFIX = os.getenv("TEMP_VC_PREFIX", "🔊 ")  # 自動作成VCの名前の先頭
EMPTY_TIMEOUT = int(os.getenv("EMPTY_TIMEOUT", "60"))  # 作成後、誰も入らなければ消すまでの秒数
# 任意: 設定すると、起動時にそのチャンネルへボタンを自動投稿する
_panel = os.getenv("PANEL_CHANNEL_ID")
PANEL_CHANNEL_ID = int(_panel) if _panel else None

PANEL_TEXT = "下のボタンを押すと、あなた用のボイスチャンネルが作成されます。"

intents = discord.Intents.default()
intents.voice_states = True

owners: dict[int, int] = {}  # 作成したVCのID -> 作成者のID


async def delete_vc(channel: discord.VoiceChannel):
    owners.pop(channel.id, None)
    try:
        await channel.delete(reason="temp VC empty")
    except discord.NotFound:
        pass


async def delete_if_still_empty(channel_id: int):
    await asyncio.sleep(EMPTY_TIMEOUT)
    ch = client.get_channel(channel_id)
    if ch and len(ch.members) == 0:
        await delete_vc(ch)


class CreateVCView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 再起動後もボタンを有効にする

    @discord.ui.button(
        label="VCを作成",
        emoji="➕",
        style=discord.ButtonStyle.primary,
        custom_id="create_temp_vc",
    )
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        # すでに自分が作ったVCがあれば、新規作成せず案内する
        for cid, owner_id in list(owners.items()):
            if owner_id == member.id:
                ch = guild.get_channel(cid)
                if ch:
                    await interaction.response.send_message(
                        f"すでに作成済みです: {ch.mention}", ephemeral=True
                    )
                    return
                owners.pop(cid, None)

        await interaction.response.defer(ephemeral=True)

        # ボタンを押したチャンネルと同じカテゴリに作成
        category = getattr(interaction.channel, "category", None)
        vc = await guild.create_voice_channel(
            name=f"{PREFIX}{member.display_name}",
            category=category,
            reason=f"temp VC for {member}",
        )
        owners[vc.id] = member.id

        if member.voice and member.voice.channel:
            try:
                await member.move_to(vc)
                await interaction.followup.send(f"{vc.mention} を作成して移動しました。", ephemeral=True)
                return
            except discord.HTTPException:
                pass

        # ボイスに未接続の場合は、作成したVCへのリンクを案内する
        await interaction.followup.send(
            f"{vc.mention} を作成しました。{EMPTY_TIMEOUT}秒以内に参加してください。",
            ephemeral=True,
        )
        asyncio.create_task(delete_if_still_empty(vc.id))


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(CreateVCView())


client = Bot()


@client.tree.command(name="message", description="VC作成ボタンをこのチャンネルに送信します")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def message_cmd(interaction: discord.Interaction):
    # インタラクションの返信として送るので、チャンネルへの送信権限がなくても動く
    await interaction.response.send_message(PANEL_TEXT, view=CreateVCView())


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    for guild in client.guilds:
        # スラッシュコマンドをサーバーに即時反映
        client.tree.copy_global_to(guild=guild)
        try:
            await client.tree.sync(guild=guild)
        except discord.HTTPException as e:
            print(f"コマンド同期に失敗: {guild.name}: {e}")

        # 再起動前に作られたVCを整理
        for ch in guild.voice_channels:
            if ch.name.startswith(PREFIX):
                if len(ch.members) == 0:
                    await delete_vc(ch)
                else:
                    owners.setdefault(ch.id, 0)

    # 任意: PANEL_CHANNEL_ID があれば、ボタンがなければ自動投稿
    if PANEL_CHANNEL_ID:
        panel = client.get_channel(PANEL_CHANNEL_ID)
        if panel is None:
            print("PANEL_CHANNEL_ID のチャンネルが見つかりません")
            return
        try:
            async for m in panel.history(limit=50):
                if m.author == client.user and m.components:
                    return
            await panel.send(PANEL_TEXT, view=CreateVCView())
        except discord.Forbidden:
            print("パネルチャンネルへの送信/履歴閲覧の権限がありません（/message を使ってください）")


@client.event
async def on_voice_state_update(member, before, after):
    # 作成したVCが空になったら削除
    if before.channel and before.channel.id in owners and len(before.channel.members) == 0:
        await delete_vc(before.channel)


client.run(TOKEN)
