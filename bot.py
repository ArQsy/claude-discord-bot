import os
import re
import json
import math
import uuid
import asyncio
import base64
import tempfile
import time
from datetime import datetime, timezone, timedelta
import discord
import anthropic
import psycopg2
import psycopg2.extras
import requests
from aiohttp import web
import speech_recognition as sr
from pydub import AudioSegment

DISCORD_BOT_TOKEN = os.environ['DISCORD_BOT_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
DATABASE_URL = os.environ['DATABASE_URL']
GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')
AQUAVOICE_API_KEY = os.environ.get('AQUAVOICE_API_KEY', '')
PORT = int(os.environ.get('PORT', 8080))
_raw_base_url = os.environ.get('BOT_BASE_URL', '')
BOT_BASE_URL = f"https://{_raw_base_url}" if _raw_base_url and not _raw_base_url.startswith('http') else _raw_base_url

# 位置情報リクエストの一時保存 {token: (channel_id, keyword, radius, open_now, expires_at)}
_pending_locations: dict[str, tuple] = {}
_PENDING_LOCATION_TTL = 600  # 10分

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
    # 予約はtravelより先に評価（「旅行の予約」を予約として扱う）
    if any(k in text for k in RESERVATION_KEYWORDS):
        return "reservation"
    if any(k in text for k in TRAVEL_KEYWORDS):
        return "travel"
    # map_search: マップ系キーワードがあればURLなしでも検出
    if any(k in text for k in MAP_KEYWORDS):
        return "map_search"
    if any(k in text for k in REMINDER_KEYWORDS):
        return "reminder"
    return "chat"


def _parse_reminder(text):
    """Claudeで日時とメッセージを抽出（Haiku使用でコスト節約）"""
    now_jst = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        system=f"""現在の日時（JST）: {now_jst}
ユーザーのメッセージからリマインダーの日時と内容を抽出してください。
必ずJSON形式のみで返してください（説明文は不要）:
{{"remind_at": "YYYY-MM-DDTHH:MM:SS", "message": "リマインダー内容"}}
日時が不明な場合: {{"error": "日時が不明です"}}""",
        messages=[{"role": "user", "content": text}]
    )
    raw = response.content[0].text.strip()
    # コードブロックで囲まれている場合に除去
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ───────────── ユーティリティ ─────────────

_BROWSER_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'

def _extract_coords(text):
    """テキストから座標を抽出（URL・生座標・Google Maps URL対応）"""
    # ① 生座標（例: 35.6762, 139.6503 または 35.6762,139.6503）
    m = re.search(r'(-?\d{1,3}\.\d{4,})[,\s]+(-?\d{1,3}\.\d{4,})', text)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng

    # ② URLに含まれる座標パターン
    url_patterns = [
        r'[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'/@(-?\d+\.\d+),(-?\d+\.\d+)',
        r'll=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)',
    ]
    for url in re.findall(r'https?://\S+', text):
        for pat in url_patterns:
            m = re.search(pat, url)
            if m:
                return float(m.group(1)), float(m.group(2))
        # 短縮URLのリダイレクト追跡
        if 'goo.gl' in url or 'maps.app' in url:
            try:
                r = requests.get(url, allow_redirects=True, timeout=8,
                                 headers={'User-Agent': _BROWSER_UA})
                for pat in url_patterns:
                    m = re.search(pat, r.url)
                    if m:
                        return float(m.group(1)), float(m.group(2))
            except Exception as e:
                print(f"URL展開失敗: {e}")
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


PLACE_TYPE_MAP = {
    'コンビニ': 'convenience_store',
    'レストラン': 'restaurant',
    '居酒屋': 'bar',
    'バー': 'bar',
    'カフェ': 'cafe',
    'スーパー': 'supermarket',
    '薬局': 'pharmacy',
    'ドラッグストア': 'pharmacy',
    'ドラスト': 'pharmacy',
    'ホテル': 'lodging',
    'ガソリンスタンド': 'gas_station',
    '駐車場': 'parking',
    '銭湯': 'spa',
    'カラオケ': 'karaoke',
    'ラーメン': 'restaurant',
    '寿司': 'restaurant',
    '焼肉': 'restaurant',
    'マック': 'meal_takeaway',
    'マクドナルド': 'meal_takeaway',
    'スタバ': 'cafe',
}


