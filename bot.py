import os
import re
import io
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
JARVIS_LOG_CHANNEL_ID = os.environ.get('JARVIS_LOG_CHANNEL_ID', '')
PORT = int(os.environ.get('PORT', 8080))
_raw_base_url = os.environ.get('BOT_BASE_URL', '')
BOT_BASE_URL = f"https://{_raw_base_url}" if _raw_base_url and not _raw_base_url.startswith('http') else _raw_base_url

# 位置情報リクエストの一時保存 {token: (channel_id, keyword, radius, open_now, expires_at)}
_pending_locations: dict[str, tuple] = {}
_PENDING_LOCATION_TTL = 600  # 10分

# 直前の検索結果キャッシュ {channel_id: (places, lat, lng)}
_last_places_cache: dict[str, tuple] = {}

JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """あなたは優秀なパーソナルアシスタントです。日本語で回答してください。
ウェブ検索が必要な場合は検索ツールを使って最新情報を取得してください。
コードの作業、情報収集、調査、雑談など何でも対応します。

【このBotが持つ機能】
- リマインダー設定（「〇月〇日〇時にリマインドして」「〇日の夜にお知らせして」等）
- メモ保存・一覧表示
- 周辺スポット検索（Google Maps連携）
- 旅行・航空券検索
- レストラン予約サポート
- ボイスメッセージの文字起こし
- 画像認識・分析

リマインダーや通知に関する質問には「できない」と答えず、上記機能を案内してください。"""

HISTORY_LIMIT = 40
BOT_PREFIX = "**【アシスタント】**\n"

REMINDER_KEYWORDS = ["リマインド", "reminder", "通知して", "忘れないように", "アラーム", "お知らせして", "知らせて", "忘れずに", "忘れないで", "教えてほしい", "声かけて", "声をかけて"]
MEMO_SAVE_KEYWORDS = ["メモして", "メモ：", "メモ:", "覚えておいて", "記録して", "メモ保存"]
MEMO_LIST_KEYWORDS = ["メモ見せて", "メモ一覧", "メモを教えて", "メモ確認", "メモリスト"]
RESERVATION_KEYWORDS = ["予約", "席を取って", "予約して", "予約したい", "ご予約", "席の予約"]
TRAVEL_KEYWORDS = ["航空券", "飛行機", "ホテル", "宿", "ツアー", "旅行", "格安", "安いフライト", "旅館", "パック旅行", "ANA", "JAL", "LCC"]
MAP_KEYWORDS = ["近く", "現在地", "付近", "周辺", "近い", "近くの", "今開いてる", "営業中", "地図", "マップ"]
FOOD_KEYWORDS = ["ラーメン", "ラーメン屋", "寿司", "焼肉", "居酒屋", "カフェ", "レストラン", "うどん", "そば", "バー", "ランチ", "ディナー", "飲食店", "グルメ", "お店", "食べ物", "ご飯"]
SEARCH_PHRASES = ["探して", "を探", "見つけて", "おすすめ", "教えて", "ある？", "ない？", "どこ"]
# JARVISへのパス：PC操作・ローカル作業・明示的な指示
JARVIS_KEYWORDS = ["JARVISに", "ジャービスに", "JARVISで", "jarvisに", "PCで", "PCの", "ファイルを", "フォルダを",
                   "RECONを", "スクリプトを", "ターミナルで", "ローカルで", "自動化して", "PCを起動", "PCを操作"]
JARVIS_MEMORY_SAVE_KEYWORDS = ["JARVIS記憶して", "JARVISに覚えさせて", "JARVIS覚えて"]
JARVIS_MEMORY_LIST_KEYWORDS = ["JARVISの記憶", "JARVIS記憶一覧", "JARVISが覚えてること"]
JARVIS_LOG_KEYWORDS = ["JARVISのログ", "JARVISの会話履歴", "JARVISと何を話した"]
PROPOSAL_KEYWORDS = ["提案:", "提案：", "クロードに:", "クロードに：", "Claudeに:", "Claudeに：", "提案メモ"]
PROPOSAL_LIST_KEYWORDS = ["提案一覧", "提案リスト", "Claudeへの提案"]
URL_CLIP_KEYWORDS = ["要約", "まとめ", "クリップ", "読んで", "サマリー"]
NOTE_IDEA_KEYWORDS = ["noteネタ", "note案", "記事ネタ", "ネタにして", "記事のアイデア", "noteアイデア", "note書いて", "記事にして"]
CARD_PRICE_KEYWORDS = ["相場", "ポケカ", "カード価格", "買取", "いくら", "価格", "カードの値段"]

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

