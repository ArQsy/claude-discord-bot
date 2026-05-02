import os
import asyncio
import base64
import tempfile
import discord
import anthropic
import psycopg2
import psycopg2.extras
import speech_recognition as sr
from pydub import AudioSegment

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

HISTORY_LIMIT = 40
BOT_PREFIX = "**【アシスタント】**\n"


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


def _transcribe_sync(audio_bytes, suffix='.ogg'):
    tmp_ogg = None
    tmp_wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp_ogg = f.name

        tmp_wav = tmp_ogg.replace(suffix, '.wav')
        audio = AudioSegment.from_file(tmp_ogg)
        audio.export(tmp_wav, format='wav')

        r = sr.Recognizer()
        with sr.AudioFile(tmp_wav) as source:
            audio_data = r.record(source)
        return r.recognize_google(audio_data, language='ja-JP')
    finally:
        for path in [tmp_ogg, tmp_wav]:
            if path and os.path.exists(path):
                os.unlink(path)


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
    has_audio = any(a.content_type and a.content_type.startswith('audio/') for a in message.attachments)
    has_image = any(a.content_type and a.content_type.startswith('image/') for a in message.attachments)

    if not (is_dm or is_mentioned or has_audio or has_image):
        return

    channel_id = str(message.channel.id)
    loop = asyncio.get_event_loop()

    async with message.channel.typing():
        try:
            user_text = message.content
            for mention in message.mentions:
                user_text = user_text.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
            user_text = user_text.strip()

            # ボイスメッセージの文字起こし
            audio_attachment = next(
                (a for a in message.attachments if a.content_type and a.content_type.startswith('audio/')),
                None
            )
            if audio_attachment:
                audio_bytes = await audio_attachment.read()
                suffix = '.ogg' if 'ogg' in (audio_attachment.content_type or '') else '.wav'
                try:
                    transcribed = await loop.run_in_executor(None, _transcribe_sync, audio_bytes, suffix)
                    user_text = f"[ボイスメッセージ] {transcribed}" if not user_text else f"{user_text}\n[ボイスメッセージ] {transcribed}"
                except Exception as e:
                    print(f"文字起こし失敗: {e}")
                    await message.reply(f"{BOT_PREFIX}ボイスメッセージの文字起こしに失敗しました。")
                    return

            # 画像の処理
            image_attachments = [a for a in message.attachments if a.content_type and a.content_type.startswith('image/')]
            image_contents = []
            for img in image_attachments:
                img_bytes = await img.read()
                b64 = base64.standard_b64encode(img_bytes).decode('utf-8')
                image_contents.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": img.content_type.split(';')[0], "data": b64}
                })

            if not user_text and not image_contents:
                return

            history = await loop.run_in_executor(None, _load_history, channel_id)

            # 画像がある場合はコンテンツをリスト形式で構築
            if image_contents:
                user_content = image_contents + ([{"type": "text", "text": user_text}] if user_text else [{"type": "text", "text": "この画像について教えてください。"}])
            else:
                user_content = user_text

            history.append({"role": "user", "content": user_content})

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

            save_text = user_text if user_text else f"[画像 {len(image_contents)}枚]"
            await loop.run_in_executor(None, _save_messages, channel_id, save_text, reply_text)

            full_reply = BOT_PREFIX + reply_text
            for chunk in split_message(full_reply):
                await message.reply(chunk)

        except Exception as e:
            print(f"エラー: {e}")
            await message.reply(f"{BOT_PREFIX}エラーが発生しました: {str(e)[:200]}")


discord_client.run(DISCORD_BOT_TOKEN)