def _nearby_search(lat, lng, keyword, radius=2000, open_now=False):
    """Places API で周辺スポットを検索"""
    params = {
        "location": f"{lat},{lng}",
        "radius": radius,
        "language": "ja",
        "key": GOOGLE_MAPS_API_KEY,
    }
    # 日本語キーワードをAPIタイプに変換できる場合はtypeを使う
    place_type = PLACE_TYPE_MAP.get(keyword)
    if place_type:
        params["type"] = place_type
    else:
        params["keyword"] = keyword

    if open_now:
        params["opennow"] = "true"

    r = requests.get(
        "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
        params=params, timeout=10
    )
    results = r.json().get("results", [])
    print(f"Places API: {len(results)}件 (type={place_type}, keyword={keyword}, radius={radius})")

    # 結果が少なければ半径を広げて再試行
    if len(results) < 3 and radius < 5000:
        params["radius"] = 5000
        params.pop("opennow", None)
        r2 = requests.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params=params, timeout=10
        )
        results = r2.json().get("results", []) or results
        print(f"Places API (拡大): {len(results)}件")

    return results


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
    # AquaVoice APIが設定されていればそちらを優先
    if AQUAVOICE_API_KEY:
        return _transcribe_aquavoice(audio_bytes, suffix)
    # フォールバック: Google Speech Recognition
    tmp_ogg = tmp_wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp_ogg = f.name
        base = tmp_ogg[:-len(suffix)] if tmp_ogg.endswith(suffix) else tmp_ogg
        tmp_wav = base + '.wav'
        AudioSegment.from_file(tmp_ogg).export(tmp_wav, format='wav')
        r = sr.Recognizer()
        with sr.AudioFile(tmp_wav) as source:
            audio_data = r.record(source)
        return r.recognize_google(audio_data, language='ja-JP')
    finally:
        for p in [tmp_ogg, tmp_wav]:
            if p and os.path.exists(p):
                os.unlink(p)


