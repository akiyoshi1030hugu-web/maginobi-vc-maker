import os
import discord

TOKEN = os.environ["DISCORD_TOKEN"]
TRIGGER_CHANNEL_ID = int(os.environ["TRIGGER_CHANNEL_ID"])  # 「➕ VC作成」用のVCのID
PREFIX = os.getenv("TEMP_VC_PREFIX", "🔊 ")  # 自動作成VCの名前の先頭

intents = discord.Intents.default()
intents.voice_states = True

client = discord.Client(intents=intents)
temp_channels: set[int] = set()  # 作成したVCのID


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    # 再起動前に作られて空のまま残ったVCを掃除
    trigger = client.get_channel(TRIGGER_CHANNEL_ID)
    if trigger is None:
        print("TRIGGER_CHANNEL_ID のチャンネルが見つかりません")
        return
    for ch in trigger.guild.voice_channels:
        if (
            ch.id != TRIGGER_CHANNEL_ID
            and ch.category == trigger.category
            and ch.name.startswith(PREFIX)
        ):
            if len(ch.members) == 0:
                await ch.delete(reason="temp VC cleanup")
            else:
                temp_channels.add(ch.id)


@client.event
async def on_voice_state_update(member, before, after):
    # 1) トリガーVCに入ったら新規VCを作って移動
    if after.channel and after.channel.id == TRIGGER_CHANNEL_ID:
        trigger = after.channel
        new_vc = await trigger.guild.create_voice_channel(
            name=f"{PREFIX}{member.display_name}",
            category=trigger.category,
            bitrate=trigger.bitrate,
            reason=f"temp VC for {member}",
        )
        temp_channels.add(new_vc.id)
        try:
            await member.move_to(new_vc)
        except discord.HTTPException:
            # 移動前に本人が退出した場合など
            await new_vc.delete()
            temp_channels.discard(new_vc.id)

    # 2) 作成したVCが空になったら削除
    if before.channel and before.channel.id in temp_channels:
        if len(before.channel.members) == 0:
            temp_channels.discard(before.channel.id)
            try:
                await before.channel.delete(reason="temp VC empty")
            except discord.NotFound:
                pass


client.run(TOKEN)
