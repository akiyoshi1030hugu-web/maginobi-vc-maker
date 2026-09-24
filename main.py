import asyncio
import os
import re
from datetime import datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

# ===================== 設定 =====================
TOKEN = os.environ["DISCORD_TOKEN"]
TRIGGER_CHANNEL_ID = int(os.environ["TRIGGER_CHANNEL_ID"])      # 「ボイスを作成」VCのID
NOTIFY_CHANNEL_ID = int(os.environ.get("NOTIFY_CHANNEL_ID", "0"))  # 通知を流すテキストチャンネル
BOSS_ROLE_ID = int(os.environ.get("BOSS_ROLE_ID", "0"))          # フィールドボス通知ロール
BARRIER_ROLE_ID = int(os.environ.get("BARRIER_ROLE_ID", "0"))    # 結界通知ロール
WEEKLY_ROLE_ID = int(os.environ.get("WEEKLY_ROLE_ID", "0"))      # 週課リマインドロール
BARRIER_ENABLED = os.environ.get("BARRIER_ENABLED", "0") == "1"  # 結界通知(毎時)を使うか

JST = ZoneInfo("Asia/Tokyo")

# 韓国版の仕様。日本版で変わったらここを直す
FIELD_BOSS_HOURS = [12, 18, 20, 22]  # 出現時刻
FIELD_BOSS_DURATION_MIN = 30         # 出現後に討伐できる時間
FIELD_BOSS_NOTICE_MIN = 5            # 何分前に通知するか
BARRIER_NOTICE_MIN = 3
WEEKLY_RESET = (0, 6)                # (曜日 月=0, 時) 週間リセット
WEEKLY_REMIND = (6, 21)              # (曜日 日=6, 時) リマインド

PARTY_PRESETS = ["アビス", "レイド", "深層ダンジョン", "フィールドボス", "結界", "黒い穴", "生活・交流"]

# ===================== Bot本体 =====================
intents = discord.Intents.default()
intents.voice_states = True

NAME_RE = re.compile(r"^#(\d+) - ")  # 作成VCの名前判定用
temp_channels: set[int] = set()
create_lock = asyncio.Lock()
party_lock = asyncio.Lock()
vc_owners: dict[int, int] = {}   # VC ID -> オーナーのユーザーID
vc_panels: dict[int, int] = {}   # VC ID -> パネルメッセージID


