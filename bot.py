import asyncio
import atexit
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Set, Tuple

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, Conflict, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from TikTokLive import TikTokLiveClient
from TikTokLive.client.web.web_settings import WebDefaults
from TikTokLive.events import ConnectEvent, DisconnectEvent, EnvelopeEvent, RoomUserSeqEvent

try:
    from TikTokLive.events import LinkMicBattleEvent
except ImportError:  # kütüphane sürümünde yoksa battle logu kapalı kalır
    LinkMicBattleEvent = None

# .env dosyasını (varsa) oku
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ================== AYARLAR ==================
WebDefaults.tiktok_sign_api_key = os.environ.get("euler_MWE0MjQ2NjgzNjA5NDE3NGU1MTc0NmVhMzg3ZTYyMDExM2ExZTcxMjFkYzNjYzIxMDZlNzQw")

BOT_TOKEN = os.environ.get("8916081263:AAHvXnG5vUv5WK9Vi8EPijJou8VLXphcXnE")
GROUP_CHAT_ID = -1004468850133

WATCHLIST_FILE = "watchlist.json"
SETTINGS_FILE = "settings.json"
STATS_FILE = "stats.json"
LOG_FILE = "bot.log"

AUTO_RECONNECT_SECONDS = 300   # bağlı olmayan hesapları bu aralıkla tekrar dener
CONNECT_STAGGER_SECONDS = 1.5  # hesapları tek tek bağlarken arada bekleme
LIST_PER_PAGE = 8
DEAD_DAYS = 7                  # bu kadar gündür hiç bağlanamayan hesap "ölü" sayılır
LOCK_FILE = os.path.expanduser("~/.sandik_bot.lock")
RESTART_NOTICE_FILE = "restart_notice.json"
MAX_UPLOAD_BYTES = 2_000_000


def _file_version() -> str:
    try:
        with open(os.path.abspath(sys.argv[0]), "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:6]
    except Exception:
        return "?"


BOT_VERSION = _file_version()

# ================== LOG ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
for noisy in ("httpx", "httpcore", "telegram.ext", "hpack"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("sandik")

# ================== GLOBAL DURUM ==================
WATCH_LIST: Set[str] = set()
clients: Dict[str, TikTokLiveClient] = {}
connecting: Set[str] = set()
connection_times: Dict[str, float] = {}   # sadece gerçekten bağlı olanlar
viewer_counts: Dict[str, int] = {}
background_tasks: Set[asyncio.Task] = set()

S = {"bot_active": True, "min_ratio": 0.0, "min_coins": 0, "delay": 120, "admin_ids": [],
     "favorites": [], "muted": [], "notify_unknown": True}
STATS = {"total_boxes_found": 0, "total_notifications_sent": 0, "last_box_time": None, "per_user": {},
         "history": [], "meta": {}}

# env_id -> {"user", "coins", "people", "sender", "sent"}
pending_boxes: Dict[str, dict] = {}

debug_log_counts: Dict[str, int] = {}


def debug_dump(kind: str, event, limit: int = 3, size: int = 2500):
    """Her olay türünden ilk birkaç örneği ham haliyle logla."""
    n = debug_log_counts.get(kind, 0)
    if n >= limit:
        return
    debug_log_counts[kind] = n + 1
    logger.warning(f"DEBUG {kind} #{n + 1}: {repr(event)[:size]}")


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


# ================== DOSYA İŞLEMLERİ ==================
def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"{path} okunamadı: {e}")
        return default


def save_json(path: str, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"{path} kaydedilemedi: {e}")


def load_all():
    global WATCH_LIST
    WATCH_LIST = set(load_json(WATCHLIST_FILE, []))
    S.update({k: v for k, v in load_json(SETTINGS_FILE, {}).items() if k in S})
    STATS.update({k: v for k, v in load_json(STATS_FILE, {}).items() if k in STATS})
    now = time.time()
    for u in WATCH_LIST:
        STATS["meta"].setdefault(u, {"added": now, "last_ok": None})
    logger.info(f"Watchlist yüklendi: {len(WATCH_LIST)} kişi")


def save_watchlist():
    save_json(WATCHLIST_FILE, sorted(WATCH_LIST))


def save_settings():
    save_json(SETTINGS_FILE, S)


def save_stats():
    save_json(STATS_FILE, STATS)


# ================== YARDIMCI ==================
def esc(v) -> str:
    return html.escape(str(v))


def fmt_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}sn"
    if seconds < 3600:
        return f"{seconds // 60}dk"
    return f"{seconds // 3600}sa {(seconds % 3600) // 60}dk"


def fmt_num(n) -> str:
    return f"{int(n):,}".replace(",", ".")


def is_admin(user_id: Optional[int]) -> bool:
    ids = S["admin_ids"]
    return not ids or (user_id in ids)


def is_connected(u: str) -> bool:
    return u in connection_times


def connected_users() -> List[str]:
    return [u for u in WATCH_LIST if is_connected(u)]


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{2,24}$")


def extract_names(text: str, loose: bool = False) -> List[str]:
    """Metinden TikTok kullanıcı adlarını çıkarır (link, @isim, loose modda düz isim)."""
    names: List[str] = []
    for n in re.findall(r"tiktok\.com/@([A-Za-z0-9_.]+)", text, flags=re.I):
        names.append(n)
    for n in re.findall(r"(?<![\w.])@([A-Za-z0-9_.]{2,24})", text):
        names.append(n)
    if loose:
        for tok in re.split(r"[\s,;]+", text):
            tok = tok.strip().lstrip("@")
            if USERNAME_RE.match(tok) and "tiktok.com" not in tok.lower():
                names.append(tok)
    out, seen = [], set()
    for n in names:
        n = n.lower().rstrip(".")
        if USERNAME_RE.match(n) and n not in seen:
            seen.add(n)
            out.append(n)
    return out


DIV = "━━━━━━━━━━━━━━━"


def fmt_k(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}K"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def bar(value: float, maximum: float, width: int = 5) -> str:
    filled = 0 if maximum <= 0 else round(min(value, maximum) / maximum * width)
    return "▰" * filled + "▱" * (width - filled)


def heat(viewers: int) -> str:
    if viewers >= 5000:
        return "🚀"
    if viewers >= 1000:
        return "🔥"
    if viewers >= 200:
        return "📈"
    return "👀"


def dead_users() -> List[str]:
    now = time.time()
    out = []
    for u in WATCH_LIST:
        m = STATS["meta"].get(u, {})
        ref = m.get("last_ok") or m.get("added") or now
        if not is_connected(u) and now - ref > DEAD_DAYS * 86400:
            out.append(u)
    return sorted(out)


def ratio_badge(ratio: float) -> str:
    if ratio >= 3:
        return "🔥🔥"
    if ratio >= 2:
        return "🔥"
    if ratio >= 1:
        return "✅"
    return "⚪"


