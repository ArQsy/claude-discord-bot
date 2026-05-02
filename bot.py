import os
import asyncio
import discord
import anthropic
import psycopg2
import psycopg2.extras

DISCORD_BOT_TOKEN = os.environ['DISCORD_BOT_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
DATABASE_URL = os.environ['DATABASE_URL']

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """あなたは優秀なパーソナルアシスタントです。日本語で回答してください。
ウェブ検索が必要な場合は検索ツールを使って最新情報を取得してください。
コードの作業、情報収集、調査、雑談など何でも対応します。"""

HISTORY_LIMIT = 40  # 直近何件をコンテキストに使うか


def _get_conn():
    return psycopg2.connect(DATABASE_URL)


def _init_db():
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS discord_conversations (
                    id SERIAL PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_discord_conv_channel
                ON discord_conversations(channel_id, created_at)
            """)
        conn.commit()


def _load_history(channel_id):
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT role, content FROM discord_conversations
                WHERE channel_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (channel_id, HISTORY_LIMIT))
            rows = cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _save_messages(channel_id, user_text, reply_text):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO discord_conversations (channel_id, role, content) VALUES (%s, %s, %s)",
                (channel_id, "user", user_text)
            )
            cur.execute(
                "INSERT INTO discord_conversations (channel_id, role, content) VALUES (%s, %s, %s)",
                (channel_id, "assistant", reply_text)
            )
        conn.commit()


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
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_db)
    print(f'Bot起動: {discord_client.user}')


@discord_client.event
async def on_message(message):
    if message.author == discord_client.user:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = discord_client.user in message.mentions
    if not (is_dm or is_mentioned):
        return

    channel_id = str(message.channel.id)

    user_text = message.content
    for mention in message.mentions:
        user_text = user_text.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
    user_text = user_text.strip()

    if not user_text:
        return

    async with message.channel.typing():
        try:
            loop = asyncio.get_event_loop()

            # DBから会話履歴を取得
            history = await loop.run_in_executor(None, _load_history, channel_id)
            history.append({"role": "user", "content": user_text})

            response = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8096,
                system=SYSTEM_PROMPT,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                messages=history,
            )

            reply_text = ""
            for block in response.content:
                if hasattr(block, 'text'):
                    reply_text += block.text

            if not reply_text:
                reply_text = "（応答を生成できませんでした）"

            # DBに保存
            await loop.run_in_executor(None, _save_messages, channel_id, user_text, reply_text)

            for chunk in split_message(reply_text):
                await message.reply(chunk)

        except Exception as e:
            print(f"エラー: {e}")
            await message.reply(f"エラーが発生しました: {str(e)[:200]}")


discord_client.run(DISCORD_BOT_TOKEN)
