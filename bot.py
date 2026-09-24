import asyncio
import os
import re

import discord
from discord import app_commands

BOT_VERSION = "vc-panel v4 (コマンド自動削除)"
TOKEN = os.environ["DISCORD_TOKEN"]
TRIGGER_CHANNEL_ID = int(os.environ["TRIGGER_CHANNEL_ID"])  # 「ボイスを作成」VCのID

intents = discord.Intents.default()
intents.voice_states = True

NAME_RE = re.compile(r"^#(\d+) - ")  # 作成VCの名前判定用（例: #7 - Akiのチャンネル）
temp_channels: set[int] = set()  # 作成したVCのID
vc_owners: dict[int, int] = {}   # VC ID -> オーナーのユーザーID
vc_panels: dict[int, int] = {}   # VC ID -> パネルメッセージID
create_lock = asyncio.Lock()


def next_number(category_channels) -> int:
    """現在使われていない最小の番号を返す"""
    used = set()
    for ch in category_channels:
        m = NAME_RE.match(ch.name)
        if m and ch.id in temp_channels:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return n


# ===================== VCコントロールパネル =====================
OWNER_RE = re.compile(r"<@!?(\d+)>")  # 本文の最初のメンション = オーナー


def panel_text(owner_id: int, locked: bool = False) -> str:
    """パネル本文。埋め込みを使わないので「埋め込みリンク」権限がなくても動く"""
    return (
        f"🎛 **VCコントロールパネル**\n"
        f"オーナー: <@{owner_id}>\n"
        f"状態: {'🔒 ロック中' if locked else '🔓 開放中'}\n"
        "-# 操作できるのはオーナーだけ。オーナーがいなくなったら🙋で引き継げます"
    )


def parse_panel(message: discord.Message) -> tuple[int, bool] | None:
    """パネルからオーナーとロック状態を読む(読めなければ None)"""
    text = message.content or ""
    if message.embeds:  # 旧バージョンの埋め込みパネルにも対応
        e = message.embeds[0]
        text += " " + (e.description or "") + " " + " ".join(f.value for f in e.fields)
    m = OWNER_RE.search(text)
    if not m:
        return None
    return int(m[1]), "ロック中" in text


async def send_panel(vc: discord.VoiceChannel, owner_id: int):
    """VCのテキストチャットにパネルを出す"""
    try:
        msg = await vc.send(
            panel_text(owner_id),
            view=VCPanelView(),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except discord.HTTPException:
        return
    vc_owners[vc.id] = owner_id
    vc_panels[vc.id] = msg.id


async def set_owner(vc: discord.VoiceChannel, panel: discord.Message, new_owner: discord.Member):
    parsed = parse_panel(panel)
    locked = parsed[1] if parsed else False
    await panel.edit(content=panel_text(new_owner.id, locked), embed=None)
    vc_owners[vc.id] = new_owner.id
    vc_panels[vc.id] = panel.id
    if locked:  # ロック中でも新オーナーが入れるように
        ow = vc.overwrites_for(new_owner)
        ow.connect = True
        await vc.set_permissions(new_owner, overwrite=ow)
    await vc.send(f"👑 オーナーが {new_owner.mention} に変わりました",
                  allowed_mentions=discord.AllowedMentions(users=True))


async def lock_vc(vc: discord.VoiceChannel, owner_id: int):
    """@everyone と既存のロール上書きの接続を拒否し、今いる人だけ許可"""
    guild = vc.guild
    bot_roles = {r for r in guild.me.roles if r != guild.default_role}
    ows = dict(vc.overwrites)
    targets = [t for t in ows if isinstance(t, discord.Role)] + [guild.default_role]
    for role in targets:
        if role in bot_roles:
            continue
        ow = ows.get(role, discord.PermissionOverwrite())
        ow.connect = False
        ows[role] = ow
    allow = list(vc.members)
    owner = guild.get_member(owner_id)
    if owner:
        allow.append(owner)
    for m in allow:
        ow = ows.get(m, discord.PermissionOverwrite())
        ow.connect = True
        ows[m] = ow
    await vc.edit(overwrites=ows, reason="VCロック")


class RenameModal(discord.ui.Modal, title="VC名を変更"):
    new_name = discord.ui.TextInput(label="新しい名前", max_length=80, placeholder="例: アビス周回中")

    async def on_submit(self, interaction: discord.Interaction):
        vc = interaction.channel
        m = NAME_RE.match(vc.name)
        prefix = m.group(0) if m else ""  # 「#3 - 」は自動削除の判定に使うので残す
        await interaction.response.defer(ephemeral=True, thinking=True)
        await vc.edit(name=(prefix + self.new_name.value)[:100], reason="VC名変更")
        await interaction.followup.send("名前を変更しました", ephemeral=True)


class LimitModal(discord.ui.Modal, title="人数制限を変更"):
    limit = discord.ui.TextInput(label="人数(0で無制限、最大99)", max_length=2, placeholder="4")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.limit.value)
            if not 0 <= n <= 99:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("0〜99の数字を入れてください", ephemeral=True)
        await interaction.channel.edit(user_limit=n, reason="人数制限変更")
        await interaction.response.send_message(f"人数制限を {'無制限' if n == 0 else f'{n}人'} にしました", ephemeral=True)