NOTE_IDEA_SYSTEM_PROMPT = """あなたはnote記事のコンテンツアイデアを提案するアシスタントです。
送られてきた画像・写真・場面から、noteの記事ネタを3つ提案してください。

各提案を以下の形式で：

**アイデア①：[タイトル案]**
ターゲット：〇〇
切り口：〇〇
冒頭の一文：〇〇

以下を意識してください：
・タイトルはビフォー→アフターを匂わせる（「〜だった私が〜になった」）
・具体的な数字を必ず含める
・「失敗・成功・お金」のいずれかに触れる
・読者が「自分のことだ」と感じられる切り口
・感情を揺さぶる強ワードを先頭に置く

ユーザーがジャンル（お金/恋愛/自己啓発/コラム/スピリチュアル）を指定した場合はそのトーンで提案してください。"""

CARD_PRICE_SYSTEM_PROMPT = """送られてきた画像からポケモンカードの情報を特定し、ウェブ検索で現在の市場価格を調べてください。

以下の形式で回答してください：

**🃏 [カード名]**
セット：〇〇 / レアリティ：〇〇

**💰 現在の相場**
・メルカリ/ヤフオク：〇〇円〜〇〇円
・カードショップ買取：〇〇円前後

画像からカードが特定できない場合はその旨を伝えてください。
ウェブ検索で最新の価格情報を取得してください。"""

URL_CLIP_SYSTEM_PROMPT = """指定されたURLのページをウェブ検索ツールで取得し、以下の形式で要約してください。

**📌 [タイトル]**

**💡 要点**
・〇〇
・〇〇
・〇〇

**一言：** 〇〇（15字以内）

ページにアクセスできない場合はその旨を伝えてください。余計な前置きは不要です。"""


# ───────────── DB ─────────────

def _get_conn():
    return psycopg2.connect(DATABASE_URL)