# ================== EKRANLAR (inline menü) ==================
Screen = Tuple[str, InlineKeyboardMarkup]


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def tag(u: str) -> str:
    return (" ⭐" if u in S["favorites"] else "") + (" 🔕" if u in S["muted"] else "")


def render_main() -> Screen:
    total, on = len(WATCH_LIST), len(connected_users())
    viewers = sum(viewer_counts.get(u, 0) for u in connected_users())
    last = STATS["last_box_time"]
    text = (
        "🎁✨ <b>SANDIK TAKİP MERKEZİ</b> ✨🎁\n"
        f"{DIV}\n"
        f"📡 <b>Canlı takip:</b> {on}/{total}  <code>{bar(on, max(total, 1), 10)}</code>\n"
        f"👀 <b>Toplam izleyici:</b> {fmt_num(viewers)}\n"
        f"🔔 <b>Bildirim:</b> {'🟢 Açık' if S['bot_active'] else '🔴 Kapalı'}\n"
        f"🎯 <b>Filtre:</b> ⚖️ ≥{S['min_ratio']:g}x · 💎 ≥{S['min_coins']} · ⏱ {S['delay']}sn\n"
        f"{DIV}\n"
        f"📦 Sandık: <b>{fmt_num(STATS['total_boxes_found'])}</b>   "
        f"✉️ Bildirim: <b>{fmt_num(STATS['total_notifications_sent'])}</b>\n"
        f"🕒 Son sandık: <b>{fmt_dur(time.time() - last) + ' önce' if last else 'henüz yok'}</b>\n"
        f"{DIV}\n"
        f"👇 <i>Bir seçenek seç</i>   🧩 <code>{BOT_VERSION}</code>"
    )
    kb = [
        [btn("📋 Liste", "list:0"), btn("📊 Durum", "status")],
        [btn("➕ Ekle", "add"), btn("➖ Çıkar", "list:0:rm")],
        [btn("🏆 Geçmiş", "hist"), btn("📈 İstatistik", "stats")],
        [btn("⭐ Favoriler", "favs"), btn("⚙️ Ayarlar", "settings")],
        [btn("🧹 Temizlik", "cleanup"), btn("🔄 Yenile", "reconnect")],
        [btn("🧪 Test", "test"), btn("ℹ️ Yardım", "help:main")],
    ]
    return text, InlineKeyboardMarkup(kb)