def before(hour: int, minutes: int) -> dt_time:
    """hour:00 の minutes 分前の時刻(JST)"""
    total = (hour * 60 - minutes) % (24 * 60)
    return dt_time(hour=total // 60, minute=total % 60, tzinfo=JST)


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


async def create_temp_vc(guild: discord.Guild, name: str, user_limit: int = 0) -> discord.VoiceChannel:
    """トリガーVCと同じカテゴリに一時VCを作る(募集VCと通常VCで共通)"""
    trigger = client.get_channel(TRIGGER_CHANNEL_ID)
    category = trigger.category if trigger else None
    async with create_lock:
        siblings = category.voice_channels if category else guild.voice_channels
        n = next_number(siblings)
        vc = await guild.create_voice_channel(
            name=f"#{n} - {name}"[:100],
            category=category,
            bitrate=trigger.bitrate if trigger else 64000,
            user_limit=user_limit,
            reason="temp VC",
        )
        temp_channels.add(vc.id)
    return vc


async def delete_if_unused(vc_id: int, delay: int):
    """作ったのに誰も入らなかったVCを後で消す"""
    await asyncio.sleep(delay)
    ch = client.get_channel(vc_id)
    if ch and vc_id in temp_channels and len(ch.members) == 0:
        temp_channels.discard(vc_id)
        try:
            await ch.delete(reason="temp VC unused")
        except discord.NotFound:
            pass


# ===================== VCコントロールパネル =====================
OWNER_RE = re.compile(r"オーナーID: (\d+)")


def panel_embed(owner_id: int, locked: bool = False) -> discord.Embed:
    e = discord.Embed(
        title="🎛 VCコントロールパネル",
        description=f"オーナー: <@{owner_id}>\n操作できるのはオーナーだけです。\n"
                    "オーナーがいなくなったら、VCにいる人が🙋で引き継げます。",
        color=discord.Color.blurple(),
    )
    e.add_field(name="状態", value="🔒 ロック中" if locked else "🔓 開放中")
    e.set_footer(text=f"オーナーID: {owner_id}")
    return e


def parse_panel(message: discord.Message) -> tuple[int, bool]:
    e = message.embeds[0]
    owner_id = int(OWNER_RE.search(e.footer.text or "")[1])
    locked = "ロック中" in e.fields[0].value
    return owner_id, locked


async def send_panel(vc: discord.VoiceChannel, owner_id: int):
    """VCのテキストチャットにパネルを出す"""
    try:
        msg = await vc.send(
            f"<@{owner_id}> VCの設定はここからできます",
            embed=panel_embed(owner_id),
            view=VCPanelView(),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except discord.HTTPException:
        return
    vc_owners[vc.id] = owner_id
    vc_panels[vc.id] = msg.id


async def set_owner(vc: discord.VoiceChannel, panel: discord.Message, new_owner: discord.Member):
    _, locked = parse_panel(panel)
    await panel.edit(embed=panel_embed(new_owner.id, locked))
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
            _, locked = parse_panel(self.panel)
            if locked:  # ロック中なら個別の接続許可を消して戻れなくする
                await self.vc.set_permissions(target, overwrite=None)
            await interaction.response.edit_message(content=f"{target.display_name} を切断しました", view=None)
        else:
            await set_owner(self.vc, self.panel, target)
            await interaction.response.edit_message(content=f"{target.display_name} にオーナーを譲りました", view=None)


class VCPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def check(self, interaction: discord.Interaction, owner_only: bool = True):
        vc = interaction.channel
        if not isinstance(vc, discord.VoiceChannel) or vc.id not in temp_channels:
            await interaction.response.send_message("このパネルはもう使えません", ephemeral=True)
            return None
        owner_id, locked = parse_panel(interaction.message)
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
        await interaction.message.edit(embed=panel_embed(owner_id, not locked))
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


# ===================== 通知ロールパネル =====================
class NotifyRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 再起動後もボタンが動く

    async def toggle(self, interaction: discord.Interaction, role_id: int):
        role = interaction.guild.get_role(role_id)
        if role is None:
            return await interaction.response.send_message("ロールが未設定です(管理者に連絡してください)", ephemeral=True)
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="通知OFF")
                msg = f"{role.name} の通知をOFFにしました"
            else:
                await member.add_roles(role, reason="通知ON")
                msg = f"{role.name} の通知をONにしました"
        except discord.Forbidden:
            msg = "Botの権限が足りません(ロールの管理 / ロールの順番を確認)"
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="フィールドボス", emoji="🐺", style=discord.ButtonStyle.primary, custom_id="notify:boss")
    async def boss(self, interaction, button):
        await self.toggle(interaction, BOSS_ROLE_ID)

    @discord.ui.button(label="結界", emoji="🔮", style=discord.ButtonStyle.primary, custom_id="notify:barrier")
    async def barrier(self, interaction, button):
        await self.toggle(interaction, BARRIER_ROLE_ID)

    @discord.ui.button(label="週課リマインド", emoji="📅", style=discord.ButtonStyle.secondary, custom_id="notify:weekly")
    async def weekly(self, interaction, button):
        await self.toggle(interaction, WEEKLY_ROLE_ID)


# ===================== パーティ募集 =====================
FOOTER_RE = re.compile(r"主催者ID: (\d+) / 定員: (\d+)")


def parse_party(message: discord.Message):
    embed = message.embeds[0]
    m = FOOTER_RE.search(embed.footer.text or "")
    host_id, max_n = int(m[1]), int(m[2])
    members = [int(x) for x in re.findall(r"<@!?(\d+)>", embed.fields[0].value)]
    return embed, host_id, max_n, members


def render_members(embed: discord.Embed, members: list[int], max_n: int):
    status = " 満員" if len(members) >= max_n else ""
    embed.set_field_at(
        0,
        name=f"メンバー ({len(members)}/{max_n}){status}",
        value="\n".join(f"<@{u}>" for u in members) or "なし",
        inline=False,
    )


def vc_field_index(embed: discord.Embed):
    for i, f in enumerate(embed.fields):
        if f.name == "VC":
            return i
    return None


class PartyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="参加", emoji="✋", style=discord.ButtonStyle.success, custom_id="party:join")
    async def join(self, interaction: discord.Interaction, button):
        async with party_lock:
            embed, host_id, max_n, members = parse_party(interaction.message)
            uid = interaction.user.id
            if uid in members:
                return await interaction.response.send_message("すでに参加しています", ephemeral=True)
            if len(members) >= max_n:
                return await interaction.response.send_message("満員です", ephemeral=True)
            members.append(uid)
            render_members(embed, members, max_n)
            await interaction.response.edit_message(embed=embed)

    @discord.ui.button(label="抜ける", emoji="👋", style=discord.ButtonStyle.secondary, custom_id="party:leave")
    async def leave(self, interaction: discord.Interaction, button):
        async with party_lock:
            embed, host_id, max_n, members = parse_party(interaction.message)
            uid = interaction.user.id
            if uid == host_id:
                return await interaction.response.send_message("主催者は「締切」を使ってください", ephemeral=True)
            if uid not in members:
                return await interaction.response.send_message("参加していません", ephemeral=True)
            members.remove(uid)
            render_members(embed, members, max_n)
            await interaction.response.edit_message(embed=embed)

    @discord.ui.button(label="VC作成", emoji="🔊", style=discord.ButtonStyle.primary, custom_id="party:vc")
    async def vc(self, interaction: discord.Interaction, button):
        embed, host_id, max_n, members = parse_party(interaction.message)
        if interaction.user.id not in members:
            return await interaction.response.send_message("参加者だけがVCを作れます", ephemeral=True)

        idx = vc_field_index(embed)
        if idx is not None:
            m = re.search(r"<#(\d+)>", embed.fields[idx].value)
            existing = client.get_channel(int(m[1])) if m else None
            if existing:
                return await interaction.response.send_message(f"VCはこちら → {existing.mention}", ephemeral=True)

        await interaction.response.defer()
        title = (embed.title or "パーティ").replace("🎯 ", "")
        vc = await create_temp_vc(interaction.guild, title, user_limit=min(max_n, 99))
        await send_panel(vc, interaction.user.id)
        if idx is None:
            embed.add_field(name="VC", value=vc.mention, inline=False)
        else:
            embed.set_field_at(idx, name="VC", value=vc.mention, inline=False)
        await interaction.edit_original_response(embed=embed)
        await interaction.followup.send(
            f"{vc.mention} を作成しました！ " + " ".join(f"<@{u}>" for u in members),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        asyncio.create_task(delete_if_unused(vc.id, 300))  # 5分誰も入らなければ削除

    @discord.ui.button(label="締切", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="party:close")
    async def close(self, interaction: discord.Interaction, button):
        embed, host_id, max_n, members = parse_party(interaction.message)
        if interaction.user.id != host_id and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("主催者だけが締め切れます", ephemeral=True)
        embed.title = "【締切】" + (embed.title or "")
        embed.color = discord.Color.dark_grey()
        await interaction.response.edit_message(embed=embed, view=None)


# ===================== Client / コマンド =====================
class GuildBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.synced = False

    async def setup_hook(self):
        self.add_view(NotifyRoleView())
        self.add_view(PartyView())
        self.add_view(VCPanelView())
        boss_notice.start()
        weekly_notice.start()
        if BARRIER_ENABLED:
            barrier_notice.start()


client = GuildBot()


@client.tree.command(name="募集", description="パーティ募集を作成します")
@app_commands.guild_only()
@app_commands.rename(content="内容", size="人数", start="開始", note="メモ")
@app_commands.describe(
    content="アビス / レイド など(自由入力OK)",
    size="主催者を含めた定員",
    start="開始予定(例: 21:00〜、今すぐ)",
    note="ルーン条件・役割など",
)
async def party(
    interaction: discord.Interaction,
    content: str,
    size: app_commands.Range[int, 2, 20] = 4,
    start: str = "今すぐ",
    note: str = "",
):
    desc = f"主催: {interaction.user.mention}\n開始: {start}"
    if note:
        desc += f"\nメモ: {note}"
    embed = discord.Embed(title=f"🎯 {content}", description=desc, color=discord.Color.teal())
    embed.add_field(name="", value="", inline=False)
    render_members(embed, [interaction.user.id], size)
    embed.set_footer(text=f"主催者ID: {interaction.user.id} / 定員: {size}")
    await interaction.response.send_message(embed=embed, view=PartyView())


@party.autocomplete("content")
async def party_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=p, value=p) for p in PARTY_PRESETS if current in p][:25]


def next_boss_time(now: datetime) -> tuple[datetime, bool]:
    """次(または出現中)のフィールドボス時刻と、今出現中かどうか"""
    for h in FIELD_BOSS_HOURS:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if now < t + timedelta(minutes=FIELD_BOSS_DURATION_MIN):
            return t, now >= t
    t = (now + timedelta(days=1)).replace(hour=FIELD_BOSS_HOURS[0], minute=0, second=0, microsecond=0)
    return t, False


