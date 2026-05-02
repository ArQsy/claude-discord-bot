import os
import json
import asyncio
import base64
import tempfile
from datetime import datetime, timezone, timedelta
import discord
import anthropic
import psycopg2
import psycopg2.extras
import speech_recognition as sr
from pydub import AudioSegment

DISCORD_BOT_TOKEN = os.environ['DISCORD_BOT_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
DATABASE_URL = os.environ['DATABASE_URL']

JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """あなたは優秀なパーソナルアシスタントです。日本語で回答してください。
ウェブ検索が必要な場合は検索ツールを使って最新情報を取得してください。
コードの作業、情報収集、調査、雑談など何でも対応します。"""

HISTORY_LIMIT = 40
BOT_PREFIX = "**【アシスタント】**\n"

REMINDER_KEYWORDS = ["リマインド", "reminder", "通知して", "忘れないように", "教えて", "アラーム"]
MEMO_SAVE_KEYWORDS = ["メモして", "メモ：", "メモ:", "覚えておいて", "記録して", "メモ保存"]
MEMO_LIST_KEYWORDS = ["メモ見せて", "メモ一覧", "メモを教えて", "メモ確認", "メモリスト"]
RESERVATION_KEYWORDS = ["予約", "席を取って", "予約して", "予約したい", "ご予約", "席の予約"]
TRAVEL_KEYWORDS = ["航空券", "飛行機", "ホテル", "宿", "ツアー", "旅行", "格安", "安いフライト", "旅館", "パック旅行", "ANA", "JAL", "LCC"]

TRAVEL_SYSTEM_PROMPT = """あなたは旅行・交通のお得情報を探すアシスタントです。
ユーザーの条件（出発地・目的地・日程・人数・予算）を整理し、ウェブ検索で最安値に近い選択肢を見つけてください。

以下の形式で回答してください：

**✈️ / 🏨 / 🗺️ 検索条件まとめ**
- 出発地・目的地・日程・人数・予算等を箇条書きで整理

**💰 おすすめ選択肢（安い順）**
見つかった選択肢を価格・特徴・URLつきで3〜5件紹介

**🔍 もっと探すなら**
Skyscanner・じゃらん・楽天トラベル・HIS・トリバゴなど条件に合った比較サイトのURLを案内

航空券はANA・JAL・LCCも含めて比較し、ホテルはじゃらん・楽天トラベル・Booking.com等を横断してください。
ツアーパックはHIS・JTB・エクスペディアなども確認してください。
ウェブ検索を積極的に使って最新の料金・空き状況を調べてください。"""

RESERVATION_SYSTEM_PROMPT = """あなたはレストラン予約のアシスタントです。
ユーザーのメッセージから予約情報を整理し、予約ページを探して案内してください。

以下の形式で回答してください：

**🍽️ 予約情報まとめ**
- お店：〇〇
- 日時：〇月〇日 〇時
- 人数：〇名
- 名前：（未指定の場合は「要確認」）

**🔗 予約ページ**
（ぐるなび・食べログ・ホットペッパー等で見つけた予約URLを貼る）

**📋 入力内容**
予約フォームに入力する内容を箇条書きで整理する

予約ページが見つからない場合は「公式サイト」「電話番号」を案内してください。
ウェブ検索を積極的に使って最新の予約ページURLを見つけてください。"""


# ───────────── DB ─────────────

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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT,
                    message TEXT NOT NULL,
                    remind_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    fired BOOLEAN DEFAULT FALSE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS memos (
                    id SERIAL PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
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


def _save_reminder(channel_id, user_id, username, message, remind_at):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reminders (channel_id, user_id, username, message, remind_at) VALUES (%s, %s, %s, %s, %s)",
                (channel_id, user_id, username, message, remind_at)
            )
        conn.commit()


def _get_due_reminders():
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT * FROM reminders
                WHERE fired = FALSE AND remind_at <= %s
            """, (now_utc,))
            return cur.fetchall()


def _mark_fired(reminder_id):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reminders SET fired = TRUE WHERE id = %s", (reminder_id,))
        conn.commit()


def _save_memo(channel_id, user_id, username, content):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memos (channel_id, user_id, username, content) VALUES (%s, %s, %s, %s)",
                (channel_id, user_id, username, content)
            )
        conn.commit()


def _get_memos(channel_id, limit=10):
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT content, created_at FROM memos
                WHERE channel_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (channel_id, limit))
            return cur.fetchall()


# ───────────── インテント判定 ─────────────

def _detect_intent(text):
    if any(k in text for k in MEMO_LIST_KEYWORDS):
        return "memo_list"
    if any(k in text for k in MEMO_SAVE_KEYWORDS):
        return "memo_save"
    if any(k in text for k in TRAVEL_KEYWORDS):
        return "travel"
    if any(k in text for k in RESERVATION_KEYWORDS):
        return "reservation"
    if any(k in text for k in REMINDER_KEYWORDS):
        return "reminder"
    return "chat"


def _parse_reminder(text):
    """Claudeで日時とメッセージを抽出（Haiku使用でコスト節約）"""
    now_jst = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=f"""現在の日時（JST）: {now_jst}
ユーザーのメッセージからリマインダーの日時と内容を抽出してください。
必ずJSON形式のみで返してください（説明文は不要）:
{{"remind_at": "YYYY-MM-DDTHH:MM:SS", "message": "リマインダー内容"}}
日時が不明な場合: {{"error": "日時が不明です"}}""",
        messages=[{"role": "user", "content": text}]
    )
    raw = response.content[0].text.strip()
    return json.loads(raw)


# ───────────── ユーティリティ ─────────────

def _transcribe_sync(audio_bytes, suffix='.ogg'):
    tmp_ogg = tmp_wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp_ogg = f.name
        tmp_wav = tmp_ogg.replace(suffix, '.wav')
        AudioSegment.from_file(tmp_ogg).export(tmp_wav, format='wav')
        r = sr.Recognizer()
        with sr.AudioFile(tmp_wav) as source:
            audio_data = r.record(source)
        return r.recognize_google(audio_data, language='ja-JP')
    finally:
        for p in [tmp_ogg, tmp_wav]:
            if p and os.path.exists(p):
                os.unlink(p)


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


# ───────────── バックグラウンド: リマインダーチェック ─────────────

async def reminder_checker():
    await discord_client.wait_until_ready()
    while not discord_client.is_closed():
        try:
            loop = asyncio.get_event_loop()
            due = await loop.run_in_executor(None, _get_due_reminders)
            for r in due:
                channel = discord_client.get_channel(int(r['channel_id']))
                if channel:
                    remind_jst = r['remind_at'].replace(tzinfo=timezone.utc).astimezone(JST)
                    time_str = remind_jst.strftime("%-m月%-d日 %H:%M")
                    await channel.send(
                        f"{BOT_PREFIX}⏰ **リマインダー**\n"
                        f"{r['username']}さん、{time_str}のリマインダーです！\n\n"
                        f"📝 {r['message']}"
                    )
                await loop.run_in_executor(None, _mark_fired, r['id'])
        except Exception as e:
            print(f"リマインダーチェックエラー: {e}")
        await asyncio.sleep(60)


# ───────────── イベント ─────────────

@discord_client.event
async def on_ready():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_db)
    discord_client.loop.create_task(reminder_checker())
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

            # ボイスメッセージ文字起こし
            audio_attachment = next(
                (a for a in message.attachments if a.content_type and a.content_type.startswith('audio/')),
                None
            )
            if audio_attachment:
                audio_bytes = await audio_attachment.read()
                suffix = '.ogg' if 'ogg' in (audio_attachment.content_type or '') else '.wav'
                try:
                    transcribed = await loop.run_in_executor(None, _transcribe_sync, audio_bytes, suffix)
                    user_text = f"{transcribed}" if not user_text else f"{user_text}\n{transcribed}"
                except Exception as e:
                    print(f"文字起こし失敗: {e}")
                    await message.reply(f"{BOT_PREFIX}ボイスメッセージの文字起こしに失敗しました。")
                    return

            # 画像処理
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

            # インテント判定
            intent = _detect_intent(user_text)

            # ── メモ一覧 ──
            if intent == "memo_list":
                rows = await loop.run_in_executor(None, _get_memos, channel_id)
                if not rows:
                    await message.reply(f"{BOT_PREFIX}メモはまだありません。")
                    return
                lines = []
                for i, r in enumerate(rows, 1):
                    jst_time = r['created_at'].replace(tzinfo=timezone.utc).astimezone(JST)
                    lines.append(f"**{i}.** {jst_time.strftime('%-m/%-d %H:%M')} — {r['content']}")
                await message.reply(f"{BOT_PREFIX}📝 **メモ一覧**\n" + "\n".join(lines))
                return

            # ── メモ保存 ──
            if intent == "memo_save":
                content = user_text
                for kw in MEMO_SAVE_KEYWORDS:
                    content = content.replace(kw, "").strip()
                content = content.lstrip("：:").strip()
                if not content:
                    await message.reply(f"{BOT_PREFIX}メモの内容を教えてください。")
                    return
                await loop.run_in_executor(None, _save_memo, channel_id, str(message.author.id), str(message.author), content)
                await message.reply(f"{BOT_PREFIX}✅ メモを保存しました！\n📝 {content}")
                return

            # ── リマインダー ──
            if intent == "reminder":
                try:
                    parsed = await loop.run_in_executor(None, _parse_reminder, user_text)
                    if "error" in parsed:
                        await message.reply(f"{BOT_PREFIX}⚠️ 日時が読み取れませんでした。「5月10日の15時にリマインドして」のように教えてください。")
                        return
                    remind_at_jst = datetime.fromisoformat(parsed["remind_at"]).replace(tzinfo=JST)
                    remind_at_utc = remind_at_jst.astimezone(timezone.utc).replace(tzinfo=None)
                    await loop.run_in_executor(
                        None, _save_reminder,
                        channel_id, str(message.author.id), str(message.author.display_name),
                        parsed["message"], remind_at_utc
                    )
                    time_str = remind_at_jst.strftime("%-m月%-d日 %H:%M")
                    await message.reply(f"{BOT_PREFIX}⏰ リマインダーを設定しました！\n📅 {time_str}\n📝 {parsed['message']}")
                except Exception as e:
                    print(f"リマインダー設定エラー: {e}")
                    await message.reply(f"{BOT_PREFIX}リマインダーの設定に失敗しました。日時をもう少し具体的に教えてください。")
                return

            # ── 旅行検索 ──
            if intent == "travel":
                response = anthropic_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    system=TRAVEL_SYSTEM_PROMPT,
                    tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                    messages=[{"role": "user", "content": user_text}],
                )
                reply_text = ""
                for block in response.content:
                    if hasattr(block, 'text'):
                        reply_text += block.text
                if not reply_text:
                    reply_text = "検索に失敗しました。出発地・目的地・日程・人数をもう少し詳しく教えてください。"
                for chunk in split_message(BOT_PREFIX + reply_text):
                    await message.reply(chunk)
                return

            # ── 予約サポート ──
            if intent == "reservation":
                response = anthropic_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    system=RESERVATION_SYSTEM_PROMPT,
                    tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                    messages=[{"role": "user", "content": user_text}],
                )
                reply_text = ""
                for block in response.content:
                    if hasattr(block, 'text'):
                        reply_text += block.text
                if not reply_text:
                    reply_text = "予約ページの検索に失敗しました。お店名と日時をもう少し詳しく教えてください。"
                for chunk in split_message(BOT_PREFIX + reply_text):
                    await message.reply(chunk)
                return

            # ── 通常チャット ──
            history = await loop.run_in_executor(None, _load_history, channel_id)
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

            for chunk in split_message(BOT_PREFIX + reply_text):
                await message.reply(chunk)

        except Exception as e:
            print(f"エラー: {e}")
            await message.reply(f"{BOT_PREFIX}エラーが発生しました: {str(e)[:200]}")


discord_client.run(DISCORD_BOT_TOKEN)
