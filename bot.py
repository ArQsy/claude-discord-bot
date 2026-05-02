import os
import re
import json
import math
import asyncio
import base64
import tempfile
from datetime import datetime, timezone, timedelta
import discord
import anthropic
import psycopg2
import psycopg2.extras
import requests
import speech_recognition as sr
from pydub import AudioSegment

DISCORD_BOT_TOKEN = os.environ['DISCORD_BOT_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
DATABASE_URL = os.environ['DATABASE_URL']
GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')

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
MAP_KEYWORDS = ["近く", "現在地", "付近", "周辺", "近い", "近くの", "今開いてる", "営業中", "地図", "マップ"]

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
    if any(k in text for k in MAP_KEYWORDS) and ('maps.google' in text or 'goo.gl' in text or 'maps.app' in text or any(k in text for k in ["近くの", "付近の", "周辺の"])):
        return "map_search"
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

def _extract_coords(text):
    """テキスト中のGoogle Maps URLから座標を抽出"""
    patterns = [
        r'[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'/@(-?\d+\.\d+),(-?\d+\.\d+)',
        r'll=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)',
    ]
    urls = re.findall(r'https?://[^\s]+', text)
    for url in urls:
        # 短縮URLはリダイレクト先を取得
        if 'goo.gl' in url or 'maps.app' in url:
            try:
                r = requests.get(url, allow_redirects=True, timeout=5)
                url = r.url
            except Exception:
                pass
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                return float(m.group(1)), float(m.group(2))
    return None, None


def _geocode(address):
    """住所・地名を座標に変換"""
    if not GOOGLE_MAPS_API_KEY:
        return None, None
    r = requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={"address": address, "language": "ja", "key": GOOGLE_MAPS_API_KEY},
        timeout=10
    )
    data = r.json()
    if data.get("results"):
        loc = data["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]
    return None, None


def _nearby_search(lat, lng, keyword, radius=1000, open_now=True):
    """Places API で周辺スポットを検索"""
    params = {
        "location": f"{lat},{lng}",
        "radius": radius,
        "keyword": keyword,
        "language": "ja",
        "key": GOOGLE_MAPS_API_KEY,
    }
    if open_now:
        params["opennow"] = "true"
    r = requests.get(
        "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
        params=params, timeout=10
    )
    return r.json().get("results", [])


def _haversine(lat1, lng1, lat2, lng2):
    """2点間の距離(m)を計算"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def _format_places(places, lat, lng, limit=8):
    """検索結果を整形"""
    lines = []
    for i, p in enumerate(places[:limit], 1):
        name = p.get("name", "不明")
        rating = p.get("rating", "-")
        user_ratings = p.get("user_ratings_total", 0)
        vicinity = p.get("vicinity", "")
        place_id = p.get("place_id", "")
        loc = p.get("geometry", {}).get("location", {})
        dist = int(_haversine(lat, lng, loc.get("lat", lat), loc.get("lng", lng)))
        maps_url = f"https://www.google.com/maps/place/?q=place_id:{place_id}"
        open_now = p.get("opening_hours", {}).get("open_now")
        status = "🟢 営業中" if open_now else ("🔴 営業時間外" if open_now is False else "")
        lines.append(
            f"**{i}. {name}** ⭐{rating}（{user_ratings}件）{' ' + status if status else ''}\n"
            f"　📍 {vicinity}（約{dist}m）\n"
            f"　🔗 {maps_url}"
        )
    return "\n\n".join(lines)


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

            # ── 地図・周辺検索 ──
            if intent == "map_search":
                if not GOOGLE_MAPS_API_KEY:
                    await message.reply(f"{BOT_PREFIX}Google Maps APIキーが設定されていません。")
                    return

                lat, lng = _extract_coords(user_text)

                # URLがなければ地名をジオコード
                if lat is None:
                    place_match = re.search(r'([一-鿿぀-ヿ\w]+駅|[一-鿿]{2,})', user_text)
                    if place_match:
                        lat, lng = await loop.run_in_executor(None, _geocode, place_match.group(1))

                if lat is None:
                    await message.reply(f"{BOT_PREFIX}📍 現在地のGoogle MapsリンクをコピーしてDiscordに貼り付けてください。\n例：https://maps.google.com/?q=35.6762,139.6503")
                    return

                # 検索キーワード抽出（バー・レストラン・コンビニ等）
                keyword_match = re.search(r'(バー|居酒屋|レストラン|カフェ|コンビニ|薬局|スーパー|ラーメン|寿司|焼肉|ホテル|銭湯|[一-鿿]{1,6})', user_text)
                keyword = keyword_match.group(1) if keyword_match else "飲食店"
                open_now = any(k in user_text for k in ["営業中", "今開いてる", "今やってる", "開いてる"])

                radius_match = re.search(r'(\d+)\s*km', user_text)
                radius = int(float(radius_match.group(1)) * 1000) if radius_match else 1000

                places = await loop.run_in_executor(None, _nearby_search, lat, lng, keyword, radius, open_now)
                if not places:
                    await message.reply(f"{BOT_PREFIX}半径{radius}m以内に「{keyword}」は見つかりませんでした。範囲を広げるか別のキーワードをお試しください。")
                    return

                body = _format_places(places, lat, lng)
                open_label = "（営業中のみ）" if open_now else ""
                header = f"📍 現在地から半径{radius}m以内の**{keyword}**{open_label}\n\n"
                for chunk in split_message(BOT_PREFIX + header + body):
                    await message.reply(chunk)
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