def _init_db():
    with _get_conn() as conn:
        with conn.cursor() as cur:
            # JARVIS連携テーブル
            cur.execute("""
                CREATE TABLE IF NOT EXISTS jarvis_queue (
                    id SERIAL PRIMARY KEY,
                    payload TEXT NOT NULL,
                    speak BOOLEAN DEFAULT FALSE,
                    channel_id TEXT,
                    status TEXT DEFAULT 'pending',
                    response TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    processed_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS jarvis_memory (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS claude_proposals (
                    id SERIAL PRIMARY KEY,
                    username TEXT,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    status TEXT DEFAULT 'pending'
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


def _prune_old_conversations(days=90):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM discord_conversations WHERE created_at < %s",
                (cutoff,)
            )
        conn.commit()


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


def _save_proposal(username: str, content: str) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO claude_proposals (username, content) VALUES (%s, %s)",
                (username, content)
            )
        conn.commit()


def _get_proposals(limit=10):
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT id, username, content, created_at FROM claude_proposals
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()


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

# ───────────── JARVIS連携 DB関数 ─────────────

def _jarvis_enqueue(payload: str, channel_id: str, speak: bool = False) -> int:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jarvis_queue (payload, channel_id, speak) VALUES (%s,%s,%s) RETURNING id",
                (payload, channel_id, speak),
            )
            row = cur.fetchone()
        conn.commit()
    return row[0]


def _jarvis_get_response(queue_id: int) -> str | None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, response FROM jarvis_queue WHERE id=%s",
                (queue_id,),
            )
            row = cur.fetchone()
    if row and row[0] in ("done", "error"):
        return row[1] or "（応答なし）"
    return None


def _jarvis_memory_save(key: str, value: str) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO jarvis_memory (key, value, updated_at)
                   VALUES (%s,%s,NOW())
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                (key, value),
            )
        conn.commit()


def _jarvis_memory_list() -> list[tuple[str, str]]:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM jarvis_memory ORDER BY updated_at DESC LIMIT 20")
            return cur.fetchall()


def _jarvis_log_history(limit: int = 10) -> list[dict]:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """SELECT role, content FROM discord_conversations
                   WHERE channel_id='jarvis_local'
                   ORDER BY created_at DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ───────────── インテント判定 ─────────────

def _normalize_jarvis(text: str) -> str:
    """音声認識による JARVIS の誤認識を正規化"""
    return re.sub(r'[Jj]ervis|[Jj]arvis|ジャービス|じゃーびす', 'JARVIS', text)


def _detect_intent(text):
    text = _normalize_jarvis(text)
    if any(k in text for k in JARVIS_LOG_KEYWORDS):
        return "jarvis_log"
    if any(k in text for k in JARVIS_MEMORY_LIST_KEYWORDS):
        return "jarvis_memory_list"
    if any(k in text for k in JARVIS_MEMORY_SAVE_KEYWORDS):
        return "jarvis_memory_save"
    if any(k in text for k in JARVIS_KEYWORDS):
        return "jarvis_task"
    if any(k in text for k in PROPOSAL_LIST_KEYWORDS):
        return "claude_proposal_list"
    if any(k in text for k in PROPOSAL_KEYWORDS):
        return "claude_proposal"
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
    # 食べ物＋検索フレーズ → map_search（「赤羽でラーメン探して」等）
    if any(f in text for f in FOOD_KEYWORDS):
        if any(s in text for s in SEARCH_PHRASES) or "写真" in text:
            return "map_search"
    if any(k in text for k in REMINDER_KEYWORDS):
        return "reminder"
    # URLだけ、またはURL＋要約キーワードならクリッピング
    if re.search(r'https?://\S+', text):
        without_url = re.sub(r'https?://\S+', '', text).strip()
        if len(without_url) == 0 or any(k in text for k in URL_CLIP_KEYWORDS):
            return "url_clip"
    return "chat"


def _parse_reminder(text):
    """Claudeで日時とメッセージを抽出（Haiku使用でコスト節約）"""
    now_jst = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        system=f"""現在の日時（JST）: {now_jst}
ユーザーのメッセージからリマインダーの日時と内容を抽出してください。
必ずJSON形式のみで返してください（説明文は不要）:
{{"remind_at": "YYYY-MM-DDTHH:MM:SS", "message": "リマインダー内容"}}

ルール:
- 「夜」=21:00、「朝」=08:00、「昼」=12:00 として扱う
- 「〇日前」「〇日の前日」は指定日の前日21:00とする
- 年が省略された場合は{datetime.now(JST).year}年とする（過去日なら翌年）
- 日時が完全に不明な場合のみ: {{"error": "日時が不明です"}}""",
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
    """住所・地名を座標に変換。(lat, lng, formatted_address) を返す"""
    if not GOOGLE_MAPS_API_KEY:
        return None, None, None
    r = requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={
            "address": address,
            "language": "ja",
            "region": "jp",
            "components": "country:JP",
            "key": GOOGLE_MAPS_API_KEY,
        },
        timeout=10
    )
    data = r.json()
    if data.get("results"):
        result = data["results"][0]
        loc = result["geometry"]["location"]
        formatted = result.get("formatted_address", "").replace("日本、", "")
        return loc["lat"], loc["lng"], formatted
    return None, None, None


# 都道府県・主要都市のプレフィックスパターン
_PREF_PATTERN = re.compile(
    r'^(東京|大阪|京都|北海道|神奈川|埼玉|千葉|愛知|兵庫|福岡|静岡|広島|宮城|新潟|長野|岐阜|栃木|群馬|茨城|福島|山形|秋田|岩手|青森|三重|滋賀|奈良|和歌山|鳥取|島根|岡山|山口|徳島|香川|愛媛|高知|佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄|横浜|名古屋|札幌|仙台|神戸|福島|渋谷|新宿|池袋|品川|秋葉原|上野|浅草|銀座|六本木|表参道)[のでにからまで]?'
)

def _extract_city_prefix(text):
    """「東京の末広町」→ ('東京', '末広町') のように都市名と地名を分離"""
    m = _PREF_PATTERN.match(text)
    if m:
        city = m.group(1)
        place = text[m.end():].strip()
        return city, place
    return None, text


