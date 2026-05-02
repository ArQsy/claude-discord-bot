import os
import discord
import anthropic

DISCORD_BOT_TOKEN = os.environ['DISCORD_BOT_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# チャンネルごとの会話履歴
histories = {}

SYSTEM_PROMPT = """あなたは優秀なパーソナルアシスタントです。日本語で回答してください。
ウェブ検索が必要な場合は検索ツールを使って最新情報を取得してください。
コードの作業、情報収集、調査、雑談など何でも対応します。"""

def split_message(text, limit=2000):
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind('\n', 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    return chunks

@discord_client.event
async def on_ready():
    print(f'Bot起動: {discord_client.user}')

@discord_client.event
async def on_message(message):
    if message.author == discord_client.user:
        return

    # メンション or DMのみ応答
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = discord_client.user in message.mentions
    if not (is_dm or is_mentioned):
        return

    channel_id = str(message.channel.id)
    if channel_id not in histories:
        histories[channel_id] = []

    user_text = message.content
    for mention in message.mentions:
        user_text = user_text.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
    user_text = user_text.strip()

    if not user_text:
        return

    histories[channel_id].append({"role": "user", "content": user_text})

    async with message.channel.typing():
        try:
            response = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8096,
                system=SYSTEM_PROMPT,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                messages=histories[channel_id],
            )

            reply_text = ""
            for block in response.content:
                if hasattr(block, 'text'):
                    reply_text += block.text

            if not reply_text:
                reply_text = "（応答を生成できませんでした）"

            histories[channel_id].append({"role": "assistant", "content": reply_text})

            # 履歴が長くなりすぎたら古いものを削除（直近20件を保持）
            if len(histories[channel_id]) > 40:
                histories[channel_id] = histories[channel_id][-40:]

            for chunk in split_message(reply_text):
                await message.reply(chunk)

        except Exception as e:
            print(f"エラー: {e}")
            await message.reply(f"エラーが発生しました: {str(e)[:200]}")

discord_client.run(DISCORD_BOT_TOKEN)
