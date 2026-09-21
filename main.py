import asyncio
import os
import re

import discord

TOKEN = os.environ["DISCORD_TOKEN"]
TRIGGER_CHANNEL_ID = int(os.environ["TRIGGER_CHANNEL_ID"])  # 「ボイスを作成」VCのID

intents = discord.Intents.default()
intents.voice_states = True

client = discord.Client(intents=intents)

NAME_RE = re.compile(r"^#(\d+) - ")  # 作成VCの名前判定用（例: #7 - Akiのチャンネル）
temp_channels: set[int] = set()  # 作成したVCのID
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


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    trigger = client.get_channel(TRIGGER_CHANNEL_ID)
    if trigger is None:
        print("TRIGGER_CHANNEL_ID のチャンネルが見つかりません")
        return
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

    # 2) 作成したVCが空になったら削除
    if before.channel and before.channel.id in temp_channels and len(before.channel.members) == 0:
        temp_channels.discard(before.channel.id)
        try:
            await before.channel.delete(reason="temp VC empty")
        except discord.NotFound:
            pass


client.run(TOKEN)