PLACE_TYPE_MAP = {
    # 施設タイプ（タイプ指定検索が有効なもの）
    'コンビニ': 'convenience_store',
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
    'マック': 'meal_takeaway',
    'マクドナルド': 'meal_takeaway',
    'スタバ': 'cafe',
    # 以下は意図的にマッピングしない（searchTextで名前検索する方が正確）
    # ラーメン・寿司・焼肉・居酒屋・バー・レストランは
    # 「restaurant」タイプだとマック等も含まれてしまうためキーワード検索を使う
}


def _nearby_search(lat, lng, keyword, radius=2000, open_now=False):
    """Places API (New) で周辺スポットを検索"""
    place_type = PLACE_TYPE_MAP.get(keyword)
    field_mask = "places.id,places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.location,places.regularOpeningHours,places.photos"

    def _search(r):
        if place_type:
            # searchNearby: タイプ指定の近隣検索
            body = {
                "locationRestriction": {
                    "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(r)}
                },
                "includedTypes": [place_type],
                "maxResultCount": 20,
                "languageCode": "ja",
            }
            endpoint = "https://places.googleapis.com/v1/places:searchNearby"
        else:
            # searchText: キーワード検索
            # locationBias（優先）＋rankPreference DISTANCE（近い順）で精度を確保
            # locationRestrictionは厳密すぎてヒット0になるケースがあるため使わない
            body = {
                "textQuery": keyword,
                "locationBias": {
                    "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(r)}
                },
                "rankPreference": "DISTANCE",
                "maxResultCount": 20,
                "languageCode": "ja",
            }
            endpoint = "https://places.googleapis.com/v1/places:searchText"

        resp = requests.post(
            endpoint,
            headers={
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "X-Goog-FieldMask": field_mask,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=15,
        )
        print(f"Places API: {resp.status_code} radius={r} type={place_type} keyword={keyword}")
        if resp.status_code != 200:
            print(f"  エラー詳細: {resp.text[:300]}")
            return []
        return resp.json().get("places", [])

    results = _search(radius)
    if len(results) < 3:
        wider = _search(5000) or []
        seen = {p.get('id') for p in results}
        results = results + [p for p in wider if p.get('id') not in seen]
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
    """Places API (New) の検索結果を整形"""
    lines = []
    for i, p in enumerate(places[:limit], 1):
        name = p.get("displayName", {}).get("text", "不明")
        rating = p.get("rating", "-")
        user_ratings = p.get("userRatingCount", 0)
        address = p.get("formattedAddress", "")
        place_id = p.get("id", "")
        loc = p.get("location") or {}
        dist = int(_haversine(lat, lng, loc.get("latitude", lat), loc.get("longitude", lng)))
        maps_url = f"https://www.google.com/maps/place/?q=place_id:{place_id}"
        open_now = p.get("regularOpeningHours", {}).get("openNow")
        status = "🟢 営業中" if open_now else ("🔴 営業時間外" if open_now is False else "")
        lines.append(
            f"**{i}. {name}** ⭐{rating}（{user_ratings}件）{' ' + status if status else ''}\n"
            f"　📍 {address}（約{dist}m）\n"
            f"　🔗 {maps_url}"
        )
    return "\n\n".join(lines)


def _fetch_photo_by_name(photo_name: str) -> bytes | None:
    """photo_nameから画像バイト列を取得。2方式でフォールバック"""
    base_url = f"https://places.googleapis.com/v1/{photo_name}/media?maxWidthPx=800"
    # 方式①: skipHttpRedirect=true でphotoUriを取得
    try:
        meta = requests.get(
            base_url + f"&skipHttpRedirect=true&key={GOOGLE_MAPS_API_KEY}",
            timeout=10
        )
        print(f"    [方式①] status={meta.status_code}")
        if meta.status_code == 200:
            body = meta.json()
            photo_uri = body.get("photoUri") or body.get("photo_uri", "")
            print(f"    photoUri={photo_uri[:60] if photo_uri else 'なし'} keys={list(body.keys())}")
            if photo_uri:
                img = requests.get(photo_uri, timeout=15)
                print(f"    画像DL: status={img.status_code} size={len(img.content)}")
                if img.status_code == 200 and len(img.content) > 1000:
                    return img.content
    except Exception as e:
        print(f"    [方式①] 例外: {e}")
    # 方式②: 直接リダイレクト追跡
    try:
        img = requests.get(
            base_url + f"&key={GOOGLE_MAPS_API_KEY}",
            timeout=15,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        print(f"    [方式②] status={img.status_code} ct={img.headers.get('content-type','')} size={len(img.content)}")
        if img.status_code == 200 and len(img.content) > 1000:
            return img.content
    except Exception as e:
        print(f"    [方式②] 例外: {e}")
    return None


def _fetch_place_photos_sync(places_or_photo_data, limit=3):
    """写真データリスト [(店名, photo_name), ...] またはplacesリストから画像を取得"""
    result = []
    # photo_dataリスト形式 [(name, photo_name), ...]
    if places_or_photo_data and isinstance(places_or_photo_data[0], (list, tuple)) and isinstance(places_or_photo_data[0][-1], str) and not isinstance(places_or_photo_data[0][0], dict):
        items = places_or_photo_data[:limit]
        for entry in items:
            name, photo_name = entry[0], entry[1]
            print(f"写真取得: {name} / {photo_name[:50]}")
            data = _fetch_photo_by_name(photo_name)
            if data:
                result.append((name, data))
        return result
    # placesリスト形式
    places = places_or_photo_data
    with_photos = [(p, p.get("photos", [])) for p in places[:limit]]
    print(f"写真フィールドあり: {sum(1 for _, ph in with_photos if ph)}/{len(with_photos)}件")
    for p, photos in with_photos:
        name = p.get("displayName", {}).get("text", "店舗")
        if not photos:
            print(f"  写真なし: {name}")
            continue
        photo_name = photos[0].get("name", "")
        if not photo_name:
            print(f"  photo_name空: {name}")
            continue
        print(f"写真取得: {name} / {photo_name[:50]}")
        data = _fetch_photo_by_name(photo_name)
        if data:
            result.append((name, data))
    return result


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


async def _send_places_result(channel, places, lat, lng, keyword, radius, open_now, loop, reference=None, location_name=None):
    """地図検索結果テキスト＋写真をDiscordに送信"""
    cid = str(channel.id)
    # メモリキャッシュ（即時）
    _last_places_cache[cid] = (places, lat, lng)
    # DB永続保存（再起動後も利用可能）
    photo_data = []
    for p in places[:3]:
        pname = p.get("displayName", {}).get("text", "店舗")
        ph = p.get("photos", [])
        if ph and ph[0].get("name"):
            photo_data.append([pname, ph[0]["name"]])
    if photo_data:
        await loop.run_in_executor(None, _jarvis_memory_save, f"photos_{cid}", json.dumps(photo_data, ensure_ascii=False))
        print(f"写真名をDB保存: {len(photo_data)}件")

    open_label = "（営業中のみ）" if open_now else ""
    area = location_name if location_name else "現在地"
    header = f"📍 {area}から半径{radius}mの**{keyword}**{open_label}\n\n"
    body = _format_places(places, lat, lng)
    send_kwargs = {"reference": reference} if reference else {}
    first = True
    for chunk in split_message(BOT_PREFIX + header + body):
        await channel.send(chunk, **(send_kwargs if first else {}))
        first = False

    # 上位3件の写真を取得して送信
    photos = await loop.run_in_executor(None, _fetch_place_photos_sync, places, 3)
    if photos:
        files = [discord.File(io.BytesIO(data), filename=f"photo_{i+1}.jpg")
                 for i, (_, data) in enumerate(photos)]
        caption = "📸 " + " / ".join(name for name, _ in photos)
        await channel.send(caption, files=files)


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
            ev_loop = asyncio.get_running_loop()
            await _send_places_result(channel, places, lat, lng, keyword, radius, open_now, ev_loop)

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
    _tick = 0
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
            # 1日1回、90日以上前の会話履歴を削除
            _tick += 1
            if _tick % 1440 == 0:
                await loop.run_in_executor(None, _prune_old_conversations)
                print("会話履歴pruning完了（90日以上前を削除）")
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
            # 画像付きの場合は専用インテントで上書き
            if image_contents and user_text:
                if any(k in user_text for k in NOTE_IDEA_KEYWORDS):
                    intent = "note_idea"
                elif any(k in user_text for k in CARD_PRICE_KEYWORDS):
                    intent = "card_price"
            # テキストのみで「写真」キーワード（食べ物系でなければ前回検索への参照とみなす）
            if not image_contents and intent == "chat":
                if any(k in user_text for k in ["写真", "フォト"]) and not any(f in user_text for f in FOOD_KEYWORDS):
                    intent = "photo_request"

            # ── JARVIS ログ表示 ──
            if intent == "jarvis_log":
                rows = await loop.run_in_executor(None, _jarvis_log_history, 10)
                if not rows:
                    await message.reply(f"{BOT_PREFIX}JARVISの会話履歴はまだありません。")
                    return
                lines = [f"**{r['role']}:** {r['content'][:100]}" for r in rows]
                await message.reply(f"{BOT_PREFIX}🖥️ **JARVIS 直近の会話**\n" + "\n".join(lines))
                return

            # ── JARVIS 記憶一覧 ──
            if intent == "jarvis_memory_list":
                items = await loop.run_in_executor(None, _jarvis_memory_list)
                if not items:
                    await message.reply(f"{BOT_PREFIX}JARVISの記憶はまだありません。")
                    return
                lines = [f"**{k}**: {v}" for k, v in items]
                await message.reply(f"{BOT_PREFIX}🧠 **JARVIS 記憶一覧**\n" + "\n".join(lines))
                return

            # ── JARVIS 記憶保存 ──
            if intent == "jarvis_memory_save":
                content = user_text
                for kw in JARVIS_MEMORY_SAVE_KEYWORDS:
                    content = content.replace(kw, "").strip()
                content = content.lstrip("：:").strip()
                if ":" in content or "：" in content:
                    sep = "：" if "：" in content else ":"
                    key, value = content.split(sep, 1)
                    await loop.run_in_executor(None, _jarvis_memory_save, key.strip(), value.strip())
                    await message.reply(f"{BOT_PREFIX}🧠 記憶しました。\n**{key.strip()}**: {value.strip()}")
                else:
                    await loop.run_in_executor(None, _jarvis_memory_save, f"memo_{int(time.time())}", content)
                    await message.reply(f"{BOT_PREFIX}🧠 記憶しました: {content}")
                return

            # ── JARVIS タスクパス ──
            if intent == "jarvis_task":
                payload = _normalize_jarvis(user_text)
                for kw in JARVIS_KEYWORDS:
                    payload = payload.replace(kw, "").strip()
                payload = payload or user_text
                queue_id = await loop.run_in_executor(None, _jarvis_enqueue, payload, channel_id, False)
                await message.reply(f"{BOT_PREFIX}🖥️ JARVISにタスクを送りました。返答を待っています…")
                # 最大60秒ポーリング
                for _ in range(30):
                    await asyncio.sleep(2)
                    response = await loop.run_in_executor(None, _jarvis_get_response, queue_id)
                    if response:
                        for chunk in split_message(f"{BOT_PREFIX}🖥️ **JARVIS より:**\n{response}"):
                            await message.reply(chunk)
                        # ログチャンネルにも投稿
                        if JARVIS_LOG_CHANNEL_ID:
                            log_ch = discord_client.get_channel(int(JARVIS_LOG_CHANNEL_ID))
                            if log_ch:
                                await log_ch.send(
                                    f"📲 **[Discord → JARVIS]**\n**依頼:** {payload}\n**返答:** {response[:500]}"
                                )
                        return
                await message.reply(f"{BOT_PREFIX}⏳ JARVISが応答しませんでした（60秒タイムアウト）。PCが起動しているか確認してください。")
                return

            # ── Claude提案一覧 ──
            if intent == "claude_proposal_list":
                rows = await loop.run_in_executor(None, _get_proposals)
                if not rows:
                    await message.reply(f"{BOT_PREFIX}📋 まだ提案はありません。")
                    return
                lines = []
                for i, r in enumerate(rows, 1):
                    jst_time = r['created_at'].replace(tzinfo=timezone.utc).astimezone(JST)
                    lines.append(f"**{i}.** [{jst_time.strftime('%-m/%-d %H:%M')}] {r['content']}")
                await message.reply(f"{BOT_PREFIX}📋 **Claudeへの提案一覧**\n" + "\n".join(lines))
                return

            # ── Claude提案保存 ──
            if intent == "claude_proposal":
                content = user_text
                for kw in PROPOSAL_KEYWORDS:
                    content = content.replace(kw, "").strip()
                content = content.lstrip("：:").strip()
                if not content:
                    await message.reply(f"{BOT_PREFIX}提案内容を書いてください。\n例：「提案: 検索結果に評価フィルターを追加してほしい」")
                    return
                await loop.run_in_executor(None, _save_proposal, str(message.author), content)
                # JARVISにファイル追記を依頼（失敗しても提案保存は成功扱い）
                now_str = datetime.now(JST).strftime('%Y/%m/%d %H:%M')
                file_task = (
                    f"C:\\Users\\makur\\claude-proposals.md というファイルに以下の1行を追記してください"
                    f"（ファイルがなければ新規作成）:\n- [{now_str}] {content}"
                )
                try:
                    await loop.run_in_executor(None, _jarvis_enqueue, file_task, channel_id, False)
                except Exception as e:
                    print(f"JARVIS enqueue失敗（提案はDB保存済み）: {e}")
                await message.reply(
                    f"{BOT_PREFIX}✅ Claudeへの提案を送りました！\n"
                    f"📝 {content}\n"
                    f"💾 DBに保存済み・PCが起動中ならファイルにも書き込まれます"
                )
                return

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
                        await message.reply(
                            f"{BOT_PREFIX}⚠️ 日時が特定できませんでした。\n"
                            f"具体的な日時を指定してください。\n"
                            f"例：「5月14日の夜にお知らせして」「6月1日12時にリマインドして」"
                        )
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
                place_match = None  # キーワード抽出で参照するため先に初期化

                # URLから取れなければ地名・駅名をジオコード
                geocoded_label = None  # 確認メッセージ用
                if lat is None:
                    stripped = re.sub(r'近く|付近|周辺|現在地|営業中|今開いてる|バー|居酒屋|レストラン|カフェ|検索|探して', '', user_text)
                    place_match = re.search(
                        r'([ぁ-んァ-ヶー一-鿿]{1,10}[駅町市区村丁目]+|[ぁ-んァ-ヶー一-鿿]{2,8}(?:周辺|付近|エリア)?)',
                        stripped
                    )
                    if place_match:
                        raw_place = place_match.group(1)
                        # 「東京の末広町」のように都市プレフィックスがあれば結合
                        city_prefix, _ = _extract_city_prefix(
                            re.sub(r'(の|で|に|から|まで).*', '', user_text.replace(raw_place, '').strip())
                        )
                        geocode_query = f"{city_prefix} {raw_place}" if city_prefix else raw_place
                        lat, lng, geocoded_label = await loop.run_in_executor(None, _geocode, geocode_query)

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

                # ジオコード結果を先に通知（場所の確認 → 違う場合は「東京の〇〇」と言い直せる）
                if geocoded_label:
                    await message.reply(f"{BOT_PREFIX}📍 **{geocoded_label}** で検索します。\n違う場合は「東京の〇〇」のように都市名を付けて言い直してください。")

                # 検索キーワード抽出（地名を除いてから食べ物・施設を探す）
                # ※ [一-鿿]{1,6} の漢字一括マッチは地名を誤って拾うため除去
                text_for_kw = user_text.replace(place_match.group(1), "") if place_match else user_text
                keyword_match = re.search(
                    r'(バー|居酒屋|レストラン|カフェ|コンビニ|薬局|スーパー|ラーメン|寿司|焼肉|ホテル|銭湯|カラオケ|ドラッグストア|スタバ|マック|マクドナルド|うどん|そば|ピザ|焼き鳥|天ぷら|定食)',
                    text_for_kw
                )
                if keyword_match:
                    keyword = keyword_match.group(1)
                else:
                    keyword = next((f for f in FOOD_KEYWORDS if f in text_for_kw), "飲食店")
                open_now = any(k in user_text for k in ["営業中", "今開いてる", "今やってる", "開いてる"])

                radius_match = re.search(r'(\d+)\s*km', user_text)
                radius = int(float(radius_match.group(1)) * 1000) if radius_match else 1000

                places = await loop.run_in_executor(None, _nearby_search, lat, lng, keyword, radius, open_now)
                if not places:
                    await message.reply(f"{BOT_PREFIX}半径{radius}m以内に「{keyword}」は見つかりませんでした。範囲を広げるか別のキーワードをお試しください。")
                    return

                await _send_places_result(message.channel, places, lat, lng, keyword, radius, open_now, loop, reference=message, location_name=geocoded_label)
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

            # ── 写真リクエスト（キャッシュ or DB永続データから） ──
            if intent == "photo_request":
                await message.reply(f"{BOT_PREFIX}📸 写真を取得しています…")
                photos = []
                # ①メモリキャッシュ
                cache = _last_places_cache.get(channel_id)
                if cache:
                    places_cached, _, _ = cache
                    photos = await loop.run_in_executor(None, _fetch_place_photos_sync, places_cached, 3)
                # ②DB永続データ（再起動後フォールバック）
                if not photos:
                    def _load_photo_data():
                        rows = _jarvis_memory_list()
                        for k, v in rows:
                            if k == f"photos_{channel_id}":
                                try:
                                    return json.loads(v)
                                except Exception:
                                    return []
                        return []
                    photo_data = await loop.run_in_executor(None, _load_photo_data)
                    if photo_data:
                        print(f"DBから写真データ読込: {len(photo_data)}件")
                        photos = await loop.run_in_executor(None, _fetch_place_photos_sync, photo_data, 3)
                if not photos:
                    await message.reply(f"{BOT_PREFIX}写真を取得できませんでした。先に周辺検索を行ってください。")
                    return
                files = [discord.File(io.BytesIO(data), filename=f"photo_{i+1}.jpg")
                         for i, (_, data) in enumerate(photos)]
                caption = "📸 " + " / ".join(n for n, _ in photos)
                await message.reply(caption, files=files)
                return

            # ── noteネタ生成（画像→記事アイデア） ──
            if intent == "note_idea":
                user_content = image_contents + ([{"type": "text", "text": user_text}] if user_text else [{"type": "text", "text": "この写真からnoteの記事ネタを3つ提案してください。"}])
                def _call_note_idea():
                    return anthropic_client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=1500,
                        system=NOTE_IDEA_SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": user_content}],
                    )
                response = await loop.run_in_executor(None, _call_note_idea)
                reply_text = next((b.text for b in response.content if hasattr(b, 'text')), "アイデアの生成に失敗しました。")
                for chunk in split_message(BOT_PREFIX + reply_text):
                    await message.reply(chunk)
                return

            # ── ポケカ相場チェック（写真→価格検索） ──
            if intent == "card_price":
                user_content = image_contents + ([{"type": "text", "text": user_text}] if user_text else [{"type": "text", "text": "このポケモンカードの現在の相場を調べてください。"}])
                def _call_card_price():
                    return anthropic_client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=800,
                        system=CARD_PRICE_SYSTEM_PROMPT,
                        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                        messages=[{"role": "user", "content": user_content}],
                    )
                response = await loop.run_in_executor(None, _call_card_price)
                reply_text = next((b.text for b in response.content if hasattr(b, 'text')), "カードの特定または価格検索に失敗しました。")
                for chunk in split_message(BOT_PREFIX + reply_text):
                    await message.reply(chunk)
                return

            # ── URLクリッピング ──
            if intent == "url_clip":
                def _call_clip():
                    return anthropic_client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=1024,
                        system=URL_CLIP_SYSTEM_PROMPT,
                        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                        messages=[{"role": "user", "content": user_text}],
                    )
                response = await loop.run_in_executor(None, _call_clip)
                reply_text = ""
                for block in response.content:
                    if hasattr(block, 'text'):
                        reply_text += block.text
                if not reply_text:
                    reply_text = "ページの取得に失敗しました。URLが正しいか確認してください。"
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