class MemberSelectView(discord.ui.View):
    """キック / 譲渡の相手を選ぶ(本人だけに表示)"""

    def __init__(self, vc: discord.VoiceChannel, panel: discord.Message, candidates, action: str):
        super().__init__(timeout=60)
        self.vc, self.panel, self.action = vc, panel, action
        options = [discord.SelectOption(label=m.display_name[:100], value=str(m.id)) for m in candidates][:25]
        self.select = discord.ui.Select(placeholder="メンバーを選択", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        target = next((m for m in self.vc.members if m.id == int(self.select.values[0])), None)
        if target is None:
            return await interaction.response.edit_message(content="その人はもうVCにいません", view=None)
        if self.action == "kick":
            await target.move_to(None, reason=f"{interaction.user} がキック")
            parsed = parse_panel(self.panel)
            if parsed and parsed[1]:  # ロック中なら個別の接続許可を消して戻れなくする
                await self.vc.set_permissions(target, overwrite=None)
            await interaction.response.edit_message(content=f"{target.display_name} を切断しました", view=None)
        else:
            await set_owner(self.vc, self.panel, target)
            await interaction.response.edit_message(content=f"{target.display_name} にオーナーを譲りました", view=None)


class VCPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        """エラーが起きても「応答しませんでした」にしない"""
        print(f"[panel error] {item.label}: {error!r}")
        msg = f"エラーが起きました: {type(error).__name__}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def check(self, interaction: discord.Interaction, owner_only: bool = True):
        vc = interaction.channel
        if not isinstance(vc, discord.VoiceChannel) or vc.id not in temp_channels:
            await interaction.response.send_message("このパネルはもう使えません", ephemeral=True)
            return None
        parsed = parse_panel(interaction.message)
        if parsed is None:
            await interaction.response.send_message("パネルを読み取れませんでした。VCを作り直してください", ephemeral=True)
            return None
        owner_id, locked = parsed
        # 再起動後もパネルから状態を復元
        vc_owners[vc.id] = owner_id
        vc_panels[vc.id] = interaction.message.id
        if owner_only and interaction.user.id != owner_id:
            await interaction.response.send_message("オーナーだけが操作できます", ephemeral=True)
            return None
        return vc, owner_id, locked

    @discord.ui.button(label="名前変更", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="vc:rename", row=0)
    async def rename(self, interaction, button):
        if await self.check(interaction):
            await interaction.response.send_modal(RenameModal())

    @discord.ui.button(label="人数制限", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="vc:limit", row=0)
    async def limit(self, interaction, button):
        if await self.check(interaction):
            await interaction.response.send_modal(LimitModal())

    @discord.ui.button(label="ロック切替", emoji="🔒", style=discord.ButtonStyle.primary, custom_id="vc:lock", row=0)
    async def lock(self, interaction, button):
        r = await self.check(interaction)
        if not r:
            return
        vc, owner_id, locked = r
        await interaction.response.defer()
        try:
            if locked:
                await vc.edit(sync_permissions=True, reason="VCロック解除")  # カテゴリの権限に戻す
            else:
                await lock_vc(vc, owner_id)
        except discord.Forbidden:
            return await interaction.followup.send("Botに「ロールの管理」権限が必要です", ephemeral=True)
        await interaction.message.edit(content=panel_text(owner_id, not locked), embed=None)
        await interaction.followup.send("🔓 ロックを解除しました" if locked else "🔒 ロックしました(今いる人だけ入れます)",
                                        ephemeral=True)

    @discord.ui.button(label="キック", emoji="👢", style=discord.ButtonStyle.danger, custom_id="vc:kick", row=1)
    async def kick(self, interaction, button):
        r = await self.check(interaction)
        if not r:
            return
        vc, owner_id, _ = r
        candidates = [m for m in vc.members if m.id != owner_id and not m.bot]
        if not candidates:
            return await interaction.response.send_message("キックできるメンバーがいません", ephemeral=True)
        await interaction.response.send_message(
            "切断するメンバーを選んでください",
            view=MemberSelectView(vc, interaction.message, candidates, "kick"), ephemeral=True)

    @discord.ui.button(label="譲渡", emoji="👑", style=discord.ButtonStyle.secondary, custom_id="vc:transfer", row=1)
    async def transfer(self, interaction, button):
        r = await self.check(interaction)
        if not r:
            return
        vc, owner_id, _ = r
        candidates = [m for m in vc.members if m.id != owner_id and not m.bot]
        if not candidates:
            return await interaction.response.send_message("譲れるメンバーがいません", ephemeral=True)
        await interaction.response.send_message(
            "新しいオーナーを選んでください",
            view=MemberSelectView(vc, interaction.message, candidates, "transfer"), ephemeral=True)

    @discord.ui.button(label="オーナー取得", emoji="🙋", style=discord.ButtonStyle.success, custom_id="vc:claim", row=1)
    async def claim(self, interaction, button):
        r = await self.check(interaction, owner_only=False)
        if not r:
            return
        vc, owner_id, _ = r
        if interaction.user.id == owner_id:
            return await interaction.response.send_message("すでにあなたがオーナーです", ephemeral=True)
        if interaction.user not in vc.members:
            return await interaction.response.send_message("VCに入っている人だけが取得できます", ephemeral=True)
        if any(m.id == owner_id for m in vc.members):
            return await interaction.response.send_message("オーナーがまだVCにいます", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await set_owner(vc, interaction.message, interaction.user)
        await interaction.followup.send("オーナーになりました", ephemeral=True)



# ===================== Bot本体 =====================
class VCBot(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tree = app_commands.CommandTree(self)
        self.commands_cleared = False

    async def setup_hook(self):
        self.add_view(VCPanelView())  # 再起動後もパネルのボタンが動くように


client = VCBot(intents=intents)


@client.event
async def on_ready():
    print(f"Logged in as {client.user} / {BOT_VERSION}")
    trigger = client.get_channel(TRIGGER_CHANNEL_ID)
    if trigger is None:
        print("TRIGGER_CHANNEL_ID のチャンネルが見つかりません")
        return

    # 前のバージョンで登録された /コマンド を全部消す(このBotはコマンドを使わない)
    if not client.commands_cleared:
        client.tree.clear_commands(guild=trigger.guild)
        await client.tree.sync(guild=trigger.guild)
        await client.tree.sync()
        client.commands_cleared = True
        print("スラッシュコマンドを削除しました")


    # 再起動前に作られたVCを整理
    for ch in trigger.guild.voice_channels:
        if ch.id != TRIGGER_CHANNEL_ID and ch.category == trigger.category and NAME_RE.match(ch.name):
            if len(ch.members) == 0:
                await ch.delete(reason="temp VC cleanup")
            else:
                temp_channels.add(ch.id)


@client.event
async def on_voice_state_update(member, before, after):
    # 1) 「ボイスを作成」に入ったら、新しいVCを作って移動
    if after.channel and after.channel.id == TRIGGER_CHANNEL_ID:
        trigger = after.channel
        async with create_lock:
            siblings = trigger.category.voice_channels if trigger.category else trigger.guild.voice_channels
            n = next_number(siblings)
            new_vc = await trigger.guild.create_voice_channel(
                name=f"#{n} - {member.display_name}のチャンネル",
                category=trigger.category,
                bitrate=trigger.bitrate,
                reason=f"temp VC for {member}",
            )
            temp_channels.add(new_vc.id)
        try:
            await member.move_to(new_vc)
        except discord.HTTPException:
            # 移動前に本人が退出した場合など
            temp_channels.discard(new_vc.id)
            await new_vc.delete()
        else:
            await send_panel(new_vc, member.id)

    left = before.channel if before.channel and (after.channel is None or after.channel.id != before.channel.id) else None

    # 2) オーナーが抜けたら、残っている人に自動で引き継ぐ
    if left and left.id in temp_channels and left.members and vc_owners.get(left.id) == member.id:
        humans = [m for m in left.members if not m.bot]
        panel_id = vc_panels.get(left.id)
        if humans and panel_id:
            try:
                panel = await left.fetch_message(panel_id)
                await set_owner(left, panel, humans[0])
            except discord.HTTPException:
                pass

    # 3) 作成したVCが空になったら削除
    if before.channel and before.channel.id in temp_channels and len(before.channel.members) == 0:
        temp_channels.discard(before.channel.id)
        vc_owners.pop(before.channel.id, None)
        vc_panels.pop(before.channel.id, None)
        try:
            await before.channel.delete(reason="temp VC empty")
        except discord.NotFound:
            pass


client.run(TOKEN)