def next_weekly_reset(now: datetime) -> datetime:
    wd, h = WEEKLY_RESET
    t = now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(days=(wd - now.weekday()) % 7)
    if t <= now:
        t += timedelta(days=7)
    return t


@client.tree.command(name="ボス", description="次のフィールドボス・結界・週間リセットの時間を表示")
async def boss_info(interaction: discord.Interaction):
    now = datetime.now(JST)
    boss, active = next_boss_time(now)
    barrier = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    reset = next_weekly_reset(now)

    def ts(d: datetime) -> str:
        u = int(d.timestamp())
        return f"<t:{u}:t>(<t:{u}:R>)"

    boss_line = (f"🟢 出現中!{ts(boss + timedelta(minutes=FIELD_BOSS_DURATION_MIN))}まで"
                 if active else ts(boss))
    embed = discord.Embed(title="⏰ タイムテーブル", color=discord.Color.gold())
    embed.add_field(name="🐺 フィールドボス", value=boss_line, inline=False)
    embed.add_field(name="🔮 不吉な召喚の結界", value=ts(barrier), inline=False)
    embed.add_field(name="📅 週間リセット", value=ts(reset), inline=False)
    embed.set_footer(text="韓国版の仕様をもとにしています")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="通知パネル", description="通知ロールの切り替えパネルを設置(管理者用)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def notify_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔔 通知設定",
        description="ボタンを押すと通知のON/OFFを切り替えられます。\n"
                    "🐺 フィールドボス出現5分前\n🔮 結界出現3分前(毎時)\n📅 日曜夜の週課リマインド",
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(embed=embed, view=NotifyRoleView())
    await interaction.response.send_message("設置しました", ephemeral=True)


# ===================== 定期通知 =====================
async def notify(text: str, role_id: int, delete_after: float | None = None):
    ch = client.get_channel(NOTIFY_CHANNEL_ID)
    if ch is None:
        return
    mention = f"<@&{role_id}> " if role_id else ""
    await ch.send(
        mention + text,
        delete_after=delete_after,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )


@tasks.loop(time=[before(h, FIELD_BOSS_NOTICE_MIN) for h in FIELD_BOSS_HOURS])
async def boss_notice():
    spawn = datetime.now(JST) + timedelta(minutes=FIELD_BOSS_NOTICE_MIN)
    spawn = spawn.replace(second=0, microsecond=0)
    await notify(
        f"🐺 **フィールドボス**が <t:{int(spawn.timestamp())}:R> に出現！(出現後{FIELD_BOSS_DURATION_MIN}分間討伐可)",
        BOSS_ROLE_ID,
        delete_after=(FIELD_BOSS_NOTICE_MIN + FIELD_BOSS_DURATION_MIN) * 60,
    )


@tasks.loop(time=[before(h, BARRIER_NOTICE_MIN) for h in range(24)])
async def barrier_notice():
    await notify(f"🔮 **不吉な召喚の結界**がまもなく出現(約{BARRIER_NOTICE_MIN}分後)", BARRIER_ROLE_ID, delete_after=10 * 60)


@tasks.loop(time=[dt_time(hour=WEEKLY_REMIND[1], minute=0, tzinfo=JST)])
async def weekly_notice():
    if datetime.now(JST).weekday() != WEEKLY_REMIND[0]:
        return
    reset = next_weekly_reset(datetime.now(JST))
    await notify(
        f"📅 週間リセットは <t:{int(reset.timestamp())}:R>！フィールドボスの週報酬など、取り忘れはない？",
        WEEKLY_ROLE_ID,
    )


@boss_notice.before_loop
@barrier_notice.before_loop
@weekly_notice.before_loop
async def wait_ready():
    await client.wait_until_ready()


# ===================== イベント =====================
@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    trigger = client.get_channel(TRIGGER_CHANNEL_ID)
    if trigger is None:
        print("TRIGGER_CHANNEL_ID のチャンネルが見つかりません")
        return

    if not client.synced:  # スラッシュコマンドをこのサーバーに即時反映
        client.tree.copy_global_to(guild=trigger.guild)
        await client.tree.sync(guild=trigger.guild)
        client.synced = True

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
        new_vc = await create_temp_vc(member.guild, f"{member.display_name}のチャンネル")
        try:
            await member.move_to(new_vc)
        except discord.HTTPException:
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