def _transcribe_aquavoice(audio_bytes, suffix='.ogg'):
    tmp_src = tmp_wav = None
    try:
        # 元ファイルを保存
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp_src = f.name

        # WAVに変換（AquaVoiceはogg/opusを直接受け付けない場合がある）
        tmp_wav = tmp_src.replace(suffix, '.wav')
        AudioSegment.from_file(tmp_src).export(tmp_wav, format='wav')

        with open(tmp_wav, 'rb') as f:
            resp = requests.post(
                'https://api.aquavoice.com/api/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {AQUAVOICE_API_KEY}'},
                files={'file': ('audio.wav', f, 'audio/wav')},
                data={'model': 'avalon-v1.5'},
                timeout=30,
            )
        print(f"AquaVoice応答: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json().get('text', '')
    finally:
        for p in [tmp_src, tmp_wav]:
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

# ───────────── Webサーバー（位置情報共有用） ─────────────

LOCATION_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>現在地を共有</title>
<style>
  body { font-family: -apple-system, sans-serif; display: flex; flex-direction: column;
         align-items: center; justify-content: center; min-height: 100vh; margin: 0;
         background: #1a1a2e; color: #fff; text-align: center; padding: 20px; }
  h2 { margin-bottom: 8px; }
  p { color: #aaa; margin-bottom: 32px; }
  button { background: #5865F2; color: white; border: none; border-radius: 12px;
           padding: 16px 32px; font-size: 18px; cursor: pointer; }
  button:disabled { background: #444; }
  #status { margin-top: 24px; color: #aaa; }
</style>
</head>
<body>
<h2>📍 現在地を共有</h2>
<p>ボタンを押して位置情報を許可してください</p>
<button id="btn" onclick="share()">現在地を共有する</button>
<div id="status"></div>
<script>
async function share() {
  const btn = document.getElementById('btn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = '取得中...';
  try {
    const pos = await new Promise((resolve, reject) =>
      navigator.geolocation.getCurrentPosition(resolve, reject, {enableHighAccuracy: true})
    );
    const res = await fetch('/location/submit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        lat: pos.coords.latitude,
        lng: pos.coords.longitude,
        token: new URLSearchParams(location.search).get('token')
      })
    });
    if (res.ok) {
      status.textContent = '';
      document.querySelector('p').textContent = '2秒後にDiscordに戻ります';
      btn.textContent = '✅ 送信完了！';
      document.querySelector('h2').textContent = 'Discordに結果が届きます';
      setTimeout(() => {
        window.location.href = 'discord://';
        setTimeout(() => { window.close(); }, 1000);
      }, 2000);
    } else {
      status.textContent = 'エラーが発生しました';
      btn.disabled = false;
    }
  } catch(e) {
    status.textContent = '位置情報の取得に失敗しました: ' + e.message;
    btn.disabled = false;
  }
}
</script>
</body>
</html>"""


async def handle_location_page(request):
    return web.Response(text=LOCATION_HTML, content_type='text/html')


async def handle_location_submit(request):
    try:
        data = await request.json()
        token = data.get('token', '')
        lat = float(data.get('lat', 0))
        lng = float(data.get('lng', 0))

        entry = _pending_locations.get(token)
        if entry is None:
            return web.Response(status=400, text='Invalid token')
        if time.monotonic() > entry[4]:
            _pending_locations.pop(token, None)
            return web.Response(status=400, text='Token expired')

        channel_id, keyword, radius, open_now, _ = _pending_locations.pop(token)
        channel = discord_client.get_channel(int(channel_id))
        if not channel:
            return web.Response(status=400, text='Channel not found')

        places = _nearby_search(lat, lng, keyword, radius, open_now)
        if not places:
            await channel.send(f"{BOT_PREFIX}半径{radius}m以内に「{keyword}」は見つかりませんでした。")
        else:
            open_label = "（営業中のみ）" if open_now else ""
            header = f"📍 現在地から半径{radius}m以内の**{keyword}**{open_label}\n\n"
            body = _format_places(places, lat, lng)
            for chunk in split_message(BOT_PREFIX + header + body):
                await channel.send(chunk)

        return web.Response(text='OK')
    except Exception as e:
        print(f"位置情報受信エラー: {e}")
        return web.Response(status=500, text=str(e))


async def handle_health(request):
    return web.Response(text='OK')


async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health)
    app.router.add_get('/location', handle_location_page)
    app.router.add_post('/location/submit', handle_location_submit)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f'Webサーバー起動: port {PORT}')


def _cleanup_pending_locations():
    """期限切れのトークンを削除"""
    now = time.monotonic()
    expired = [t for t, v in _pending_locations.items() if now > v[4]]
    for t in expired:
        del _pending_locations[t]


async def reminder_checker():
    await discord_client.wait_until_ready()
    while not discord_client.is_closed():
        try:
            loop = asyncio.get_running_loop()
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
            # 期限切れ位置情報トークンを定期削除
            _cleanup_pending_locations()
        except Exception as e:
            print(f"リマインダーチェックエラー: {e}")
        await asyncio.sleep(60)


# ───────────── イベント ─────────────

@discord_client.event
async def on_ready():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _init_db)
    asyncio.ensure_future(reminder_checker())
    asyncio.ensure_future(start_web_server())
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
    loop = asyncio.get_running_loop()

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
                ct = audio_attachment.content_type or ''
                if 'ogg' in ct:
                    suffix = '.ogg'
                elif 'mp4' in ct or 'aac' in ct or 'm4a' in ct:
                    suffix = '.mp4'
                elif 'webm' in ct:
                    suffix = '.webm'
                else:
                    suffix = '.wav'
                try:
                    transcribed = await loop.run_in_executor(None, _transcribe_sync, audio_bytes, suffix)
                    print(f"文字起こし結果: 「{transcribed}」")
                    if len(transcribed) < 4:
                        await message.reply(f"{BOT_PREFIX}⚠️ 音声認識がうまくいきませんでした（認識結果:「{transcribed}」）。\nもう少しゆっくり・はっきり話してみてください。")
                        return
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

                lat, lng = await loop.run_in_executor(None, _extract_coords, user_text)

                # URLから取れなければ地名・駅名をジオコード
                if lat is None:
                    place_match = re.search(
                        r'([ぁ-んァ-ヶー一-鿿]{1,10}[駅町市区村丁目]+|[ぁ-んァ-ヶー一-鿿]{2,8}(?:周辺|付近|エリア)?)',
                        re.sub(r'近く|付近|周辺|現在地|営業中|今開いてる|バー|居酒屋|レストラン|カフェ|検索|探して', '', user_text)
                    )
                    if place_match:
                        lat, lng = await loop.run_in_executor(None, _geocode, place_match.group(1))

                # 現在地リンク方式（GPS）
                if lat is None:
                    keyword_match = re.search(r'(バー|居酒屋|レストラン|カフェ|コンビニ|薬局|スーパー|ラーメン|寿司|焼肉|ホテル|銭湯|カラオケ|ドラッグストア|ドラスト|駐車場|ガソリンスタンド|スタバ|マック|マクドナルド)', user_text)
                    keyword = keyword_match.group(1) if keyword_match else "飲食店"
                    open_now = any(k in user_text for k in ["営業中", "今開いてる", "今やってる", "開いてる"])
                    radius_match = re.search(r'(\d+)\s*km', user_text)
                    radius = int(float(radius_match.group(1)) * 1000) if radius_match else 1000

                    token = str(uuid.uuid4())[:8]
                    _pending_locations[token] = (channel_id, keyword, radius, open_now, time.monotonic() + _PENDING_LOCATION_TTL)

                    if BOT_BASE_URL:
                        link = f"{BOT_BASE_URL}/location?token={token}"
                        view = discord.ui.View()
                        view.add_item(discord.ui.Button(
                            label="📍 現在地を共有する",
                            url=link,
                            style=discord.ButtonStyle.link
                        ))
                        await message.reply(
                            f"{BOT_PREFIX}🔍 **{keyword}** / 半径{radius}m{'（営業中のみ）' if open_now else ''}\n"
                            f"下のボタンをタップして現在地を送信してください。",
                            view=view
                        )
                    else:
                        await message.reply(
                            f"{BOT_PREFIX}📍 場所を特定できませんでした。駅名・地名で指定してください。\n"
                            f"例：「渋谷駅近くの今開いてるバー」"
                        )
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
                def _call_travel():
                    return anthropic_client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=2048,
                        system=TRAVEL_SYSTEM_PROMPT,
                        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                        messages=[{"role": "user", "content": user_text}],
                    )
                response = await loop.run_in_executor(None, _call_travel)
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
                def _call_reservation():
                    return anthropic_client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=2048,
                        system=RESERVATION_SYSTEM_PROMPT,
                        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                        messages=[{"role": "user", "content": user_text}],
                    )
                response = await loop.run_in_executor(None, _call_reservation)
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

            def _call_chat():
                return anthropic_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=8096,
                    system=SYSTEM_PROMPT,
                    tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                    messages=history,
                )
            response = await loop.run_in_executor(None, _call_chat)

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