def render_list(page: int = 0, remove_mode: bool = False) -> Screen:
    users = sorted(WATCH_LIST, key=lambda u: (not is_connected(u), -viewer_counts.get(u, 0), u))
    pages = max(1, (len(users) + LIST_PER_PAGE - 1) // LIST_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = users[page * LIST_PER_PAGE:(page + 1) * LIST_PER_PAGE]
    suffix = ":rm" if remove_mode else ""

    head = (
        f"{'➖ <b>ÇIKAR</b>' if remove_mode else '📋 <b>İZLEME LİSTESİ</b>'}\n{DIV}\n"
        f"👥 <b>{len(users)}</b> hesap · 🟢 <b>{len(connected_users())}</b> canlı · "
        f"⚪ <b>{len(users) - len(connected_users())}</b> kapalı"
    )
    if remove_mode:
        head += "\n\n❌ Çıkarmak istediğin hesabın yanındaki butona bas."
    if not users:
        head += "\n\n📭 Liste boş. <b>➕ Ekle</b> ile başla."

    kb = []
    for u in chunk:
        mark = "🟢" if is_connected(u) else "⚪"
        v = viewer_counts.get(u, 0)
        label = f"{mark} @{u}{tag(u)}" + (f" · {heat(v)}{fmt_k(v)}" if is_connected(u) and v else "")
        kb.append([btn(label, f"ui:{u}"), btn("❌", f"rm:{u}:{page}{suffix}")])

    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"list:{page - 1}{suffix}"))
    nav.append(btn(f"📄 {page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("➡️", f"list:{page + 1}{suffix}"))
    kb.append(nav)
    kb.append([btn("🏠 Menü", "menu"), btn("🔄 Yenile", f"list:{page}{suffix}")])
    return head, InlineKeyboardMarkup(kb)


def render_status() -> Screen:
    now = time.time()
    on = sorted(connected_users(), key=lambda u: -viewer_counts.get(u, 0))
    off = len(WATCH_LIST) - len(on)
    total_viewers = sum(viewer_counts.get(u, 0) for u in on)
    last = STATS["last_box_time"]
    lines = [
        "📊 <b>SİSTEM DURUMU</b>",
        DIV,
        f"📡 İzlenen: <b>{len(WATCH_LIST)}</b> · 🟢 Canlı: <b>{len(on)}</b> · ⚪ Kapalı: <b>{off}</b>",
        f"👀 Toplam izleyici: <b>{fmt_num(total_viewers)}</b>",
        f"🔔 Bildirim: <b>{'Açık ✅' if S['bot_active'] else 'Kapalı ❌'}</b>",
        f"🎯 Filtre: ⚖️ ≥{S['min_ratio']:g}x · 💎 ≥{S['min_coins']} · ⏱ {S['delay']}sn",
        f"🕒 Son sandık: <b>{fmt_dur(now - last) + ' önce' if last else 'henüz yok'}</b>",
    ]
    if on:
        lines += ["", "🟢 <b>CANLI YAYINLAR</b>", DIV]
        for u in on[:15]:
            v = viewer_counts.get(u, 0)
            vs = f"{heat(v)} {fmt_num(v)}" if v else "👀 ?"
            lines.append(f"• @{esc(u)}{tag(u)} — {vs} — ⏱ {fmt_dur(now - connection_times[u])}")
        if len(on) > 15:
            lines.append(f"… ve {len(on) - 15} yayın daha")
    else:
        lines.append("\n😴 Şu an bağlı yayın yok.")
    kb = [[btn("🔄 Yenile", "status"), btn("🏠 Menü", "menu")]]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


def render_stats() -> Screen:
    now = time.time()
    last = STATS["last_box_time"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_n = sum(1 for h in STATS["history"] if datetime.fromtimestamp(h["t"]).strftime("%Y-%m-%d") == today)
    lines = [
        "📈 <b>İSTATİSTİKLER</b>",
        DIV,
        f"📦 Bulunan sandık: <b>{fmt_num(STATS['total_boxes_found'])}</b>",
        f"✉️ Gönderilen bildirim: <b>{fmt_num(STATS['total_notifications_sent'])}</b>",
        f"📅 Bugünkü bildirim: <b>{today_n}</b>",
        f"📡 İzlenen hesap: <b>{len(WATCH_LIST)}</b>",
        f"🕒 Son sandık: <b>{fmt_dur(now - last) + ' önce' if last else 'henüz yok'}</b>",
    ]
    per_user = STATS.get("per_user", {})
    top = sorted(per_user.items(), key=lambda kv: -kv[1].get("boxes", 0))[:8]
    if top:
        best = top[0][1].get("boxes", 1) or 1
        lines += ["", "🏆 <b>EN ÇOK SANDIK ÇIKARANLAR</b>", DIV]
        medals = ["🥇", "🥈", "🥉"]
        for i, (u, d) in enumerate(top):
            n = d.get("boxes", 0)
            lines.append(f"{medals[i] if i < 3 else '▫️'} @{esc(u)} — <b>{n}</b> <code>{bar(n, best)}</code>")
    kb = [[btn("🏆 Geçmiş", "hist"), btn("🔄 Yenile", "stats")], [btn("🏠 Menü", "menu")]]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


def render_history() -> Screen:
    hist = STATS["history"][:10]
    lines = ["🏆 <b>SON SANDIKLAR</b>", DIV]
    if not hist:
        lines.append("\n📭 Henüz bildirim gönderilen sandık yok.")
    for i, h in enumerate(hist, 1):
        t = datetime.fromtimestamp(h["t"]).strftime("%H:%M")
        if h.get("coins") and h.get("people"):
            r = h["coins"] / h["people"]
            detail = f"💎{h['coins']}/👥{h['people']} · ⚖️{r:.1f}x {ratio_badge(r)}"
        else:
            detail = "💎?/👥? · ⚖️?"
        v = f" · 👀{fmt_k(h['viewers'])}" if h.get("viewers") else ""
        lines.append(f"{i}. <b>@{esc(h['user'])}</b>{tag(h['user'])}\n    {detail}{v} · 🕒 {t}")
    kb = [[btn("📈 İstatistik", "stats"), btn("🔄 Yenile", "hist")], [btn("🏠 Menü", "menu")]]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


def render_favs() -> Screen:
    fav = [u for u in S["favorites"] if u in WATCH_LIST]
    mut = [u for u in S["muted"] if u in WATCH_LIST]
    lines = [
        "⭐ <b>FAVORİLER & SESSİZLER</b>",
        DIV,
        "⭐ <b>Favori:</b> filtreye takılmaz, bildirimi her zaman gelir.",
        "🔕 <b>Sessiz:</b> bu hesaptan hiç bildirim gelmez.",
        "",
        "<i>Bir hesabı favoriye/sessize almak için 📋 Liste → hesaba dokun.</i>",
    ]
    kb = []
    if fav:
        lines.append(f"\n⭐ <b>Favoriler ({len(fav)})</b>")
        for u in fav[:10]:
            kb.append([btn(f"⭐ @{u}" + (" 🟢" if is_connected(u) else " ⚪"), f"ui:{u}")])
    if mut:
        lines.append(f"\n🔕 <b>Sessizler ({len(mut)})</b>")
        for u in mut[:10]:
            kb.append([btn(f"🔕 @{u}", f"ui:{u}")])
    if not fav and not mut:
        lines.append("\n📭 Henüz favori veya sessiz hesap yok.")
    kb.append([btn("📋 Liste", "list:0"), btn("🏠 Menü", "menu")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


def render_cleanup() -> Screen:
    dead = dead_users()
    lines = ["🧹 <b>TEMİZLİK</b>", DIV]
    if not dead:
        lines.append(f"\n✨ Harika! {DEAD_DAYS} gündür hiç bağlanamayan hesap yok.")
        kb = [[btn("🏠 Menü", "menu")]]
    else:
        lines.append(f"\n⚠️ <b>{len(dead)}</b> hesap {DEAD_DAYS} gündür hiç bağlanamadı (yayın açmıyor olabilir):\n")
        lines += [f"• @{esc(u)}" for u in dead[:20]]
        if len(dead) > 20:
            lines.append(f"… ve {len(dead) - 20} tane daha")
        lines.append("\nHepsini listeden silmek ister misin?")
        kb = [[btn(f"🗑 Hepsini Sil ({len(dead)})", "cleanup_ok")], [btn("🏠 Menü", "menu")]]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


def render_settings() -> Screen:
    on = S["bot_active"]
    text = (
        "⚙️ <b>AYARLAR</b>\n"
        f"{DIV}\n"
        "⚖️ <b>Min Ratio</b> — coin ÷ kişi bunun altındaysa bildirim gitmez\n"
        "💎 <b>Min Coin</b> — sandık coini bunun altındaysa gitmez\n"
        "⏱ <b>Gecikme</b> — sandık yakalandıktan kaç sn sonra bildirim atılır\n"
        f"{DIV}\n"
        "❓ <b>Bilinmeyen sandık</b> — TikTok coin/kişi bilgisi göndermediyse bildirilsin mi\n"
        f"{DIV}\n"
        "<i>⭐ Favori hesaplar filtreye takılmaz.</i>"
    )
    kb = [
        [btn(f"🔔 Bildirim: {'🟢 AÇIK' if on else '🔴 KAPALI'}", "toggle")],
        [btn("➖", "ratio:dn"), btn(f"⚖️ Ratio {S['min_ratio']:g}x", "noop"), btn("➕", "ratio:up")],
        [btn("➖", "coin:dn"), btn(f"💎 Coin {S['min_coins']}", "noop"), btn("➕", "coin:up")],
        [btn("➖", "delay:dn"), btn(f"⏱ Gecikme {S['delay']}sn", "noop"), btn("➕", "delay:up")],
        [btn(f"❓ Bilinmeyen sandık: {'🟢 Bildir' if S['notify_unknown'] else '🔴 Gizle'}", "unk")],
        [btn("🔄 Tümünü Yeniden Bağla", "reconnect_all")],
        [btn("🗑 Listeyi Temizle", "clear")],
        [btn("🏠 Menü", "menu")],
    ]
    return text, InlineKeyboardMarkup(kb)


def render_userinfo(u: str) -> Screen:
    now = time.time()
    d = STATS.get("per_user", {}).get(u, {})
    lines = [f"👤 <b>@{esc(u)}</b>{tag(u)}", DIV]
    if is_connected(u):
        v = viewer_counts.get(u, 0)
        lines.append(f"🟢 <b>Canlı</b> · bağlı {fmt_dur(now - connection_times[u])}")
        lines.append(f"{heat(v)} İzleyici: <b>{fmt_num(v) if v else '?'}</b>")
    else:
        lines.append("⚪ <b>Bağlı değil</b> (yayında olmayabilir)")
    lines.append(f"📦 Yakalanan sandık: <b>{d.get('boxes', 0)}</b>")
    if d.get("last"):
        lines.append(f"🕒 Son sandık: {fmt_dur(now - d['last'])} önce")
    fav_on, mut_on = u in S["favorites"], u in S["muted"]
    kb = [
        [InlineKeyboardButton("🔗 Yayına Git", url=f"https://www.tiktok.com/@{u}/live")],
        [
            btn("⭐ Favoriden çıkar" if fav_on else "⭐ Favori yap", f"fav:{u}"),
            btn("🔔 Sesi aç" if mut_on else "🔕 Sessize al", f"mute:{u}"),
        ],
        [btn("❌ Listeden Çıkar", f"rm:{u}:0"), btn("🔙 Liste", "list:0")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


HELP_TEXTS = {
    "main": f"ℹ️ <b>YARDIM</b>\n{DIV}\nBir konu seç 👇",
    "cmds": (
        f"📌 <b>KOMUTLAR</b>\n{DIV}\n"
        "🏠 /menu — Ana menü\n"
        "➕ /add @isim @isim2 — Ekle (toplu olur)\n"
        "➖ /remove @isim — Çıkar\n"
        "📋 /list — Liste\n"
        "📊 /status — Durum\n"
        "📈 /stats — İstatistik\n"
        "⚙️ /settings — Ayarlar\n"
        "🧪 /test — Gruba test mesajı\n"
        "🎯 /setratio 1.5 · /setcoins 10 · /setdelay 120\n"
        "🆔 /id — Sohbet ID\n"
        "↩️ /rollback — Önceki kod sürümüne dön\n\n"
        "📄 <b>Kod güncelleme:</b> Gruba bir <code>.py</code> dosyası gönder, bot kendini onunla değiştirip yeniden başlar (sadece yönetici)."
    ),
    "add": (
        f"➕ <b>KOLAY EKLEME</b>\n{DIV}\n"
        "1️⃣ <b>➕ Ekle</b> butonuna bas, isimleri veya linkleri gönder.\n"
        "2️⃣ Butona basmadan <code>tiktok.com/@isim</code> linki atarsan da otomatik eklenir.\n"
        "3️⃣ Aynı mesaja istediğin kadar isim yazabilirsin."
    ),
    "box": (
        f"🎁 <b>SANDIK SİSTEMİ</b>\n{DIV}\n"
        "Bot sandığı yakalayınca ayarladığın gecikmeden sonra gruba bildirim atar.\n\n"
        "⚖️ Ratio = coin ÷ kişi\n"
        "🔥 ratio ≥ 2 · 🔥🔥 ratio ≥ 3\n"
        "⭐ Favori hesaplar filtreye takılmaz · 🔕 Sessizler hiç bildirim atmaz\n"
        "❓ TikTok bazen coin/kişi göndermez; o zaman <b>?</b> yazar ve filtre uygulanmaz\n"
        "🔁 Bağlı olmayan hesapları bot 5 dakikada bir kendisi tekrar dener\n"
        f"🧹 {DEAD_DAYS} gündür hiç bağlanamayan hesaplar Temizlik ekranında önerilir"
    ),
}


def render_help(section: str = "main") -> Screen:
    kb = [
        [btn("📌 Komutlar", "help:cmds"), btn("➕ Ekleme", "help:add")],
        [btn("🎁 Sandık Sistemi", "help:box")],
        [btn("🏠 Menü", "menu")],
    ]
    return HELP_TEXTS.get(section, HELP_TEXTS["main"]), InlineKeyboardMarkup(kb)


def render_clear_confirm() -> Screen:
    kb = [[btn("✅ Evet, Temizle", "clear_ok"), btn("❌ İptal", "settings")]]
    return f"⚠️ <b>DİKKAT</b>\n{DIV}\n<b>{len(WATCH_LIST)}</b> hesabın hepsi silinecek. Emin misin?", InlineKeyboardMarkup(kb)


def render_add_prompt() -> Screen:
    text = (
        f"➕ <b>YAYINCI EKLE</b>\n{DIV}\n"
        "Kullanıcı adlarını veya TikTok linklerini gönder.\n"
        "Birden fazla olabilir, boşluk veya alt satırla ayır.\n\n"
        "💡 <i>Örnek:</i> <code>@isim1 @isim2 tiktok.com/@isim3</code>"
    )
    return text, InlineKeyboardMarkup([[btn("❌ İptal", "menu")]])


# ================== TELEGRAM YARDIMCILARI ==================
async def edit_screen(query, screen: Screen):
    text, markup = screen
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def reply_screen(update: Update, screen: Screen):
    text, markup = screen
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def remove_reply_keyboard(update: Update):
    """Eski alttaki kalıcı klavyeyi kaldırır (mesaj gönderip hemen siler)."""
    try:
        m = await update.effective_message.reply_text("⌨️", reply_markup=ReplyKeyboardRemove())
        await m.delete()
    except Exception as e:
        logger.debug(f"Klavye kaldırılamadı: {e}")


# ================== KULLANICI EKLE/ÇIKAR ==================
def add_users(names: List[str], bot) -> Tuple[List[str], List[str]]:
    added = [n for n in names if n not in WATCH_LIST]
    existed = [n for n in names if n in WATCH_LIST]
    if added:
        WATCH_LIST.update(added)
        now = time.time()
        for n in added:
            STATS["meta"].setdefault(n, {"added": now, "last_ok": None})
        save_watchlist()
        save_stats()
        spawn(connect_many(added, bot))
    return added, existed


async def remove_user_now(username: str):
    WATCH_LIST.discard(username)
    save_watchlist()
    client = clients.pop(username, None)
    connection_times.pop(username, None)
    viewer_counts.pop(username, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass


def add_result_screen(added: List[str], existed: List[str]) -> Screen:
    lines = [f"➕ <b>EKLEME SONUCU</b>", DIV]
    if added:
        lines.append(f"✅ <b>Eklendi ({len(added)}):</b>")
        lines.append(", ".join(f"@{esc(n)}" for n in added[:20]))
        if len(added) > 20:
            lines.append(f"… ve {len(added) - 20} tane daha")
        lines.append("🔌 Bağlantılar sırayla kuruluyor…")
    if existed:
        lines.append(f"\n♻️ <b>Zaten listede ({len(existed)}):</b>")
        lines.append(", ".join(f"@{esc(n)}" for n in existed[:10]))
    if not added and not existed:
        lines.append("⚠️ Geçerli bir kullanıcı adı bulunamadı.")
    kb = [[btn("➕ Yine Ekle", "add"), btn("📋 Liste", "list:0")], [btn("🏠 Menü", "menu")]]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


# ================== KOMUTLAR ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and not S["admin_ids"]:
        S["admin_ids"].append(user.id)
        save_settings()
    await remove_reply_keyboard(update)
    await reply_screen(update, render_main())


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remove_reply_keyboard(update)
    await reply_screen(update, render_main())


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_screen(update, render_list(0))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_screen(update, render_status())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_screen(update, render_stats())


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_screen(update, render_settings())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_screen(update, render_help("main"))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.effective_message.reply_text(f"Chat ID: {chat.id}\nTip: {chat.type}")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text("⛔ Bu işlem için yetkin yok.")
        return
    if not context.args:
        context.chat_data["await"] = "add"
        await reply_screen(update, render_add_prompt())
        return
    names = extract_names(" ".join(context.args), loose=True)
    await reply_screen(update, add_result_screen(*add_users(names, context.application.bot)))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text("⛔ Bu işlem için yetkin yok.")
        return
    if not context.args:
        await reply_screen(update, render_list(0, remove_mode=True))
        return
    names = extract_names(" ".join(context.args), loose=True)
    removed = [n for n in names if n in WATCH_LIST]
    for n in removed:
        await remove_user_now(n)
    msg = ("❌ Çıkarıldı: " + ", ".join(f"@{esc(n)}" for n in removed)) if removed else "Listede bulunamadı."
    await update.effective_message.reply_text(msg, parse_mode="HTML")


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, err = await send_test(context.application.bot)
    if ok:
        await update.effective_message.reply_text("✅ Test mesajı gruba gönderildi.")
    else:
        await update.effective_message.reply_text(
            f"❌ Hata: <code>{esc(err)}</code>\n\nBotun gruba ekli ve yetkili olduğundan emin ol.",
            parse_mode="HTML",
        )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = reconnect_offline(context.application.bot)
    await update.effective_message.reply_text(f"🔄 {n} hesap için bağlantı başlatıldı.")


async def cmd_setratio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        S["min_ratio"] = max(0.0, float(context.args[0].replace(",", ".")))
        save_settings()
        await update.effective_message.reply_text(f"✅ Min ratio: {S['min_ratio']:g}x")
    except Exception:
        await update.effective_message.reply_text("Kullanım: /setratio 1.5")


async def cmd_setcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        S["min_coins"] = max(0, int(context.args[0]))
        save_settings()
        await update.effective_message.reply_text(f"✅ Min coin: {S['min_coins']}")
    except Exception:
        await update.effective_message.reply_text("Kullanım: /setcoins 10")


async def cmd_setdelay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        S["delay"] = max(0, min(3600, int(context.args[0])))
        save_settings()
        await update.effective_message.reply_text(f"✅ Gecikme: {S['delay']}sn")
    except Exception:
        await update.effective_message.reply_text("Kullanım: /setdelay 120")


async def send_test(bot) -> Tuple[bool, str]:
    try:
        await bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text="✅ <b>Test mesajı</b>\n\nBot gruba başarıyla yazabiliyor.",
            parse_mode="HTML",
        )
        return True, ""
    except Exception as e:
        return False, str(e)


# ================== KOD YÜKLEME / YENİDEN BAŞLATMA ==================
def restart_self():
    logger.info("Bot yeniden başlatılıyor…")
    logging.shutdown()
    os.execv(sys.executable, [sys.executable] + sys.argv)


def strict_admin(user_id: Optional[int]) -> bool:
    """Kod yükleme gibi tehlikeli işlemler: yönetici listesi boşsa kimseye izin yok."""
    return bool(S["admin_ids"]) and user_id in S["admin_ids"]


async def doc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    doc = msg.document if msg else None
    if not doc or not (doc.file_name or "").lower().endswith(".py"):
        return
    uid = update.effective_user.id if update.effective_user else None
    if not strict_admin(uid):
        await msg.reply_text("⛔ Kod yükleme sadece yönetici içindir. (Önce /start ile yönetici ol)")
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await msg.reply_text("⚠️ Dosya çok büyük (en fazla 2 MB).")
        return

    try:
        tg_file = await doc.get_file()
        raw = bytes(await tg_file.download_as_bytearray())
        source = raw.decode("utf-8")
        compile(source, doc.file_name, "exec")  # sadece sözdizimi kontrolü
        if "def main" not in source:
            raise ValueError("Dosyada main() bulunamadı, bot kodu gibi görünmüyor.")
    except Exception as e:
        await msg.reply_text(
            f"❌ <b>Dosya kabul edilmedi</b>\n<code>{esc(str(e)[:400])}</code>\n\nÇalışan bot değiştirilmedi.",
            parse_mode="HTML",
        )
        return

    target = os.path.abspath(sys.argv[0])
    try:
        shutil.copy2(target, target + ".bak")
        tmp = target + ".new"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, target)
        save_json(RESTART_NOTICE_FILE, {"chat_id": update.effective_chat.id, "old": BOT_VERSION})
    except Exception as e:
        await msg.reply_text(f"❌ Dosya yazılamadı: <code>{esc(e)}</code>", parse_mode="HTML")
        return

    new_v = hashlib.md5(raw).hexdigest()[:6]
    await msg.reply_text(
        f"✅ <b>Yeni kod alındı</b>\n{DIV}\n"
        f"📄 {esc(doc.file_name)} · {source.count(chr(10)) + 1} satır\n"
        f"🧩 Sürüm: <code>{BOT_VERSION}</code> ➜ <code>{new_v}</code>\n"
        "🔄 Bot yeniden başlatılıyor…\n\n"
        "<i>Açılmazsa Termux'ta: cp " + esc(os.path.basename(target)) + ".bak " + esc(os.path.basename(target)) + "</i>",
        parse_mode="HTML",
    )
    await asyncio.sleep(1.5)
    restart_self()


async def cmd_rollback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not strict_admin(uid):
        await update.effective_message.reply_text("⛔ Sadece yönetici.")
        return
    target = os.path.abspath(sys.argv[0])
    backup = target + ".bak"
    if not os.path.exists(backup):
        await update.effective_message.reply_text("Yedek (.bak) dosyası yok.")
        return
    try:
        shutil.copy2(backup, target)
        save_json(RESTART_NOTICE_FILE, {"chat_id": update.effective_chat.id, "old": BOT_VERSION})
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Geri alınamadı: {e}")
        return
    await update.effective_message.reply_text("↩️ Önceki sürüme dönülüyor, bot yeniden başlıyor…")
    await asyncio.sleep(1.5)
    restart_self()


# ================== METİN MESAJLARI ==================
LEGACY_BUTTONS = {
    "📋 Liste": cmd_list,
    "📊 Durum": cmd_status,
    "➕ Ekle": cmd_add,
    "➖ Çıkar": cmd_remove,
    "🔔 Bildirim": cmd_settings,
    "⚙️ Ayarlar": cmd_settings,
    "📈 İstatistik": cmd_stats,
    "ℹ️ Yardım": cmd_help,
    "🧪 Test": cmd_test,
    "🔄 Yenile": cmd_refresh,
}


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    uid = update.effective_user.id if update.effective_user else None

    # Eski alt klavyenin butonlarına basılırsa: klavyeyi kaldır ve ilgili ekranı aç
    if text in LEGACY_BUTTONS:
        await remove_reply_keyboard(update)
        context.args = []
        await LEGACY_BUTTONS[text](update, context)
        return

    if not is_admin(uid):
        return

    if context.chat_data.get("await") == "add":
        context.chat_data.pop("await", None)
        names = extract_names(text, loose=True)
        await reply_screen(update, add_result_screen(*add_users(names, context.application.bot)))
        return

    # Menüye basmadan link atılırsa otomatik ekle
    if "tiktok.com/@" in text.lower():
        names = extract_names(text)
        if names:
            await reply_screen(update, add_result_screen(*add_users(names, context.application.bot)))


# ================== INLINE BUTONLAR ==================
MUTATING = ("add", "toggle", "ratio:", "coin:", "delay:", "rm:", "clear", "reconnect", "test",
            "fav:", "mute:", "cleanup_ok", "unk")


def step(value, direction: str, delta, lo, hi):
    value = value + delta if direction == "up" else value - delta
    return max(lo, min(hi, value))


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    uid = query.from_user.id if query.from_user else None

    if data.startswith(MUTATING) and not is_admin(uid):
        await query.answer("⛔ Bu işlem için yetkin yok.", show_alert=True)
        return

    toast: Optional[str] = None
    screen: Optional[Screen] = None
    bot = context.application.bot

    if data == "noop":
        await query.answer()
        return

    if data == "menu":
        context.chat_data.pop("await", None)
        screen = render_main()

    elif data.startswith("list:"):
        parts = data.split(":")
        screen = render_list(int(parts[1]), remove_mode=len(parts) > 2 and parts[2] == "rm")

    elif data.startswith("ui:"):
        screen = render_userinfo(data[3:])

    elif data.startswith("rm:"):
        parts = data.split(":")
        user, page = parts[1], int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await remove_user_now(user)
        toast = f"❌ @{user} çıkarıldı"
        screen = render_list(page, remove_mode=len(parts) > 3 and parts[3] == "rm")

    elif data == "status":
        screen = render_status()

    elif data == "stats":
        screen = render_stats()

    elif data == "settings":
        screen = render_settings()

    elif data.startswith("help:"):
        screen = render_help(data[5:])

    elif data == "add":
        context.chat_data["await"] = "add"
        screen = render_add_prompt()

    elif data == "toggle":
        S["bot_active"] = not S["bot_active"]
        save_settings()
        toast = "🔔 Bildirimler açıldı" if S["bot_active"] else "🔕 Bildirimler kapandı"
        screen = render_settings()

    elif data.startswith("ratio:"):
        S["min_ratio"] = round(step(S["min_ratio"], data[6:], 0.5, 0.0, 50.0), 2)
        save_settings()
        screen = render_settings()

    elif data.startswith("coin:"):
        S["min_coins"] = int(step(S["min_coins"], data[5:], 10, 0, 100000))
        save_settings()
        screen = render_settings()

    elif data.startswith("delay:"):
        S["delay"] = int(step(S["delay"], data[6:], 15, 0, 600))
        save_settings()
        screen = render_settings()

    elif data == "reconnect":
        n = reconnect_offline(bot)
        toast = f"🔄 {n} hesap bağlanıyor…" if n else "Hepsi zaten bağlı ✅"
        screen = render_main()

    elif data == "reconnect_all":
        n = await reconnect_everyone(bot)
        toast = f"🔄 {n} hesap yeniden bağlanıyor…"
        screen = render_settings()

    elif data == "clear":
        screen = render_clear_confirm()

    elif data == "clear_ok":
        for u in list(WATCH_LIST):
            await remove_user_now(u)
        toast = "🗑 Liste temizlendi"
        screen = render_main()

    elif data == "unk":
        S["notify_unknown"] = not S["notify_unknown"]
        save_settings()
        toast = "❓ Bilinmeyenler bildirilecek" if S["notify_unknown"] else "❓ Bilinmeyenler gizlendi"
        screen = render_settings()

    elif data == "hist":
        screen = render_history()

    elif data == "favs":
        screen = render_favs()

    elif data == "cleanup":
        screen = render_cleanup()

    elif data == "cleanup_ok":
        dead = dead_users()
        for u in dead:
            await remove_user_now(u)
        toast = f"🧹 {len(dead)} hesap silindi"
        screen = render_main()

    elif data.startswith("fav:") or data.startswith("mute:"):
        kind, user = data.split(":", 1)
        key = "favorites" if kind == "fav" else "muted"
        other = "muted" if kind == "fav" else "favorites"
        if user in S[key]:
            S[key].remove(user)
            toast = "Kaldırıldı"
        else:
            S[key].append(user)
            if user in S[other]:
                S[other].remove(user)  # favori ve sessiz aynı anda olamaz
            toast = "⭐ Favoriye eklendi" if kind == "fav" else "🔕 Sessize alındı"
        save_settings()
        screen = render_userinfo(user)

    elif data == "test":
        ok, err = await send_test(bot)
        await query.answer("✅ Gruba gönderildi" if ok else f"❌ {err[:150]}", show_alert=not ok)
        return

    else:
        await query.answer()
        return

    await query.answer(toast)
    if screen:
        await edit_screen(query, screen)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        logger.error(
            "⚠️ Aynı token ile BAŞKA bir bot kopyası çalışıyor! "
            "Diğer Termux oturumlarını kapat: pkill -f python  (sonra botu tek kez başlat)"
        )
    elif isinstance(err, NetworkError):
        logger.warning(f"Ağ hatası (geçici): {err}")
    else:
        logger.error(f"Beklenmeyen hata: {err}", exc_info=err)


# ================== BİLDİRİM ==================
async def send_box_notification(bot, env_id: str):
    delay = S["delay"]
    if delay > 0:
        await asyncio.sleep(delay)

    box = pending_boxes.get(env_id)
    if not box or box["sent"]:
        return
    box["sent"] = True

    if not S["bot_active"]:
        return

    unique_id = box["user"]
    if unique_id in S["muted"]:
        logger.info(f"Sessiz hesap, bildirim atılmadı @{unique_id}")
        return

    coins, people, sender = box["coins"], box["people"], box["sender"]
    viewers = viewer_counts.get(unique_id, 0)  # gecikmeden SONRA güncel değeri al
    is_fav = unique_id in S["favorites"]

    known = coins > 0 and people > 0
    ratio = round(coins / people, 2) if known else 0.0

    if not known and not S["notify_unknown"] and not is_fav:
        logger.info(f"Bilinmeyen sandık gizlendi @{unique_id}")
        return

    # Coin/kişi bilgisi TikTok'tan gelmediyse filtre uygulanamaz, yine de bildir. Favoriler filtreye takılmaz.
    if known and not is_fav and (ratio < S["min_ratio"] or coins < S["min_coins"]):
        logger.info(f"Filtreye takıldı @{unique_id} ratio={ratio} coins={coins}")
        return

    coins_s, people_s = (str(coins), str(people)) if known else ("?", "?")
    ratio_s = f"{ratio:g}x" if known else "?"
    viewers_s = f"{heat(viewers)} {fmt_num(viewers)}" if viewers else "👀 ?"
    badge = ratio_badge(ratio) if known else "🎁"
    now = datetime.now().strftime("%H:%M:%S")
    url = f"https://www.tiktok.com/@{unique_id}/live"

    lines = [
        f"{badge} <b>SANDIK BİLDİRİMİ</b> 🎁{' ⭐' if is_fav else ''}",
        DIV,
        f"🆔 <b>TT-ID</b> ▸ <code>@{esc(unique_id)}</code>",
        f"⏳ <b>TIME</b> ▸ <code>{now}</code>",
        DIV,
        f"🎁 <b>BOX</b> ▸ <code>{coins_s}/{people_s}</code>",
        f"👥 <b>People</b> ▸ <code>{people_s}</code>",
        f"💎 <b>Diamonds</b> ▸ <code>{coins_s}</code>",
        f"⚖️ <b>Ratio</b> ▸ <code>{ratio_s}</code> {'<code>' + bar(ratio, 4) + '</code>' if known else ''}",
        f"{viewers_s.split(' ')[0]} <b>Viewers</b> ▸ <code>{viewers_s.split(' ', 1)[1]}</code>",
    ]
    if sender and sender != "bilinmiyor":
        lines.append(f"👤 <b>Sender</b> ▸ <code>{esc(sender)}</code>")
    lines += [DIV, "🚀 <i>Hemen katıl, sandık açılmadan kap!</i>"]
    text = "\n".join(lines)
    plain = (
        f"SANDIK BİLDİRİMİ\n\n@{unique_id}\nBOX: {coins_s}/{people_s}\n"
        f"Viewers: {viewers or '?'}\nRatio: {ratio_s}\n{url}"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Yayına Git", url=url)]])

    try:
        try:
            await bot.send_message(chat_id=GROUP_CHAT_ID, text=text, parse_mode="HTML", reply_markup=markup)
        except BadRequest as parse_err:
            logger.warning(f"HTML gönderilemedi, düz metin deneniyor: {parse_err}")
            await bot.send_message(chat_id=GROUP_CHAT_ID, text=plain, reply_markup=markup)
        STATS["total_notifications_sent"] += 1
        STATS["history"].insert(0, {
            "t": time.time(), "user": unique_id, "coins": coins, "people": people, "viewers": viewers,
        })
        del STATS["history"][30:]
        save_stats()
        logger.info(f"Bildirim gitti → @{unique_id} ({coins_s}/{people_s}, viewers={viewers})")
    except Exception as e:
        logger.error(f"Telegram bildirim hatası: {e}")


# ================== TİKTOK ==================
VIEWER_FIELDS = ("viewer_count", "total", "m_total", "total_user")


def find_int(obj, keys=("user_count", "viewer_count", "total_user"), depth: int = 0) -> int:
    """İç içe dict/list içinde ilk sıfırdan büyük sayıyı arar (oda bilgisi için)."""
    if depth > 5:
        return 0
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        for v in obj.values():
            r = find_int(v, keys, depth + 1)
            if r:
                return r
    elif isinstance(obj, (list, tuple)):
        for v in obj[:20]:
            r = find_int(v, keys, depth + 1)
            if r:
                return r
    return 0


def read_viewers(event) -> int:
    # 1) doğrudan alan adları
    for name in VIEWER_FIELDS:
        try:
            v = int(getattr(event, name, 0) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return v
    # 2) alan adı farklıysa olayın metin gösteriminden çek
    text = repr(event)
    for name in VIEWER_FIELDS:
        m = re.search(rf"(?<![\w]){name}=(\d+)", text)
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return 0


def read_envelope(event) -> Tuple[str, int, int, str]:
    info = getattr(event, "envelope_info", None) or getattr(event, "treasure_box_data", None)
    coins = people = 0
    sender = "bilinmiyor"
    if info:
        coins = getattr(info, "diamond_count", None) or getattr(info, "coins", None) or 0
        people = getattr(info, "people_count", None) or getattr(info, "can_open", None) or 0
        sender = getattr(info, "send_user_name", None) or sender
    user = getattr(event, "user", None) or getattr(event, "treasure_box_user", None)
    if user and sender == "bilinmiyor":
        sender = (
            getattr(user, "unique_id", None) or getattr(user, "nickname", None) or "bilinmiyor"
        )
    env_id = str(
        getattr(info, "envelope_id", "")
        or getattr(getattr(event, "common", None), "msg_id", "")
        or ""
    )
    return env_id, int(coins or 0), int(people or 0), str(sender)


async def start_client(unique_id: str, bot):
    if unique_id in clients or unique_id in connecting or unique_id not in WATCH_LIST:
        return
    connecting.add(unique_id)
    client = TikTokLiveClient(unique_id=f"@{unique_id}")

    @client.on(ConnectEvent)
    async def on_connect(_: ConnectEvent):
        now = time.time()
        connection_times[unique_id] = now
        STATS["meta"].setdefault(unique_id, {"added": now, "last_ok": None})["last_ok"] = now
        save_stats()
        logger.info(f"Bağlandı: @{unique_id}")
        # Başlangıç izleyici sayısı (oda bilgisinden), sonra RoomUserSeq olayları günceller
        try:
            room = getattr(client, "room_info", None)
            count = find_int(room) if room else 0
            if count:
                viewer_counts.setdefault(unique_id, count)
            else:
                debug_dump("ROOMINFO", room, limit=2, size=1500)
        except Exception as e:
            logger.debug(f"Oda bilgisi okunamadı: {e}")

    @client.on(DisconnectEvent)
    async def on_disconnect(_: DisconnectEvent):
        logger.info(f"Bağlantı koptu: @{unique_id}")
        clients.pop(unique_id, None)
        connection_times.pop(unique_id, None)
        await asyncio.sleep(12)
        if unique_id in WATCH_LIST:
            spawn(start_client(unique_id, bot))

    @client.on(RoomUserSeqEvent)
    async def on_viewers(event: RoomUserSeqEvent):
        debug_dump("VIEWERS", event, limit=3, size=1200)
        v = read_viewers(event)
        if v:
            viewer_counts[unique_id] = v
        else:
            debug_dump("VIEWERS_0", event, limit=3, size=1200)

    if LinkMicBattleEvent is not None:
        @client.on(LinkMicBattleEvent)
        async def on_battle(event):
            debug_dump("BATTLE", event, limit=3, size=3000)

    @client.on(EnvelopeEvent)
    async def on_envelope(event: EnvelopeEvent):
        try:
            env_id, coins, people, sender = read_envelope(event)
            if not env_id:
                env_id = f"{unique_id}-{time.time()}"
            _info = getattr(event, "envelope_info", None)
            logger.info(
                f"Envelope olayı @{unique_id} id={env_id[-6:]} display={getattr(event, 'display', None)} "
                f"tür={getattr(_info, 'business_type', None)} coin={coins} kişi={people}"
            )

            box = pending_boxes.get(env_id)
            if box:
                # Aynı sandığın sonradan gelen (dolu) verisi ilkini günceller, tekrar bildirim atmaz
                if coins and not box["coins"]:
                    box["coins"] = coins
                if people and not box["people"]:
                    box["people"] = people
                if sender != "bilinmiyor" and box["sender"] == "bilinmiyor":
                    box["sender"] = sender
                return

            if not coins or not people:
                logger.warning(f"Sandık verisi 0 geldi @{unique_id}: {repr(event)[:600]}")

            pending_boxes[env_id] = {
                "user": unique_id, "coins": coins, "people": people, "sender": sender, "sent": False,
            }
            while len(pending_boxes) > 500:
                pending_boxes.pop(next(iter(pending_boxes)))

            now = time.time()
            STATS["total_boxes_found"] += 1
            STATS["last_box_time"] = now
            per = STATS["per_user"].setdefault(unique_id, {"boxes": 0, "last": None})
            per["boxes"] += 1
            per["last"] = now
            save_stats()

            spawn(send_box_notification(bot, env_id))
            logger.info(
                f"Sandık yakalandı @{unique_id} → {coins}/{people} display={getattr(event, 'display', None)}"
            )
        except Exception as e:
            logger.error(f"Envelope işleme hatası @{unique_id}: {e}", exc_info=True)

    clients[unique_id] = client
    try:
        await client.start()
    except Exception as e:
        logger.info(f"@{unique_id} bağlanamadı ({type(e).__name__}): {str(e)[:120]}")
        clients.pop(unique_id, None)
        connection_times.pop(unique_id, None)
    finally:
        connecting.discard(unique_id)


async def connect_many(users: List[str], bot):
    """Hesapları arada bekleyerek tek tek bağlar (rate limit koruması)."""
    for u in users:
        if u in WATCH_LIST:
            spawn(start_client(u, bot))
            await asyncio.sleep(CONNECT_STAGGER_SECONDS)


def reconnect_offline(bot) -> int:
    offline = [u for u in sorted(WATCH_LIST) if not is_connected(u) and u not in connecting]
    if offline:
        spawn(connect_many(offline, bot))
    return len(offline)


async def reconnect_everyone(bot) -> int:
    users = sorted(WATCH_LIST)
    for u in users:
        client = clients.pop(u, None)
        connection_times.pop(u, None)
        connecting.discard(u)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
    spawn(connect_many(users, bot))
    return len(users)


async def auto_reconnect_loop(bot):
    await asyncio.sleep(AUTO_RECONNECT_SECONDS)
    while True:
        try:
            n = reconnect_offline(bot)
            if n:
                logger.info(f"Otomatik yeniden bağlanma: {n} hesap denendi")
        except Exception as e:
            logger.error(f"Otomatik bağlanma hatası: {e}")
        await asyncio.sleep(AUTO_RECONNECT_SECONDS)


# ================== ANA ==================
def acquire_lock():
    """Aynı bot iki kez açılmasın (Conflict hatasını önler)."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                old = int((f.read() or "0").strip() or 0)
            if old and old != os.getpid():
                alive = False
                try:
                    os.kill(old, 0)
                    alive = True
                except OSError:
                    pass  # eski süreç ölmüş, kilit bayat
                if alive:
                    # PID başka bir programa geçmiş olabilir: gerçekten Python botu mu bak
                    try:
                        with open(f"/proc/{old}/cmdline", "rb") as f:
                            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore").lower()
                        alive = "python" in cmd
                    except OSError:
                        pass  # okunamadıysa çalışıyor kabul et
                if alive:
                    print(f"❌ Bot zaten çalışıyor (PID {old}). Önce kapat: kill {old}")
                    print("   Kapanmazsa: kill -9 " + str(old) + "   |   kilidi silmek için: rm " + LOCK_FILE)
                    sys.exit(1)
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(LOCK_FILE) and os.remove(LOCK_FILE))
    except SystemExit:
        raise
    except Exception as e:
        logger.warning(f"Kilit dosyası kullanılamadı: {e}")


async def post_init(app: Application):
    commands = [
        BotCommand("menu", "Ana menü"),
        BotCommand("add", "Yayıncı ekle (toplu olur)"),
        BotCommand("remove", "Yayıncı çıkar"),
        BotCommand("list", "Listeyi göster"),
        BotCommand("status", "Durum"),
        BotCommand("stats", "İstatistik"),
        BotCommand("settings", "Ayarlar"),
        BotCommand("test", "Test mesajı"),
        BotCommand("help", "Yardım"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Komut ayarlama hatası: {e}")

    notice = load_json(RESTART_NOTICE_FILE, None)
    if notice and notice.get("chat_id"):
        try:
            await app.bot.send_message(
                chat_id=notice["chat_id"],
                text=(
                    f"✅ <b>Bot yeniden başladı</b>\n🧩 Sürüm: <code>{esc(notice.get('old', '?'))}</code>"
                    f" ➜ <code>{BOT_VERSION}</code>\n📡 {len(WATCH_LIST)} hesap bağlanıyor…"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Yeniden başlama bildirimi gönderilemedi: {e}")
    try:
        os.remove(RESTART_NOTICE_FILE)
    except OSError:
        pass

    # Kayıtlı hesapları arka planda, tek tek bağla (bot hemen cevap versin)
    spawn(connect_many(sorted(WATCH_LIST), app.bot))
    spawn(auto_reconnect_loop(app.bot))


def main():
    acquire_lock()
    load_all()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .post_init(post_init)
        .build()
    )

    for name, fn in [
        ("start", cmd_start), ("menu", cmd_menu), ("help", cmd_help), ("add", cmd_add),
        ("remove", cmd_remove), ("list", cmd_list), ("status", cmd_status), ("test", cmd_test),
        ("id", cmd_id), ("stats", cmd_stats), ("settings", cmd_settings),
        ("setratio", cmd_setratio), ("setcoins", cmd_setcoins), ("setdelay", cmd_setdelay),
        ("refresh", cmd_refresh),
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CommandHandler("rollback", cmd_rollback))
    app.add_handler(MessageHandler(filters.Document.ALL, doc_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)

    logger.info("Bot başlatıldı...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=-1,  # internet gelene kadar tekrar dene
    )


if __name__ == "__main__":
    main()
