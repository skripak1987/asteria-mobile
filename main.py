# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════╗
║  ASTERIA MOBILE v1.0 — автономний торговий бот для Android    ║
║  Торгівля (BingX) • Саморозвиток (автопілот + AI) • Статистика ║
║  Один файл, Kivy-інтерфейс, без numpy/pandas/matplotlib        ║
╚══════════════════════════════════════════════════════════════╝
Режими: PAPER (віртуальні угоди, за замовчуванням) і LIVE (реальні ордери BingX).
Дані: ціни Binance (публічні), торгівля: BingX через ccxt (якщо ccxt є в збірці).
"""

import os
import sys
import json
import time
import math
import threading
import traceback
from datetime import datetime
from collections import deque

try:
    import requests
except Exception:          # без requests бот не працює, але імпорт не валить інтерфейс
    requests = None

APP_VERSION = "1.0"

# ════════════════════════════════════════════════════════
#  ПЛАТФОРМА І ШЛЯХИ (на Android — приватне сховище застосунку, переживає перезапуск)
# ════════════════════════════════════════════════════════
IS_ANDROID = ("ANDROID_ARGUMENT" in os.environ) or ("ANDROID_PRIVATE" in os.environ)


def _resolve_data_dir():
    if IS_ANDROID:
        base = os.environ.get("ANDROID_PRIVATE") or os.path.expanduser("~")
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "asteria_data")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = base
    return d


STATS_DIR = _resolve_data_dir()


def load_json_safe(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json_safe(path, data):
    """Атомарний запис (через .tmp), щоб різке вимкнення не псувало файл."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass


def _clean(o):
    """Приводить значення до звичайних, придатних для JSON (NaN/inf → None)."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, bool):
        return o
    if isinstance(o, float):
        return None if (o != o or o in (float("inf"), float("-inf"))) else round(o, 6)
    return o


# ════════════════════════════════════════════════════════
#  НАЛАШТУВАННЯ (зберігаються у asteria_data/)
# ════════════════════════════════════════════════════════
TUNING_FILE    = os.path.join(STATS_DIR, "tuning_settings.json")
SNAPSHOTS_FILE = os.path.join(STATS_DIR, "trade_snapshots.json")
TRADE_CFG_FILE = os.path.join(STATS_DIR, "saturn_trade.cfg")      # ключі BingX
TG_CFG_FILE    = os.path.join(STATS_DIR, "telegram.cfg")
_CFG_FILE      = os.path.join(STATS_DIR, "asteria_api.cfg")       # провайдер/ключ/модель AI
ACTIVE_FILE    = os.path.join(STATS_DIR, "active_trade.json")     # відкрита угода (відновлення після перезапуску)
STATE_FILE     = os.path.join(STATS_DIR, "engine_state.json")

TUNING = {"trail_enabled": True, "slope_ext_enabled": True, "float_enabled": True,
          "rr_filter_enabled": False, "rr_min": 2.5,
          "live_mode": False, "amount": 10.0, "leverage": 1, "use_sl": True,
          "autopilot_enabled": True, "digest_enabled": True, "ai_audit_enabled": False, "digest_hour": 9,
          "heartbeat_hours": 6, "autostart": True}
TUNING.update(load_json_safe(TUNING_FILE, {}))


def save_tuning():
    save_json_safe(TUNING_FILE, TUNING)


TRADE_CFG = load_json_safe(TRADE_CFG_FILE, {"api_key": "", "api_secret": ""})
TG_CFG = load_json_safe(TG_CFG_FILE, {"token": "", "chat_id": ""})
_API_CFG = load_json_safe(_CFG_FILE, {"provider": "anthropic", "key": "", "model": ""})
CLAUDE_API_KEY = _API_CFG.get("key", "")
AI_PROVIDER = _API_CFG.get("provider", "anthropic")
AI_MODEL = _API_CFG.get("model", "")

PROVIDER_DEFAULTS = {
    "anthropic":  {"url": "https://api.anthropic.com/v1/messages",
                   "model": "claude-haiku-4-5-20251001", "label": "Claude (Anthropic)"},
    "openai":     {"url": "https://api.openai.com/v1/chat/completions",
                   "model": "gpt-4o-mini", "label": "OpenAI (GPT)"},
    "gemini":     {"url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                   "model": "gemini-2.0-flash", "label": "Google Gemini"},
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions",
                   "model": "google/gemini-flash-1.5", "label": "OpenRouter"},
}

SCAN_TIMEFRAME = "5m"
WINDOW_SIZE = 35
MIN_POTENTIAL = 0.45
NEAR_MISS_TOLERANCE_PCT = 0.20
TRAP_TIMEOUT_SEC = 3600
ELITE_WHITELIST = [
    "NEAR/USDT", "BTC/USDT", "UNI/USDT", "XRP/USDT", "SOL/USDT", "LINK/USDT", "DOT/USDT", "ADA/USDT",
    "AVAX/USDT", "MATIC/USDT", "ATOM/USDT", "LTC/USDT", "APT/USDT", "SUI/USDT", "OP/USDT", "ARB/USDT",
    "FIL/USDT", "ICP/USDT", "HBAR/USDT", "XLM/USDT", "RENDER/USDT", "GRT/USDT", "AAVE/USDT", "EGLD/USDT",
    "FLOW/USDT", "THETA/USDT", "ALGO/USDT", "FTM/USDT", "IMX/USDT", "STX/USDT", "GALA/USDT", "FIDA/USDT",
]

# ── консольний журнал для інтерфейсу ──
LOG_BUFFER = deque(maxlen=300)
_real_print = print


def print(*args, **kwargs):        # noqa: A001 — перехоплюємо print, щоб показувати журнал у застосунку
    text = " ".join(str(a) for a in args)
    LOG_BUFFER.append(f"{datetime.now().strftime('%H:%M:%S')} {text}")
    try:
        _real_print(*args, **kwargs)
    except Exception:
        pass


def send_tg(text):
    """Telegram (токен і chat_id задаються в Налаштуваннях). Не блокує, помилки ігнорує."""
    token, chat = (TG_CFG.get("token") or "").strip(), (TG_CFG.get("chat_id") or "").strip()
    if not token or not chat or requests is None:
        return
    def _w():
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=8)
        except Exception:
            pass
    threading.Thread(target=_w, daemon=True).start()


# ════════════════════════════════════════════════════════
#  МАТЕМАТИКА (чистий Python, без numpy)
#  Свічка: [ts_ms, open, high, low, close, volume]
# ════════════════════════════════════════════════════════
def parse_tf_minutes(tf_str):
    try:
        t = tf_str.lower().strip()
        if "m" in t:
            return int(t.replace("m", ""))
        if "h" in t:
            return int(t.replace("h", "")) * 60
        if "d" in t:
            return int(t.replace("d", "")) * 1440
        if "w" in t:
            return int(t.replace("w", "")) * 10080
    except Exception:
        pass
    return 5


def lower_tf(tf_str):
    m = parse_tf_minutes(tf_str)
    return {5: "3m", 15: "5m", 30: "15m", 60: "15m"}.get(m, "1m")


def tf_multipliers(tf_str):
    m = parse_tf_minutes(tf_str)
    if m <= 5:
        return 1.6, 2.5
    if m <= 15:
        return 3.5, 4.0
    if m <= 60:
        return 5.0, 5.5
    return 7.0, 7.5


def _std(vals):
    n = len(vals)
    if n == 0:
        return 0.0
    mu = sum(vals) / n
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / n)


def _solve3(m, y):
    """Розв'язує систему 3×3 методом Гаусса."""
    a = [row[:] + [y[i]] for i, row in enumerate(m)]
    n = 3
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(a[r][i]))
        if abs(a[p][i]) < 1e-18:
            return None
        a[i], a[p] = a[p], a[i]
        for r in range(i + 1, n):
            f = a[r][i] / a[i][i]
            for c in range(i, n + 1):
                a[r][c] -= f * a[i][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (a[i][n] - sum(a[i][j] * x[j] for j in range(i + 1, n))) / a[i][i]
    return x


def polyfit2(ys):
    """Квадратична регресія y = a·x² + b·x + c, x = 0..n-1 (як numpy.polyfit(x, y, 2))."""
    n = len(ys)
    xs = list(range(n))
    s = lambda p: float(sum(x ** p for x in xs))
    sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sx2y = sum(x * x * y for x, y in zip(xs, ys))
    sol = _solve3([[s(4), s(3), s(2)], [s(3), s(2), s(1)], [s(2), s(1), float(n)]], [sx2y, sxy, sy])
    return tuple(sol) if sol else (0.0, 0.0, ys[-1] if ys else 0.0)


def calc_gravity(rows, tf_str):
    """Крива «гравітації»: (ціна, дно кривої, відстань до rmax, лофт, кривизна, множник тейку, множник стопу)."""
    closes = [r[4] for r in rows][-WINDOW_SIZE:]
    n = len(closes)
    a, b, c = polyfit2(closes)
    trough = [a * x * x + b * x + c for x in range(n)]
    dist_rmax = (WINDOW_SIZE - 1) - (-b / (2 * a)) if a != 0 else 999
    loft = _std([cl - t for cl, t in zip(closes, trough)])
    take_mult, stop_mult = tf_multipliers(tf_str)
    return float(closes[-1]), float(trough[-1]), dist_rmax, float(loft), float(a), take_mult, stop_mult


def calc_adx(rows, period=14):
    """ADX за Вайлдером у спрощеному вигляді (ковзне середнє), як у десктопній версії."""
    try:
        n = len(rows)
        if n < 2 * period + 1:
            return 22.5
        tr, pdm, mdm = [], [], []
        for i in range(n):
            h, l = rows[i][2], rows[i][3]
            if i == 0:
                tr.append(h - l); pdm.append(0.0); mdm.append(0.0)
                continue
            pc, ph, pl = rows[i - 1][4], rows[i - 1][2], rows[i - 1][3]
            tr.append(max(h - l, abs(h - pc), abs(l - pc)))
            up, dn = h - ph, pl - l
            pdm.append(up if (up > dn and up > 0) else 0.0)
            mdm.append(dn if (dn > up and dn > 0) else 0.0)
        dx = [None] * n
        for i in range(period - 1, n):
            atr = sum(tr[i - period + 1:i + 1]) / period
            if atr <= 0:
                continue
            pdi = 100 * (sum(pdm[i - period + 1:i + 1]) / period) / atr
            mdi = 100 * (sum(mdm[i - period + 1:i + 1]) / period) / atr
            if pdi + mdi <= 0:
                continue
            dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi)
        tail = dx[-period:]
        if any(v is None for v in tail):
            return 22.5
        return float(sum(tail) / period)
    except Exception:
        return 22.5


def calc_trend_metrics(rows, window=None):
    """Нахил (% за свічку) і «чистота» тренду R² за лінійною регресією."""
    try:
        w = window or WINDOW_SIZE
        c = [r[4] for r in rows][-w:]
        n = len(c)
        mx, my = (n - 1) / 2.0, sum(c) / n
        sxx = sum((x - mx) ** 2 for x in range(n))
        a = sum((x - mx) * (y - my) for x, y in zip(range(n), c)) / sxx
        b = my - a * mx
        ss_res = sum((y - (a * x + b)) ** 2 for x, y in zip(range(n), c))
        ss_tot = sum((y - my) ** 2 for y in c)
        return {"slope_pct_per_candle": a / c[-1] * 100, "r2": (1 - ss_res / ss_tot) if ss_tot > 0 else None}
    except Exception:
        return {}


def calc_volatility_metrics(rows):
    try:
        if len(rows) < 16:
            return {}
        h = [r[2] for r in rows]; l = [r[3] for r in rows]; c = [r[4] for r in rows]
        tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
        last = c[-1]
        rng = [(h[i] - l[i]) / c[i] for i in range(len(c))]
        return {"atr14_pct": sum(tr[-14:]) / 14 / last * 100,
                "avg_range20_pct": sum(rng[-20:]) / 20 * 100,
                "last_range_pct": (h[-1] - l[-1]) / last * 100,
                "window_range_pct": (max(h[-35:]) - min(l[-35:])) / last * 100}
    except Exception:
        return {}


# ════════════════════════════════════════════════════════
#  ДАНІ: публічні свічки Binance (з резервним дзеркалом)
# ════════════════════════════════════════════════════════
class DataProvider:
    HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]

    def __init__(self):
        self._session = requests.Session() if requests else None
        self._host = 0

    @staticmethod
    def _pair(symbol):
        return symbol.split(":")[0].replace("/", "").upper()

    def _get(self, path, params):
        if not self._session:
            return None
        for k in range(len(self.HOSTS)):
            host = self.HOSTS[(self._host + k) % len(self.HOSTS)]
            try:
                r = self._session.get(host + path, params=params, timeout=10)
                if r.status_code == 200:
                    self._host = (self._host + k) % len(self.HOSTS)
                    return r.json()
            except Exception:
                continue
        return None

    def klines(self, symbol, tf, limit=60):
        data = self._get("/api/v3/klines", {"symbol": self._pair(symbol), "interval": tf, "limit": limit})
        if not data or len(data) < 5:
            return None
        try:
            return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in data]
        except Exception:
            return None

    def price(self, symbol):
        d = self._get("/api/v3/ticker/price", {"symbol": self._pair(symbol)})
        try:
            return float(d["price"])
        except Exception:
            return None


DATA = DataProvider()



# ════ ЗНІМКИ УГОД ════
class TradeSnapshots:
    """Сховище знімків угод: відкриті живуть у пам'яті й оновлюються,
    закриті дописуються у файл. Потокобезпечне (Radar і UI в різних потоках)."""
    MAX_CLOSED = 100   # у пам'яті не більше 100 угод; повна історія — у trade_archive.jsonl

    def __init__(self):
        self._lock = threading.Lock()
        data = load_json_safe(SNAPSHOTS_FILE, [])
        self.closed = data if isinstance(data, list) else []
        self.open = {}
        self.version = 0
        self._seq = max([int(r.get("id", 0)) for r in self.closed] + [0])

    def begin(self, rec):
        rec = _clean(rec)
        with self._lock:
            self._seq += 1
            rec["id"] = self._seq
            rec["status"] = "OPEN"
            self.open[self._seq] = rec
            self.version += 1
            return self._seq

    def touch(self, sid, max_prg=None, min_prg=None):
        if not sid:
            return
        with self._lock:
            r = self.open.get(sid)
            if not r:
                return
            ex = r.setdefault("excursions", {})
            changed = False
            for k, v in (("max_prg", max_prg), ("min_prg", min_prg)):
                if v is not None and ex.get(k) != round(v, 1):
                    ex[k] = round(v, 1)
                    changed = True
            if changed:
                self.version += 1

    def finish(self, sid, result=None, excursions=None, events=None, levels=None):
        if not sid:
            return
        with self._lock:
            r = self.open.pop(sid, None)
            if not r:
                return
            r["status"] = "CLOSED"
            r["ts_close"] = datetime.now().isoformat(timespec="seconds")
            r["result"] = _clean(result or {})
            r.setdefault("excursions", {}).update(_clean(excursions or {}))
            r.setdefault("events", {}).update(_clean(events or {}))
            r.setdefault("levels", {}).update(_clean(levels or {}))
            self.closed.append(r)
            if len(self.closed) > self.MAX_CLOSED:
                self.closed = self.closed[-self.MAX_CLOSED:]
            self.version += 1
            snapshot = list(self.closed)
        save_json_safe(SNAPSHOTS_FILE, snapshot)
        try:
            archive_append(r)
        except Exception:
            pass

    def closed_tail(self, n):
        """Останні n закритих знімків (у хронологічному порядку)."""
        with self._lock:
            return list(self.closed[-n:])

    def rows(self):
        with self._lock:
            return list(self.open.values()) + list(self.closed)


TRADE_SNAPSHOTS = TradeSnapshots()


# ════ ПОПЛАВОК (адаптивний режим ризику) ════
SLOPE_THRESHOLD       = 0.12   # |нахил| кривої, % за свічку, вище якого стоп розширюється
SLOPE_STOP_MULTIPLIER = 1.5    # у скільки разів розширюється стандартний стоп
SLOPE_STOP_MIN_ATR    = 1.0    # розширений стоп не тісніший за стільки ATR(14); 0 = вимкнено
SLOPE_KEEP_RISK       = True   # у реальних угодах зменшувати суму на коефіцієнт розширення (ризик у $ не росте)

FLOAT_WINDOW          = 100    # скільки останніх закритих угод аналізує поплавок
FLOAT_DEF_LOSS_STREAK = 3      # серія збитків -> ЗАХИСНИЙ режим
FLOAT_DEF_BREAKS      = 3      # ранніх зламів структури серед останніх FLOAT_BREAK_WINDOW -> ЗАХИСНИЙ
FLOAT_BREAK_WINDOW    = 5
FLOAT_RECOVERY_WINS   = 2      # серія плюсів для виходу із ЗАХИСНОГО режиму
FLOAT_AGG_WIN_STREAK  = 4      # серія плюсів для переходу в АГРЕСИВНИЙ режим
FLOAT_AGG_WINRATE     = 0.70   # ... за умови такої частки плюсів серед останніх FLOAT_AGG_LOOKBACK
FLOAT_AGG_LOOKBACK    = 10
FLOAT_MODE_UA = {"DEFENSIVE": "ЗАХИСНИЙ", "NEUTRAL": "НЕЙТРАЛЬНИЙ", "AGGRESSIVE": "АГРЕСИВНИЙ"}
# ефекти режимів (амбітний режим тільки ЗНІМАЄ частину запобіжників, захисний — додає)
FLOAT_EFFECTS = {
    "DEFENSIVE":  {"min_pot_mult": 1.30, "amount_cap": 1,    "be_trigger_mult": 0.80},
    "NEUTRAL":    {"min_pot_mult": 1.00, "amount_cap": None, "be_trigger_mult": 1.00},
    "AGGRESSIVE": {"min_pot_mult": 0.90, "amount_cap": None, "be_trigger_mult": 1.00},
}
FLOAT_EFFECT_LABELS = {"min_pot_mult": "множник мін. потенціалу", "amount_cap": "межа множника суми",
                       "be_trigger_mult": "множник порога беззбитку"}


def apply_slope_stop_extension(direction, entry, stop, slope_pct, atr_abs=0.0,
                               threshold=None, multiplier=None, min_atr=None):
    """Якщо |нахил| кривої вищий за поріг — розширює стоп: відстань × множник,
    але не тісніше за min_atr × ATR. Повертає (новий_стоп, коефіцієнт_розширення).
    Коефіцієнт 1.0 — стоп не змінено. Параметри читаються з констант у момент виклику."""
    threshold  = SLOPE_THRESHOLD if threshold is None else threshold
    multiplier = SLOPE_STOP_MULTIPLIER if multiplier is None else multiplier
    min_atr    = SLOPE_STOP_MIN_ATR if min_atr is None else min_atr
    if slope_pct is None or entry is None or stop is None:
        return stop, 1.0
    if abs(slope_pct) <= threshold or multiplier <= 1.0:
        return stop, 1.0
    dist = abs(entry - stop)
    if dist <= 0:
        return stop, 1.0
    new_dist = max(dist * multiplier, (atr_abs or 0.0) * min_atr)
    if new_dist <= dist:
        return stop, 1.0
    new_stop = entry - new_dist if direction == "LONG" else entry + new_dist
    return new_stop, new_dist / dist


def parse_rr_input(text):
    """"1:2.5" / "2,5" / "2:5" -> 2.5 (прибутку на 1 ризику); None — некоректне значення."""
    t = str(text).strip().replace(",", ".").replace(" ", "")
    try:
        if ":" in t:
            a, b = t.split(":", 1)
            a, b = float(a), float(b)
            v = (b / a) if a > 0 else None
        else:
            v = float(t)
    except ValueError:
        return None
    return v if (v is not None and 0 < v <= 100) else None


def format_rr(v):
    return f"1:{float(v):g}"


MTF_PULLBACK_K = 0.05   # зсув углиб від межі нижчого ТФ (у частках loft) — як у Radar


def calc_pullback_entry(direction, price, p_l, loft_l, cv_l, tm_l):
    """Ціна входу "пастки": відкат за кривою нижчого ТФ проти напрямку сигналу.
    Повертає (вхід, чи_це_відкат)."""
    if direction == "LONG" and cv_l < 0:
        e = (p_l - tm_l * loft_l) + loft_l * MTF_PULLBACK_K
        if e < price:
            return e, True
    elif direction == "SHORT" and cv_l > 0:
        e = (p_l + tm_l * loft_l) - loft_l * MTF_PULLBACK_K
        if e > price:
            return e, True
    return price, False


def float_classify(rec):
    """Результат закритого знімка для поплавка: win / loss / break / flat,
    None — не рахується (скасована пастка, скид, перемикання монети, вхід не відбувся)."""
    r = rec.get("result") or {}
    pnl = r.get("pnl_pct")
    outcome = str(r.get("outcome") or "")
    if pnl is None or r.get("entered") is False:
        return None
    if any(w in outcome for w in ("ПАСТКА", "ПЕРЕМКНУТО", "СКИНУТО")):
        return None
    if "ЗЛОМ" in outcome:
        return "break"
    if "БЕЗЗБИТОК" in outcome and not r.get("tp1_done"):
        return "flat"
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "flat"


def float_replay(tokens):
    """Машина станів поплавка по хронологічному списку результатів.
    Повертає (режим, {n, loss_streak, win_streak}, [переходи]).
    Стан завжди відновлюється з історії, тому не потребує окремого збереження."""
    mode, loss_s, win_s = "NEUTRAL", 0, 0
    trans = []
    for i, t in enumerate(tokens):
        if t in ("loss", "break"):
            loss_s += 1; win_s = 0
        elif t == "win":
            win_s += 1; loss_s = 0
        recent = tokens[max(0, i - FLOAT_BREAK_WINDOW + 1): i + 1]
        breaks = sum(1 for x in recent if x == "break")
        new, why = mode, None
        if mode != "DEFENSIVE" and loss_s >= FLOAT_DEF_LOSS_STREAK:
            new, why = "DEFENSIVE", f"серія збитків: {loss_s}"
        elif mode != "DEFENSIVE" and breaks >= FLOAT_DEF_BREAKS:
            new, why = "DEFENSIVE", f"ранні злами структури: {breaks} з останніх {len(recent)}"
        elif mode == "DEFENSIVE" and win_s >= FLOAT_RECOVERY_WINS:
            new, why = "NEUTRAL", f"серія плюсів: {win_s}"
        elif mode == "NEUTRAL" and win_s >= FLOAT_AGG_WIN_STREAK:
            last = [x for x in tokens[max(0, i - FLOAT_AGG_LOOKBACK + 1): i + 1] if x != "flat"]
            wr = (sum(1 for x in last if x == "win") / len(last)) if last else 0.0
            if wr >= FLOAT_AGG_WINRATE:
                new, why = "AGGRESSIVE", f"серія плюсів: {win_s}, частка плюсів {wr:.0%}"
        elif mode == "AGGRESSIVE" and t in ("loss", "break"):
            new, why = "NEUTRAL", "збиток після серії плюсів"
        if new != mode:
            trans.append({"i": i, "from": mode, "to": new, "reason": why})
            mode = new
    return mode, {"n": len(tokens), "loss_streak": loss_s, "win_streak": win_s}, trans


class AdaptiveFloatManager:
    """Адаптивний поплавок: режим ризику (ЗАХИСНИЙ / НЕЙТРАЛЬНИЙ / АГРЕСИВНИЙ)
    за останніми FLOAT_WINDOW закритими угодами зі сховища знімків."""

    def __init__(self, snapshots):
        self.snap = snapshots
        self._lock = threading.Lock()
        self._ver = None
        self._announced = False
        self.mode = "NEUTRAL"
        self._info = {"n": 0, "loss_streak": 0, "win_streak": 0}
        self._reason = "—"

    def refresh(self):
        v = self.snap.version
        if v == self._ver:
            return
        with self._lock:
            if v == self._ver:
                return
            self._ver = v
            tokens = [t for t in (float_classify(r) for r in self.snap.closed_tail(FLOAT_WINDOW * 3)) if t][-FLOAT_WINDOW:]
            mode, info, trans = float_replay(tokens)
            prev = self.mode
            self.mode, self._info = mode, info
            if trans:
                self._reason = trans[-1]["reason"]
            if not self._announced:
                self._announced = True
                print(f"[ПОПЛАВОК] старт: {FLOAT_MODE_UA[mode]} | вікно: {info['n']}/{FLOAT_WINDOW} угод")
            elif mode != prev:
                print(f"[ПОПЛАВОК] {FLOAT_MODE_UA[prev]} → {FLOAT_MODE_UA[mode]} | {self._reason} | "
                      f"вікно: {info['n']}/{FLOAT_WINDOW} угод, серія збитків {info['loss_streak']}, плюсів {info['win_streak']}")

    def effects(self):
        self.refresh()
        if not TUNING.get("float_enabled", True):
            return FLOAT_EFFECTS["NEUTRAL"]
        return FLOAT_EFFECTS[self.mode]

    def min_potential(self):
        return MIN_POTENTIAL * self.effects()["min_pot_mult"] * GOVERNOR.adj("min_pot_adj")

    def amount_cap(self):
        return self.effects()["amount_cap"] or AMOUNT_MULT_MAX

    def be_trigger(self):
        return BE_TRIGGER_PCT * self.effects()["be_trigger_mult"] * GOVERNOR.adj("be_trigger_adj")

    def status(self):
        self.refresh()
        return {"mode": self.mode, "enabled": bool(TUNING.get("float_enabled", True)),
                "reason": self._reason, "effects": self.effects(), **self._info}


ADAPTIVE_FLOAT = AdaptiveFloatManager(TRADE_SNAPSHOTS)


# ════ ПАРАМЕТРИ УГОДИ ════
BE_TRIGGER_PCT      = 31.0   # угода пройшла стільки % шляху до тейку -> стоп у беззбиток
BE_BUFFER_PCT       = 0.10   # невеликий плюс над входом (% від ціни входу), покриває комісії
AMOUNT_MULT_MAX     = 10     # суму можна збільшити максимум у стільки разів
AMOUNT_MULT_MIN_RR  = 2.0    # збільшуємо суму лише коли RR не нижче
AMOUNT_MIN_STOP_PCT = 0.15   # тісніший стоп (% від ціни) вважаємо шумом — суму не збільшуємо
FREE_RESERVE        = 0.95   # не використовуємо більше цієї частки вільного балансу


def calc_progress_pct(direction, entry, take, price):
    """Скільки % шляху від входу до тейку пройдено (мінус — ціна пішла проти)."""
    dist = abs(take - entry)
    if dist <= 0:
        return 0.0
    sign = 1 if direction == "LONG" else -1
    return sign * (price - entry) / dist * 100.0


def calc_be_stop(direction, entry, price, buf_pct=BE_BUFFER_PCT):
    """Новий стоп: вхід + невеликий плюс. Плюс не більший за половину
    поточного прибутку, щоб стоп не опинився впритул до ціни."""
    sign = 1 if direction == "LONG" else -1
    profit = sign * (price - entry)
    if profit <= 0:
        return None
    buf = min(entry * buf_pct / 100.0, profit * 0.5)
    return entry + sign * buf


def calc_amount_mult(rr, stop_pct, cap=AMOUNT_MULT_MAX, min_rr=AMOUNT_MULT_MIN_RR,
                     min_stop_pct=AMOUNT_MIN_STOP_PCT):
    """Множник суми від RR: 1 якщо RR невисокий або стоп підозріло тісний."""
    if rr is None or stop_pct is None:
        return 1
    if rr < min_rr or stop_pct < min_stop_pct:
        return 1
    return int(max(1, min(cap, rr)))


TP1_PATH_PCT    = 70.0   # перший тейк: стільки % шляху від входу до повного тейку
TP1_CLOSE_SHARE = 0.70   # яку частку позиції закриває перший тейк (решта біжить до повного)


def calc_tp1_price(direction, entry, take, path_pct=TP1_PATH_PCT):
    """Ціна першого тейку: path_pct% шляху від входу до повного тейку."""
    return entry + (take - entry) * path_pct / 100.0


def blend_exit_price(share, tp1_price, final_exit):
    """Середня ціна виходу при частковому закритті: share закрито на TP1,
    решта — по final_exit. PnL лінійний за ціною, тож середній вихід
    дає точний сумарний результат."""
    return share * tp1_price + (1.0 - share) * final_exit


def clip_amount(desired, base, free, reserve=FREE_RESERVE):
    """Обмежує суму вільним балансом. Баланс невідомий -> лишаємо введену суму."""
    if free is None:
        return min(desired, base)
    return max(0.0, min(desired, free * reserve))




# ════ BingX (ccxt, лише для LIVE) ════
class BingXTrader:
    """Торговий модуль для BingX Futures через ccxt"""
    BASE = "https://open-api.bingx.com"

    def __init__(self):
        self._ex    = None
        self._ready = False
        self._init()

    def _init(self):
        try:
            import ccxt
            self._ex = ccxt.bingx({
                "apiKey":  TRADE_CFG.get("api_key","").strip(),
                "secret":  TRADE_CFG.get("api_secret","").strip(),
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},  # ф'ючерси
            })
            self._ready = bool(TRADE_CFG.get("api_key","").strip())
        except: self._ready = False

    @staticmethod
    def _to_ccxt_symbol(symbol: str) -> str:
        """Уніфікований символ для ф'ючерсів BingX через ccxt —
        обов'язково зі слешем між базовою і котирувальною валютою:
        "NEAR/USDT" -> "NEAR/USDT:USDT". Раніше слеш прибирався
        (.replace("/","")), через що ЖОДЕН виклик до біржі не міг
        спрацювати — API просто не розпізнавав такий символ."""
        base_quote = symbol if "/" in symbol else symbol.replace("USDT", "/USDT")
        return f"{base_quote}:USDT"

    def _ensure_markets(self):
        try:
            self._ex.load_markets()
        except Exception as e:
            print(f"⚠ load_markets: {e}")

    def _hedged(self, sym):
        """Режим позицій акаунта (hedge чи one-way) — від нього залежать
        параметри ордерів. Визначаємо один раз і запам'ятовуємо."""
        cached = getattr(self, "_hedged_cache", None)
        if cached is not None:
            return cached
        try:
            r = self._ex.fetch_position_mode(sym)
            self._hedged_cache = bool(r.get("hedged"))
            return self._hedged_cache
        except Exception as e:
            print(f"⚠ Не вдалось визначити режим позицій (hedge/one-way): {e} — вважаю one-way")
            return False

    def _p(self, sym, **extra):
        """Параметри ордера з урахуванням режиму позицій акаунта."""
        p = dict(extra)
        if self._hedged(sym):
            p["hedged"] = True
        return p

    def set_leverage(self, symbol, leverage, direction=None):
        """ccxt для BingX ОБОВ'ЯЗКОВО вимагає параметр side (LONG/SHORT/BOTH),
        без нього set_leverage одразу кидає помилку. В one-way режимі це
        BOTH, в hedge — сторона угоди."""
        try:
            sym = self._to_ccxt_symbol(symbol)
            self._ensure_markets()
            if self._hedged(sym):
                sides = [direction] if direction in ("LONG", "SHORT") else ["LONG", "SHORT"]
            else:
                sides = ["BOTH"]
            for s in sides:
                self._ex.set_leverage(leverage, sym, {"side": s})
            return True
        except Exception as e:
            print(f"⚠ set_leverage({symbol}): {e}")
            return False

    def set_margin_mode(self, symbol, mode="isolated"):
        """Режим маржі на BingX ставиться ОКРЕМИМ викликом (не параметром
        ордера). Якщо по символу вже є відкрита позиція, біржа може
        відмовити — це не критично, але виводимо попередження."""
        try:
            sym = self._to_ccxt_symbol(symbol)
            self._ensure_markets()
            self._ex.set_margin_mode(mode, sym)
            return True
        except Exception as e:
            print(f"⚠ set_margin_mode({symbol}, {mode}): {e}")
            return False

    def get_free_usdt(self):
        """Вільний USDT на ф'ючерсному акаунті; None, якщо дізнатись не вдалось."""
        try:
            bal = self._ex.fetch_balance()
            free = (bal.get("USDT") or {}).get("free")
            return float(free) if free is not None else None
        except Exception as e:
            print(f"⚠ fetch_balance: {e}")
            return None

    def open_order(self, symbol, direction, amount_usdt, leverage, tp_price,
                   sl_price=None, use_sl=True, fallback_amount=None):
        """Відкриває маркет-ордер (ізольована маржа) + виставляє TP і SL.
        Повертає (ok, oid_або_помилка, ціна, qty, extra)."""
        try:
            sym = self._to_ccxt_symbol(symbol)

            if not self.set_leverage(symbol, leverage, direction):
                return False, "Не вдалось встановити плече — перевір символ/права ключа", 0.0, 0.0, {}
            if not self.set_margin_mode(symbol, "isolated"):
                print("⚠ Ізольований режим не підтверджено — перевір на біржі режим маржі")

            # Сума: бажана (вже масштабована) обмежується вільним балансом
            free = self.get_free_usdt()
            base = fallback_amount if fallback_amount else amount_usdt
            amount_real = clip_amount(amount_usdt, base, free)
            if amount_real < 1.0:
                return False, f"Недостатньо вільних коштів (вільно: {free})", 0.0, 0.0, {}

            side = "buy" if direction == "LONG" else "sell"
            price = float(self._ex.fetch_ticker(sym)["last"])
            raw_qty = (amount_real * leverage) / price
            qty = float(self._ex.amount_to_precision(sym, raw_qty))
            if qty <= 0:
                return False, "Розрахована кількість — нуль (замала сума)", 0.0, 0.0, {}

            # Усі розрахунки цін і кількостей — ДО маркет-ордера. Якщо щось
            # тут впаде (наприклад, кількість менша за крок точності),
            # позиція ще не відкрита. Інакше помилка ПІСЛЯ ордера лишила б
            # відкриту позицію без TP/SL і без нагляду програми.
            tp_price_adj = float(self._ex.price_to_precision(sym, tp_price))
            sl_price_adj = float(self._ex.price_to_precision(sym, sl_price)) if sl_price else None
            tp_side = "sell" if direction == "LONG" else "buy"
            try:
                qty1 = float(self._ex.amount_to_precision(sym, qty * TP1_CLOSE_SHARE))
                qty2 = float(self._ex.amount_to_precision(sym, qty - qty1))
                tp1_adj = float(self._ex.price_to_precision(sym, calc_tp1_price(direction, price, tp_price_adj)))
                two_level = qty1 > 0 and qty2 > 0 and tp1_adj != tp_price_adj
            except Exception:
                qty1 = qty2 = 0.0
                tp1_adj = None
                two_level = False

            order = self._ex.create_order(sym, "market", side, qty, params=self._p(sym))
            oid = order.get("id", "")
            extra = {"tp_id": None, "sl_id": None, "amount_used": amount_real,
                     "tp1_id": None, "tp1": None, "qty1": 0.0, "qty2": qty}

            # Дворівневий тейк: TP1 закриває TP1_CLOSE_SHARE позиції на
            # TP1_PATH_PCT% шляху, TP2 — решту на повному тейку. Якщо
            # позиція замала для поділу або ціни збіглись — один тейк.
            if two_level:
                try:
                    o1 = self._ex.create_order(sym, "limit", tp_side, qty1, tp1_adj,
                                               params=self._p(sym, reduceOnly=True))
                    extra.update(tp1_id=o1.get("id"), tp1=tp1_adj, qty1=qty1, qty2=qty2)
                except Exception as e:
                    print(f"⚠ TP1-ордер не виставлено ({sym}): {e} — ставлю один тейк")
                    two_level = False
            try:
                o = self._ex.create_order(sym, "limit", tp_side, (qty2 if two_level else qty), tp_price_adj,
                                          params=self._p(sym, reduceOnly=True))
                extra["tp_id"] = o.get("id")
            except Exception as e:
                print(f"⚠ TP-ордер не виставлено ({sym}): {e}")

            if use_sl and sl_price_adj:
                try:
                    o = self._ex.create_order(sym, "stop_market", tp_side, qty, None,
                                              params=self._p(sym, stopPrice=sl_price_adj, reduceOnly=True))
                    extra["sl_id"] = o.get("id")
                except Exception as e:
                    print(f"⚠ SL-ордер не виставлено ({sym}): {e}")

            return True, oid, price, qty, extra
        except Exception as e:
            return False, str(e), 0.0, 0.0, {}

    def move_stop(self, symbol, direction, qty, new_stop, old_sl_id):
        """Переносить стоп: СПОЧАТКУ ставить новий, і лише потім скасовує
        старий — так позиція жодної миті не лишається без стопу.
        Повертає (ok, new_sl_id, new_stop_adj)."""
        try:
            sym = self._to_ccxt_symbol(symbol)
            self._ensure_markets()
            tp_side = "sell" if direction == "LONG" else "buy"
            stop_adj = float(self._ex.price_to_precision(sym, new_stop))
            o = self._ex.create_order(sym, "stop_market", tp_side, qty, None,
                                      params=self._p(sym, stopPrice=stop_adj, reduceOnly=True))
            new_id = o.get("id")
        except Exception as e:
            print(f"⚠ Не вдалось виставити новий стоп ({symbol}): {e} — старий лишається")
            return False, None, None
        if old_sl_id:
            try:
                self._ex.cancel_order(old_sl_id, sym)
            except Exception as e:
                print(f"⚠ Старий стоп не скасовано ({symbol}): {e} — перевір ордери на біржі")
        return True, new_id, stop_adj

    def cancel_all(self, symbol):
        """Прибирає всі відкриті ордери по символу (осиротілі TP/SL після закриття)."""
        try:
            sym = self._to_ccxt_symbol(symbol)
            self._ex.cancel_all_orders(sym)
        except Exception as e:
            print(f"⚠ cancel_all_orders({symbol}): {e}")

    def position_size(self, symbol):
        """Розмір відкритої позиції (контрактів); 0.0 — позиції немає; None — не вдалось дізнатись."""
        try:
            sym = self._to_ccxt_symbol(symbol)
            self._ensure_markets()
            total = 0.0
            for p in self._ex.fetch_positions([sym]):
                total += abs(float(p.get("contracts") or 0))
            return total
        except Exception as e:
            print(f"⚠ fetch_positions({symbol}): {e}")
            return None

    def close_order(self, symbol, direction, qty):
        """Закриває позицію маркет-ордером. Якщо позиції вже немає (її
        закрив TP/SL на біржі) — це успіх, лишається прибрати осиротілі
        ордери."""
        try:
            sym = self._to_ccxt_symbol(symbol)
            self._ensure_markets()
            size = self.position_size(symbol)
            if size is not None and size <= 0:
                self.cancel_all(symbol)
                return True
            real_qty = size if size else qty
            side = "sell" if direction == "LONG" else "buy"
            real_qty = float(self._ex.amount_to_precision(sym, real_qty))
            self._ex.create_order(sym, "market", side, real_qty,
                                  params=self._p(sym, reduceOnly=True))
            self.cancel_all(symbol)
            return True
        except Exception as e:
            print(f"⚠ Не вдалось закрити позицію {symbol}: {e}")
            return False

    def last_price(self, symbol):
        try:
            return float(self._ex.fetch_ticker(self._to_ccxt_symbol(symbol))["last"])
        except Exception:
            return None

    def get_balance(self):
        """Баланс ф'ючерсного акаунта в USDT: {total, free, used}; None — не вдалось."""
        try:
            u = self._ex.fetch_balance().get("USDT") or {}
            return {"total": float(u.get("total") or 0.0), "free": float(u.get("free") or 0.0),
                    "used": float(u.get("used") or 0.0)}
        except Exception as e:
            print(f"⚠ баланс BingX: {e}")
            return None

    def place_limit_entry(self, symbol, direction, amount_usdt, leverage, entry_price, fallback_amount=None):
        """Лімітний вхід для "ловлі пастки": ордер чекає на біржі на ціні відкату,
        TP/SL ставляться ПІСЛЯ заповнення (attach_exits).
        Повертає (ok, order_id_або_помилка, qty, extra)."""
        try:
            sym = self._to_ccxt_symbol(symbol)
            if not self.set_leverage(symbol, leverage, direction):
                return False, "Не вдалось встановити плече — перевір символ/права ключа", 0.0, {}
            if not self.set_margin_mode(symbol, "isolated"):
                print("⚠ Ізольований режим не підтверджено — перевір на біржі режим маржі")
            free = self.get_free_usdt()
            base = fallback_amount if fallback_amount else amount_usdt
            amount_real = clip_amount(amount_usdt, base, free)
            if amount_real < 1.0:
                return False, f"Недостатньо вільних коштів (вільно: {free})", 0.0, {}
            entry_adj = float(self._ex.price_to_precision(sym, entry_price))
            qty = float(self._ex.amount_to_precision(sym, (amount_real * leverage) / entry_adj))
            if qty <= 0:
                return False, "Розрахована кількість — нуль (замала сума)", 0.0, {}
            side = "buy" if direction == "LONG" else "sell"
            order = self._ex.create_order(sym, "limit", side, qty, entry_adj, params=self._p(sym))
            return True, order.get("id", ""), qty, {"amount_used": amount_real, "entry_adj": entry_adj}
        except Exception as e:
            return False, str(e), 0.0, {}

    def check_entry(self, symbol, oid):
        """Стан лімітного входу: {status: open|filled|canceled, filled, avg}."""
        try:
            o = self._ex.fetch_order(oid, self._to_ccxt_symbol(symbol))
            st = str(o.get("status") or "").lower()
            status = "filled" if st == "closed" else ("canceled" if st in ("canceled", "cancelled", "expired", "rejected") else "open")
            avg = o.get("average") or o.get("price")
            return {"status": status, "filled": float(o.get("filled") or 0.0), "avg": float(avg) if avg else None}
        except Exception as e:
            print(f"⚠ стан лімітного входу ({symbol}): {e}")
            return {"status": "open", "filled": 0.0, "avg": None, "error": str(e)}

    def cancel_entry(self, symbol, oid):
        try:
            self._ex.cancel_order(oid, self._to_ccxt_symbol(symbol))
            return True
        except Exception as e:
            print(f"⚠ не вдалось скасувати лімітний вхід ({symbol}): {e}")
            return False

    def attach_exits(self, symbol, direction, qty, entry_price, tp_price, sl_price, use_sl=True):
        """Дворівневий тейк і стоп на вже відкриту позицію (після заповнення лімітного входу).
        Повертає extra (tp_id, sl_id, tp1, qty1, qty2...)."""
        extra = {"tp_id": None, "sl_id": None, "tp1_id": None, "tp1": None, "qty1": 0.0, "qty2": qty}
        try:
            sym = self._to_ccxt_symbol(symbol)
            self._ensure_markets()
            tp_adj = float(self._ex.price_to_precision(sym, tp_price))
            sl_adj = float(self._ex.price_to_precision(sym, sl_price)) if sl_price else None
            tp_side = "sell" if direction == "LONG" else "buy"
            two = False
            try:
                qty1 = float(self._ex.amount_to_precision(sym, qty * TP1_CLOSE_SHARE))
                qty2 = float(self._ex.amount_to_precision(sym, qty - qty1))
                tp1_adj = float(self._ex.price_to_precision(sym, calc_tp1_price(direction, entry_price, tp_adj)))
                two = qty1 > 0 and qty2 > 0 and tp1_adj != tp_adj
            except Exception:
                two = False
            if two:
                try:
                    o1 = self._ex.create_order(sym, "limit", tp_side, qty1, tp1_adj,
                                               params=self._p(sym, reduceOnly=True))
                    extra.update(tp1_id=o1.get("id"), tp1=tp1_adj, qty1=qty1, qty2=qty2)
                except Exception as e:
                    print(f"⚠ ТЕЙК-1 не виставлено ({sym}): {e} — ставлю один тейк")
                    two = False
            try:
                o = self._ex.create_order(sym, "limit", tp_side, (extra["qty2"] if two else qty), tp_adj,
                                          params=self._p(sym, reduceOnly=True))
                extra["tp_id"] = o.get("id")
            except Exception as e:
                print(f"⚠ ТЕЙК не виставлено ({sym}): {e}")
            if use_sl and sl_adj:
                try:
                    o = self._ex.create_order(sym, "stop_market", tp_side, qty, None,
                                              params=self._p(sym, stopPrice=sl_adj, reduceOnly=True))
                    extra["sl_id"] = o.get("id")
                except Exception as e:
                    print(f"⚠ СТОП не виставлено ({sym}): {e}")
        except Exception as e:
            print(f"⚠ виходи після входу ({symbol}): {e}")
        return extra

    def get_position(self, symbol):
        """Отримує поточну позицію"""
        try:
            sym = self._to_ccxt_symbol(symbol)
            positions = self._ex.fetch_positions([sym])
            for p in positions:
                if float(p.get("contracts", 0)) > 0:
                    return p
            return None
        except Exception:
            return None





# ════════════════════════════════════════════════════════
#  АВТОПІЛОТ: архів угод, тіньові сигнали, поплавок v2 (губернатор параметрів),
#  автостоп, AI-аудит і щоденний дайджест у Telegram.
#  Ядро Asteria AI НЕ змінює: лише обмежені коригування (множники поверх
#  поплавка) у жорстких межах, з денним лімітом, автовідкатом і автостопом.
# ════════════════════════════════════════════════════════
ARCHIVE_FILE         = os.path.join(STATS_DIR, "trade_archive.jsonl")        # вічний архів закритих угод
SHADOW_FILE          = os.path.join(STATS_DIR, "shadow_signals.json")        # відхилені сигнали (поточні)
SHADOW_ARCHIVE_FILE  = os.path.join(STATS_DIR, "shadow_archive.jsonl")       # вічний архів вирішених тіньових сигналів
AUTOPILOT_STATE_FILE = os.path.join(STATS_DIR, "autopilot_state.json")
AUTOPILOT_LOG_FILE   = os.path.join(STATS_DIR, "autopilot_log.jsonl")

TUNING.setdefault("autopilot_enabled", True)
TUNING.setdefault("digest_enabled", True)
TUNING.setdefault("ai_audit_enabled", False)
TUNING.setdefault("digest_hour", 9)

GOV_SHORT_WINDOW   = 25      # коротке вікно (угод): швидка реакція
GOV_LONG_WINDOW    = 100     # довге вікно (угод): база для порівняння
GOV_MIN_SHORT      = 12      # мінімум угод у короткому вікні для рішення
GOV_BATCH          = 8       # коригування не частіше ніж раз на стільки нових угод (і раз на годину)
GOV_DAILY_CAP      = 0.10    # макс. сумарна зміна множника «мін. потенціал» за добу
GOV_DECAY_PER_DAY  = 0.25    # частка відстані до бази, яку множники «забувають» за добу
GOV_ROLLBACK_AFTER = 15      # через скільки угод перевіряти зміну (автовідкат)
GOV_FREEZE_HOURS   = 24      # заморозка змін після автовідкату
GOV_HALT_LOSS_STREAK = 6     # автостоп: серія збитків/зламів
GOV_HALT_NET_N     = 10      # автостоп: сума результатів останніх N угод ...
GOV_HALT_NET_PCT   = 6.0     # ... не гірша за -X %
GOV_HALT_HOURS     = 3       # тривалість автостопу (повторний за добу ×4)
AUDIT_EVERY_TRADES = 30      # AI-аудит раз на стільки нових угод
AUDIT_MIN_INTERVAL_H = 6     # ... але не частіше, ніж раз на стільки годин
SHADOW_TIMEOUT_H   = 24
SHADOW_NO_FILL_SEC = 3600

# параметр: (мін, макс, крок). Множники діють ПОВЕРХ режиму поплавка; 1.0 = база.
GOV_BOUNDS = {"min_pot_adj": (0.90, 1.30, 0.04),
              "be_trigger_adj": (0.85, 1.15, 0.03)}
GOV_PARAM_UA = {"min_pot_adj": "мін. потенціал входу", "be_trigger_adj": "поріг беззбитку"}

_JSONL_LOCK = threading.Lock()


def append_jsonl(path, obj):
    try:
        with _JSONL_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def read_jsonl_tail(path, n):
    from collections import deque
    out = deque(maxlen=n)
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return list(out)


def _count_lines(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return 0


ARCHIVE_COUNT = [0]


def archive_append(rec):
    """Закрита угода → вічний архів (append-only). У пам'яті лишається лише MAX_CLOSED."""
    append_jsonl(ARCHIVE_FILE, rec)
    ARCHIVE_COUNT[0] += 1


def archive_init():
    if not os.path.exists(ARCHIVE_FILE):          # одноразова міграція наявної історії
        for r in list(TRADE_SNAPSHOTS.closed):
            append_jsonl(ARCHIVE_FILE, r)
    ARCHIVE_COUNT[0] = _count_lines(ARCHIVE_FILE)
    with TRADE_SNAPSHOTS._lock:                   # у пам'яті — не більше 100 угод
        if len(TRADE_SNAPSHOTS.closed) > TRADE_SNAPSHOTS.MAX_CLOSED:
            TRADE_SNAPSHOTS.closed = TRADE_SNAPSHOTS.closed[-TRADE_SNAPSHOTS.MAX_CLOSED:]
            TRADE_SNAPSHOTS.version += 1


archive_init()


def closed_pnl_series(n=GOV_LONG_WINDOW):
    """[(pnl_pct, токен, запис)] для угод, що рахуються поплавком, у хронологічному порядку."""
    out = []
    for rec in TRADE_SNAPSHOTS.closed_tail(n * 2):
        t = float_classify(rec)
        if not t:
            continue
        pnl = (rec.get("result") or {}).get("pnl_pct")
        if pnl is None:
            continue
        out.append((float(pnl), t, rec))
    return out[-n:]


def window_stats(pairs):
    n = len(pairs)
    if not n:
        return {"n": 0, "wr": 0.0, "exp": 0.0, "sum": 0.0}
    wins = sum(1 for p, _, _ in pairs if p > 0)
    s = sum(p for p, _, _ in pairs)
    return {"n": n, "wr": wins / n, "exp": s / n, "sum": s}


def _tf_ms(tf):
    return parse_tf_minutes(tf) * 60000


# ───────────────────────── ТІНЬОВІ СИГНАЛИ ─────────────────────────
class ShadowSignals:
    """Сигнали, які фільтри ВІДХИЛИЛИ (низький потенціал, щит ADX, фільтр R:R).
    Далі окремий потік дивиться на історію свічок і визначає, що було б, якби їх узяли:
    так видно, чи фільтри та адаптація реально допомагають, і є дані для навчання моделі."""
    MAX_RESOLVED = 500

    def __init__(self):
        self._lock = threading.Lock()
        d = load_json_safe(SHADOW_FILE, {})
        d = d if isinstance(d, dict) else {}
        self.pending = d.get("pending", [])
        self.resolved = d.get("resolved", [])
        self._seen = {}
        self._seq = max([int(r.get("id", 0)) for r in self.pending + self.resolved] + [0])
        self._ex = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _save(self):
        save_json_safe(SHADOW_FILE, {"pending": self.pending, "resolved": self.resolved[-self.MAX_RESOLVED:]})

    def log_signal(self, sym, tf, direction, entry, stop, take, reason, feats=None):
        try:
            if not (entry and stop and take) or abs(entry - stop) <= 0:
                return
            sym = str(sym).split(":")[0]
            now = time.time()
            key = (sym, direction, reason)
            if now - self._seen.get(key, 0) < 900:      # не частіше раз на 15 хв на однаковий випадок
                return
            self._seen[key] = now
            dt = datetime.fromtimestamp(now)
            with self._lock:
                self._seq += 1
                rec = {"id": self._seq, "ts": now, "ts_iso": dt.isoformat(timespec="seconds"),
                       "sym": sym, "tf": tf, "direction": direction, "reason": reason,
                       "entry": float(entry), "stop": float(stop), "take": float(take),
                       "rr": abs(take - entry) / abs(entry - stop), "hour": dt.hour, "weekday": dt.weekday(),
                       "feats": _clean(feats or {}), "status": "PENDING"}
                self.pending.append(rec)
                if len(self.pending) > 300:
                    self.pending = self.pending[-300:]
                self._save()
        except Exception:
            pass

    @staticmethod
    def _evaluate(rec, rows, now):
        d = 1 if rec["direction"] == "LONG" else -1
        e, s, t = rec["entry"], rec["stop"], rec["take"]
        t0 = rec["ts"] * 1000 - _tf_ms(rec["tf"])
        filled = False
        for row in rows:
            ts, _o, h, l, c = row[0], row[1], row[2], row[3], row[4]
            if ts < t0:
                continue
            if not filled:
                if l <= e <= h:
                    filled = True
                    if (d == 1 and l <= s) or (d == -1 and h >= s):     # у свічці входу — консервативно
                        return "STOP", s
                elif ts / 1000.0 - rec["ts"] > SHADOW_NO_FILL_SEC:
                    return "NO_FILL", None
                continue
            hit_stop = (l <= s) if d == 1 else (h >= s)
            hit_take = (h >= t) if d == 1 else (l <= t)
            if hit_stop:                                                 # обидва в одній свічці → стоп
                return "STOP", s
            if hit_take:
                return "TAKE", t
        if now - rec["ts"] > SHADOW_TIMEOUT_H * 3600:
            if not filled:
                return "NO_FILL", None
            return "TIMEOUT", float(rows[-1][4]) if rows else e
        return None, None

    def _loop(self):
        time.sleep(30)
        while True:
            try:
                self._resolve_pending()
            except Exception as ex:
                print(f"[ТІНЬ] помилка: {ex}")
            time.sleep(300)

    def _resolve_pending(self):
        with self._lock:
            todo = [r for r in self.pending if time.time() - r["ts"] > 300]
        if not todo:
            return
        groups = {}
        for r in todo:
            groups.setdefault((r["sym"], r["tf"]), []).append(r)
        done = []
        for (sym, tf), recs in groups.items():
            try:
                rows = DATA.klines(sym, tf, 500)
            except Exception:
                continue
            if not rows:
                continue
            for r in recs:
                outcome, px = self._evaluate(r, rows, time.time())
                if outcome is None:
                    continue
                r["status"] = "RESOLVED"
                r["outcome"] = outcome
                r["ts_resolved"] = time.time()
                if px is not None:
                    d = 1 if r["direction"] == "LONG" else -1
                    pnl = d * (px - r["entry"]) / r["entry"] * 100.0
                    risk_pct = abs(r["entry"] - r["stop"]) / r["entry"] * 100.0
                    r["pnl_pct"] = round(pnl, 4)
                    r["r_mult"] = round(pnl / risk_pct, 3) if risk_pct > 0 else None
                    r["exit"] = px
                done.append(r)
        if done:
            with self._lock:
                ids = {r["id"] for r in done}
                self.pending = [r for r in self.pending if r["id"] not in ids]
                self.resolved.extend(done)
                self.resolved = self.resolved[-self.MAX_RESOLVED:]
                self._save()
            for r in done:
                append_jsonl(SHADOW_ARCHIVE_FILE, r)

    def stats(self, since_ts=0.0):
        """Підсумок за причинами відхилення (лише ті, що реально «увійшли» у ціну)."""
        with self._lock:
            res = [r for r in self.resolved if r.get("ts_resolved", 0) >= since_ts]
            npend = len(self.pending)
        by = {}
        for r in res:
            b = by.setdefault(r["reason"], {"rejected": 0, "filled": 0, "wins": 0, "sum": 0.0})
            b["rejected"] += 1
            if r.get("outcome") in ("TAKE", "STOP", "TIMEOUT") and r.get("pnl_pct") is not None:
                b["filled"] += 1
                b["sum"] += r["pnl_pct"]
                if r["pnl_pct"] > 0:
                    b["wins"] += 1
        tot = {"rejected": sum(b["rejected"] for b in by.values()), "filled": sum(b["filled"] for b in by.values()),
               "wins": sum(b["wins"] for b in by.values()), "sum": sum(b["sum"] for b in by.values())}
        return {"by": by, "total": tot, "pending": npend}


SHADOW = ShadowSignals()

REASON_UA = {"LOW_POT": "малий потенціал", "SHIELD": "щит ADX/структури", "RR_FILTER": "фільтр R:R"}


# ───────────────────────── AI (Claude / OpenAI / Gemini / OpenRouter) ─────────────────────────
def ai_complete(prompt, max_tokens=900, system=""):
    """Виклик обраного в «Дайджесті» провайдера. Повертає (текст, помилка)."""
    key, provider = CLAUDE_API_KEY, AI_PROVIDER
    cfg = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["anthropic"])
    model = AI_MODEL or cfg["model"]
    if not key:
        return "", "не заданий API-ключ"
    try:
        if provider == "anthropic":
            body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
            if system:
                body["system"] = system
            r = requests.post(cfg["url"], headers={"Content-Type": "application/json", "x-api-key": key,
                                                   "anthropic-version": "2023-06-01"}, json=body, timeout=60)
            if r.status_code == 200:
                return r.json()["content"][0]["text"].strip(), ""
        elif provider == "gemini":
            body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": max_tokens}}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            r = requests.post(cfg["url"].format(model=model) + f"?key={key}",
                              headers={"Content-Type": "application/json"}, json=body, timeout=60)
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip(), ""
        else:   # openai / openrouter
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
            if provider == "openrouter":
                headers["HTTP-Referer"] = "https://asteria-terminal"
            r = requests.post(cfg["url"], headers=headers,
                              json={"model": model, "max_tokens": max_tokens, "messages": msgs}, timeout=60)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip(), ""
        return "", f"{provider} HTTP {r.status_code}: {r.text[:160]}"
    except Exception as ex:
        return "", str(ex)[:160]


def _extract_json(text):
    try:
        a, b = text.index("{"), text.rindex("}")
        return json.loads(text[a:b + 1])
    except (ValueError, TypeError):
        return {}


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ───────────────────────── ГУБЕРНАТОР ПАРАМЕТРІВ (поплавок v2) ─────────────────────────
class AutopilotGovernor:
    def __init__(self):
        self._lock = threading.RLock()
        self._audit_busy = False
        st = load_json_safe(AUTOPILOT_STATE_FILE, {})
        self.st = {"adj": {k: 1.0 for k in GOV_BOUNDS}, "n_eval": None, "n_halt_eval": None, "n_audit": None,
                   "last_eval_ts": 0.0, "daily": {"day": "", "used": 0.0}, "pending_check": None,
                   "freeze_until": 0.0, "halt_until": 0.0, "halt_times": [], "decay_ts": time.time(),
                   "last_digest_ts": 0.0, "last_audit_ts": 0.0, "last_audit": None, "events": []}
        if isinstance(st, dict):
            self.st.update(st)
        for k in GOV_BOUNDS:
            self.st["adj"].setdefault(k, 1.0)
        n = ARCHIVE_COUNT[0]
        for k in ("n_eval", "n_halt_eval", "n_audit"):
            if self.st.get(k) is None or self.st[k] > n:
                self.st[k] = n

    # ── доступ ──
    def adj(self, name):
        if not TUNING.get("autopilot_enabled", True):
            return 1.0
        return float(self.st["adj"].get(name, 1.0))

    def entries_allowed(self):
        if not TUNING.get("autopilot_enabled", True):
            return True, ""
        left = self.st.get("halt_until", 0.0) - time.time()
        if left > 0:
            return False, f"АВТОСТОП: ще {left / 60:.0f} хв"
        return True, ""

    def clear_halt(self):
        with self._lock:
            self.st["halt_until"] = 0.0
            self._event("автостоп знято вручну")
            self._save()

    def reset_adj(self):
        with self._lock:
            for k in GOV_BOUNDS:
                self.st["adj"][k] = 1.0
            self.st["pending_check"] = None
            self.st["freeze_until"] = 0.0
            self._event("корекції скинуто до бази")
            self._save()

    # ── службове ──
    def _save(self):
        save_json_safe(AUTOPILOT_STATE_FILE, self.st)

    def _event(self, text):
        print(f"[АВТОПІЛОТ] {text}")
        ev = {"ts": time.time(), "t": datetime.now().strftime("%d.%m %H:%M"), "text": text}
        self.st["events"] = (self.st.get("events") or [])[-39:] + [ev]
        append_jsonl(AUTOPILOT_LOG_FILE, ev)

    def _apply_delta(self, name, delta, why, src):
        lo, hi, _step = GOV_BOUNDS[name]
        cur = float(self.st["adj"][name])
        new = min(hi, max(lo, cur + delta))
        if name == "min_pot_adj":
            day = datetime.now().strftime("%Y-%m-%d")
            if self.st["daily"].get("day") != day:
                self.st["daily"] = {"day": day, "used": 0.0}
            room = GOV_DAILY_CAP - self.st["daily"]["used"]
            if abs(new - cur) > room:
                new = cur + (room if new > cur else -room)
        new = round(new, 4)
        if abs(new - cur) < 1e-6:
            return 0.0
        self.st["adj"][name] = new
        if name == "min_pot_adj":
            self.st["daily"]["used"] += abs(new - cur)
        self._event(f"[{src}] {GOV_PARAM_UA[name]}: ×{cur:.3f} → ×{new:.3f} | {why}")
        return new - cur

    def _decay(self, now):
        dt_days = (now - self.st.get("decay_ts", now)) / 86400.0
        if dt_days <= 0:
            return
        f = 1.0 - (1.0 - GOV_DECAY_PER_DAY) ** dt_days
        for k in GOV_BOUNDS:
            a = self.st["adj"][k]
            self.st["adj"][k] = round(a + (1.0 - a) * f, 4)
        self.st["decay_ts"] = now

    def _check_halt(self, pairs, now):
        if now < self.st.get("halt_until", 0.0) or not pairs:
            return
        streak = 0
        for _p, t, _r in reversed(pairs):
            if t in ("loss", "break"):
                streak += 1
            else:
                break
        net = sum(p for p, _, _ in pairs[-GOV_HALT_NET_N:]) if len(pairs) >= GOV_HALT_NET_N else 0.0
        why = None
        if streak >= GOV_HALT_LOSS_STREAK:
            why = f"серія збитків/зламів: {streak}"
        elif net <= -GOV_HALT_NET_PCT:
            why = f"сума останніх {GOV_HALT_NET_N} угод: {net:+.2f}%"
        if not why:
            return
        recent = [t for t in self.st.get("halt_times", []) if now - t < 86400]
        hours = GOV_HALT_HOURS * (4 if recent else 1)
        self.st["halt_until"] = now + hours * 3600
        self.st["halt_times"] = recent + [now]
        self._event(f"АВТОСТОП на {hours} год: {why}")
        try:
            send_tg(f"🛑 <b>ASTERIA АВТОСТОП</b>\nНові входи призупинено на {hours} год.\nПричина: {_esc(why)}\n"
                    f"Зняти: Показники → Автопілот.")
        except Exception:
            pass

    def _check_rollback(self, pairs, n, now):
        pc = self.st.get("pending_check")
        if not pc or n - pc["n_at"] < GOV_ROLLBACK_AFTER:
            return
        after = [p for p, _, _ in pairs[-GOV_ROLLBACK_AFTER:]]
        exp_after = sum(after) / len(after) if after else 0.0
        eb = pc["exp_before"]
        self.st["pending_check"] = None
        if exp_after < eb - max(0.15 * abs(eb), 0.05):
            self.st["adj"].update(pc["prev"])
            self.st["freeze_until"] = now + GOV_FREEZE_HOURS * 3600
            self._event(f"АВТОВІДКАТ: після зміни результат гірший ({exp_after:+.3f}% проти {eb:+.3f}% на угоду); "
                        f"корекції повернуто, заморозка {GOV_FREEZE_HOURS} год")
        else:
            self._event(f"зміну підтверджено: {exp_after:+.3f}% на угоду (було {eb:+.3f}%)")

    def _evaluate(self, pairs, n, now):
        short, long_ = window_stats(pairs[-GOV_SHORT_WINDOW:]), window_stats(pairs)
        if short["n"] < GOV_MIN_SHORT or long_["n"] < 30:
            return
        margin = max(0.15 * abs(long_["exp"]), 0.05)
        direction = 0
        if short["exp"] < long_["exp"] - margin and short["wr"] < long_["wr"] - 0.08:
            direction = +1     # погіршення → затиснути
        elif short["exp"] > long_["exp"] + margin and short["wr"] > long_["wr"] + 0.05:
            direction = -1     # покращення → послабити
        if not direction:
            return
        prev = dict(self.st["adj"])
        why = (f"коротке вікно {short['n']}: WR {short['wr']:.0%}, {short['exp']:+.3f}%/угоду | "
               f"довге {long_['n']}: WR {long_['wr']:.0%}, {long_['exp']:+.3f}%/угоду")
        ch = 0.0
        ch += abs(self._apply_delta("min_pot_adj", direction * (GOV_BOUNDS["min_pot_adj"][2] if direction > 0 else 0.02),
                                    why, "ПОПЛАВОК v2"))
        ch += abs(self._apply_delta("be_trigger_adj", -direction * GOV_BOUNDS["be_trigger_adj"][2], why, "ПОПЛАВОК v2"))
        if ch > 0:
            self.st["pending_check"] = {"n_at": n, "prev": prev, "exp_before": short["exp"]}

    def tick(self):
        if not TUNING.get("autopilot_enabled", True):
            return
        with self._lock:
            now, n = time.time(), ARCHIVE_COUNT[0]
            self._decay(now)
            pairs = closed_pnl_series()
            if n > self.st["n_halt_eval"]:
                self.st["n_halt_eval"] = n
                self._check_halt(pairs, now)
            self._check_rollback(pairs, n, now)
            if (n - self.st["n_eval"] >= GOV_BATCH and now >= self.st.get("freeze_until", 0.0)
                    and now - self.st.get("last_eval_ts", 0.0) >= 3600):
                self.st["n_eval"], self.st["last_eval_ts"] = n, now
                self._evaluate(pairs, n, now)
            self._save()

    # ── AI-аудит ──
    def _audit_prompt(self):
        pairs = closed_pnl_series()
        trades = []
        for p, t, r in pairs[-30:]:
            trades.append({"dir": r.get("direction"), "pnl": round(p, 3), "res": t,
                           "adx": (r.get("trend") or {}).get("adx"),
                           "slope": (r.get("trend") or {}).get("slope_pct_per_candle"),
                           "r2": (r.get("trend") or {}).get("r2"),
                           "pot": (r.get("scores") or {}).get("potential_pct"),
                           "rr": (r.get("scores") or {}).get("rr"),
                           "mfe": (r.get("excursions") or {}).get("max_prg"),
                           "mae": (r.get("excursions") or {}).get("min_prg"),
                           "hour": (r.get("time") or {}).get("hour")})
        sh = SHADOW.stats(time.time() - 7 * 86400)
        payload = {"params": {k: {"now": self.st["adj"][k], "min": GOV_BOUNDS[k][0], "max": GOV_BOUNDS[k][1]}
                              for k in GOV_BOUNDS},
                   "float_mode": ADAPTIVE_FLOAT.mode,
                   "short_window": window_stats(pairs[-GOV_SHORT_WINDOW:]), "long_window": window_stats(pairs),
                   "last_trades": trades, "rejected_signals_7d": sh["by"]}
        return json.dumps(_clean(payload), ensure_ascii=False)

    def run_audit(self):
        """Повертає (текст-підсумок, помилка). Застосовує лише обмежені зміни дозволених параметрів."""
        if self._audit_busy:
            return "", "аудит уже виконується"
        self._audit_busy = True
        try:
            system = ("Ти — аудитор алгоритмічної торгівлі криптовалютою (стратегія «ловля пастки», 5m). "
                      "Тобі дають параметри, статистику вікон, останні угоди та відхилені фільтрами сигнали. "
                      "Мінімум даних шумний: не роби різких висновків. Дозволені параметри: min_pot_adj "
                      "(множник мін. потенціалу входу, >1 суворіше) і be_trigger_adj (множник порога беззбитку, "
                      "<1 раніше беззбиток). Відповідай ЛИШЕ JSON: "
                      '{"summary":"2-4 речення українською","suggestions":[{"param":"min_pot_adj","target":1.05,'
                      '"reason":"..."},{"idea":"ідея для розробника, не параметр"}]}')
            txt, err = ai_complete(self._audit_prompt(), 900, system)
            if err:
                with self._lock:
                    self._event(f"AI-аудит: помилка — {err}")
                    self._save()
                return "", err
            data = _extract_json(txt)
            summary = str(data.get("summary") or txt)[:700]
            with self._lock:
                now = time.time()
                frozen = now < self.st.get("freeze_until", 0.0)
                prev, applied, ideas = dict(self.st["adj"]), [], []
                pairs = closed_pnl_series()
                for s in (data.get("suggestions") or [])[:4]:
                    if not isinstance(s, dict):
                        continue
                    p, tgt = s.get("param"), s.get("target")
                    if p in GOV_BOUNDS and isinstance(tgt, (int, float)):
                        step = GOV_BOUNDS[p][2] * 2
                        delta = max(-step, min(step, float(tgt) - self.st["adj"][p]))
                        if frozen:
                            ideas.append(f"(заморожено) {p} → {tgt}: {s.get('reason', '')}"[:200])
                        else:
                            d = self._apply_delta(p, delta, str(s.get("reason", ""))[:160], "AI")
                            if d:
                                applied.append(f"{GOV_PARAM_UA[p]} {d:+.3f}")
                    elif s.get("idea"):
                        ideas.append(str(s["idea"])[:200])
                if applied:
                    self.st["pending_check"] = {"n_at": ARCHIVE_COUNT[0], "prev": prev,
                                                "exp_before": window_stats(pairs[-GOV_SHORT_WINDOW:])["exp"]}
                self.st["last_audit_ts"], self.st["n_audit"] = now, ARCHIVE_COUNT[0]
                self.st["last_audit"] = {"t": datetime.now().strftime("%d.%m %H:%M"), "summary": summary,
                                         "applied": applied, "ideas": ideas}
                self._event(f"AI-аудит: {summary[:120]}" + (f" | застосовано: {', '.join(applied)}" if applied else ""))
                self._save()
            return summary, ""
        finally:
            self._audit_busy = False

    # ── дайджест ──
    def build_digest(self):
        cutoff = time.time() - 86400
        recs = []
        for r in read_jsonl_tail(ARCHIVE_FILE, 400):
            try:
                if datetime.fromisoformat(r.get("ts_close", "")).timestamp() >= cutoff:
                    recs.append(r)
            except (ValueError, TypeError):
                pass
        pairs24 = []
        for r in recs:
            t = float_classify(r)
            pnl = (r.get("result") or {}).get("pnl_pct")
            if t and pnl is not None:
                pairs24.append((float(pnl), t, r))
        d24 = window_stats(pairs24)
        allp = closed_pnl_series()
        sh_, lg_ = window_stats(allp[-GOV_SHORT_WINDOW:]), window_stats(allp)
        sh = SHADOW.stats(cutoff)
        tot = sh["total"]
        lines = ["🌅 <b>ASTERIA — дайджест за добу</b>"]
        lines.append(f"Угод: <b>{d24['n']}</b> | плюсів {d24['wr']:.0%} | Σ результат {d24['sum']:+.2f}%")
        lines.append(f"Вікно {sh_['n']}: WR {sh_['wr']:.0%}, {sh_['exp']:+.3f}%/угоду  |  вікно {lg_['n']}: "
                     f"WR {lg_['wr']:.0%}, {lg_['exp']:+.3f}%/угоду")
        lines.append(f"Поплавок: {FLOAT_MODE_UA[ADAPTIVE_FLOAT.mode]} | автопілот: мін. потенціал ×{self.adj('min_pot_adj'):.2f}, "
                     f"беззбиток ×{self.adj('be_trigger_adj'):.2f}")
        halted, why = self.entries_allowed()
        if not halted:
            lines.append(f"🛑 {_esc(why)}")
        day_ev = [e for e in self.st.get("events", []) if e.get("ts", 0) >= cutoff]
        if day_ev:
            lines.append("Події автопілота:")
            for e in day_ev[-6:]:
                lines.append(f"• {e['t']} {_esc(e['text'][:140])}")
        if tot["rejected"]:
            wr = (tot["wins"] / tot["filled"]) if tot["filled"] else 0.0
            verdict = ("фільтри відсікли збитки ✅" if tot["sum"] < 0
                       else "фільтри відсікли прибуток ⚠️" if tot["sum"] > 0 else "нейтрально")
            lines.append(f"Відхилено сигналів: {tot['rejected']} (умовно виконано {tot['filled']}, плюсів {wr:.0%}, "
                         f"Σ {tot['sum']:+.2f}%) → {verdict}")
            effect = d24["sum"] - (d24["sum"] + tot["sum"])
            lines.append(f"Внесок фільтрів і адаптації в результат за добу: {effect:+.2f} п.п.")
        else:
            lines.append("Тіньові сигнали: ще немає вирішених за добу.")
        la = self.st.get("last_audit")
        if la:
            lines.append(f"🤖 AI-аудит ({la['t']}): {_esc(la['summary'][:300])}")
        return "\n".join(lines)

    def send_digest(self):
        send_tg(self.build_digest())
        with self._lock:
            self.st["last_digest_ts"] = time.time()
            self._event("дайджест надіслано в Telegram")
            self._save()

    # ── фоновий сервіс ──
    def start(self):
        threading.Thread(target=self._service_loop, daemon=True).start()

    def _service_loop(self):
        time.sleep(20)
        while True:
            try:
                self.tick()
                now = datetime.now()
                due = now.replace(hour=int(TUNING.get("digest_hour", 9)), minute=0, second=0, microsecond=0)
                if (TUNING.get("digest_enabled", True) and now >= due
                        and self.st.get("last_digest_ts", 0.0) < due.timestamp()):
                    self.send_digest()
                if (TUNING.get("ai_audit_enabled", False) and CLAUDE_API_KEY and not self._audit_busy
                        and ARCHIVE_COUNT[0] - self.st["n_audit"] >= AUDIT_EVERY_TRADES
                        and time.time() - self.st.get("last_audit_ts", 0.0) >= AUDIT_MIN_INTERVAL_H * 3600):
                    threading.Thread(target=self.run_audit, daemon=True).start()
            except Exception as ex:
                print(f"[АВТОПІЛОТ] помилка сервісу: {ex}")
            time.sleep(60)

    # ── текст для вкладки ──
    def status_text(self):
        with self._lock:
            now = time.time()
            pairs = closed_pnl_series()
            s, l = window_stats(pairs[-GOV_SHORT_WINDOW:]), window_stats(pairs)
            out = []
            on = TUNING.get("autopilot_enabled", True)
            out.append(f"АВТОПІЛОТ: {'УВІМКНЕНО' if on else 'вимкнено (множники = 1.00)'}")
            for k, (lo, hi, _) in GOV_BOUNDS.items():
                out.append(f"  {GOV_PARAM_UA[k]:<24} ×{self.st['adj'][k]:.3f}   (межі {lo:.2f}…{hi:.2f})")
            ok, why = self.entries_allowed()
            out.append("  входи: " + ("дозволені" if ok else f"ЗАБЛОКОВАНО — {why}"))
            if now < self.st.get("freeze_until", 0.0):
                out.append(f"  зміни заморожено ще {(self.st['freeze_until'] - now) / 3600:.1f} год (після автовідкату)")
            if self.st.get("pending_check"):
                pc = self.st["pending_check"]
                out.append(f"  зміна на перевірці: ще {max(0, GOV_ROLLBACK_AFTER - (ARCHIVE_COUNT[0] - pc['n_at']))} угод до підтвердження/відкату")
            out.append("")
            out.append(f"Коротке вікно ({s['n']}/{GOV_SHORT_WINDOW}): WR {s['wr']:.0%}, {s['exp']:+.3f}% на угоду")
            out.append(f"Довге вікно  ({l['n']}/{GOV_LONG_WINDOW}): WR {l['wr']:.0%}, {l['exp']:+.3f}% на угоду")
            out.append(f"У пам'яті {len(TRADE_SNAPSHOTS.closed)}/{TRADE_SNAPSHOTS.MAX_CLOSED} угод; "
                       f"у вічному архіві {ARCHIVE_COUNT[0]}")
            out.append(f"До наступної оцінки: {max(0, GOV_BATCH - (ARCHIVE_COUNT[0] - self.st['n_eval']))} угод")
            sh = SHADOW.stats(now - 7 * 86400)
            out.append("")
            out.append(f"ТІНЬОВІ СИГНАЛИ (7 діб): очікують {sh['pending']}, вирішено {sh['total']['rejected']}")
            for reason, b in sh["by"].items():
                wr = (b["wins"] / b["filled"]) if b["filled"] else 0.0
                out.append(f"  {REASON_UA.get(reason, reason):<20} відхилено {b['rejected']:<4} виконано б {b['filled']:<4} "
                           f"плюсів {wr:.0%}  Σ {b['sum']:+.2f}%")
            la = self.st.get("last_audit")
            if la:
                out.append("")
                out.append(f"ОСТАННІЙ AI-АУДИТ ({la['t']}): {la['summary']}")
                if la.get("applied"):
                    out.append("  застосовано: " + "; ".join(la["applied"]))
                for i in la.get("ideas", [])[:3]:
                    out.append(f"  ідея: {i}")
            out.append("")
            out.append("ПОДІЇ:")
            for e in (self.st.get("events") or [])[-14:][::-1]:
                out.append(f"  {e['t']}  {e['text'][:170]}")
            return "\n".join(out)


GOVERNOR = AutopilotGovernor()
GOVERNOR.start()


# ════════════════════════════════════════════════════════
#  ТОРГОВЕ ЯДРО: сканер → відбір → вхід (пастка / ринок) → супровід → результат
#  PAPER: віртуальні угоди за цінами Binance. LIVE: реальні ордери BingX.
# ════════════════════════════════════════════════════════
try:
    bingx_trader = BingXTrader()
except Exception as _e:                       # ccxt може бути відсутній у збірці
    class _NoTrader:
        _ready = False
        _ex = None
    bingx_trader = _NoTrader()
    print(f"[БІРЖА] BingX недоступний у цій збірці: {_e}")


def reload_trader():
    """Перечитує ключі BingX після зміни в Налаштуваннях."""
    global bingx_trader
    try:
        bingx_trader = BingXTrader()
    except Exception as e:
        print(f"[БІРЖА] не вдалось ініціалізувати BingX: {e}")
    return bool(getattr(bingx_trader, "_ready", False))


def _is_round(price, pct=0.005):
    for dec in (1, 0):
        if abs(price - round(price, dec)) / max(price, 1e-9) < pct:
            return True
    return False


def shield_check(adx, price):
    """Щит безпеки без стану: «розпил» (ADX<20) і круглий рівень у сильному тренді."""
    if adx < 20:
        return False, f"РОЗПИЛ (ADX:{adx:.1f})"
    if adx >= 25 and _is_round(price):
        return False, "КРУГЛИЙ РІВЕНЬ (ММ)"
    return True, "АКТИВНИЙ ТРЕНД"


def compute_signal(sym, tf=None, rows=None, rows_l=None):
    """Повний розрахунок сигналу монети. None — недостатньо даних."""
    tf = tf or SCAN_TIMEFRAME
    rows = rows or DATA.klines(sym, tf, 60)
    if not rows or len(rows) < WINDOW_SIZE:
        return None
    price, trough, dist, loft, curv, tm, sm = calc_gravity(rows, tf)
    if loft <= 0:
        return None
    tdir = "LONG" if curv > 0 else "SHORT"
    m = 1 if tdir == "LONG" else -1
    take = price + m * tm * loft
    stop = price - m * sm * loft
    trend = calc_trend_metrics(rows)
    vol = calc_volatility_metrics(rows)
    stop_orig, ext = stop, 1.0
    if TUNING.get("slope_ext_enabled", True):
        atr_abs = (vol.get("atr14_pct") or 0.0) * price / 100.0
        stop, ext = apply_slope_stop_extension(tdir, price, stop, trend.get("slope_pct_per_candle"), atr_abs)
    ltf = lower_tf(tf)
    if rows_l is None:
        rows_l = DATA.klines(sym, ltf, 60)
    entry, trap, mtf = price, False, {}
    if rows_l and len(rows_l) >= WINDOW_SIZE:
        p_l, _a, _b, loft_l, cv_l, tm_l, _c = calc_gravity(rows_l, ltf)
        e, is_trap = calc_pullback_entry(tdir, price, p_l, loft_l, cv_l, tm_l)
        mtf = {"ltf": ltf, "pullback_entry": bool(is_trap), "loft_l": loft_l, "curv_l": cv_l, "price_l": p_l}
        if is_trap:                                   # стоп тримає ту саму відстань від входу пастки
            new_stop = e - m * abs(price - stop)
            if m * (take - e) > 0 and m * (e - new_stop) > 0:
                entry, trap, stop = e, True, new_stop
    risk = abs(entry - stop)
    pot = abs(take - entry) / max(entry, 1e-9) * 100
    rr = abs(take - entry) / risk if risk > 0 else 0.0
    adx = calc_adx(rows)
    if adx >= 25 and rr >= 2.0:
        conf = 3
    elif adx >= 20 and rr >= 1.0:
        conf = 2
    else:
        conf = 1
    return {"sym": sym, "tf": tf, "dir": tdir, "price": price, "entry": entry, "trap": trap, "stop": stop,
            "take": take, "pot": pot, "rr": rr, "adx": adx, "conf": conf, "ext": ext, "stop_orig": stop_orig,
            "slope": trend.get("slope_pct_per_candle"), "trend": trend, "vol": vol, "mtf": mtf,
            "geom": {"trough": trough, "dist_rmax": dist, "loft": loft, "curvature": curv,
                     "take_mult": tm, "stop_mult": sm}}


def build_snapshot(source, sig, kind, entry, stop, take, amount_info=None):
    now = datetime.now()
    risk = abs(entry - stop)
    return {
        "source": source, "kind": kind, "symbol": sig["sym"], "tf": sig["tf"], "direction": sig["dir"],
        "ts_open": now.isoformat(timespec="seconds"),
        "levels": {"signal_price": sig["price"], "entry_planned": entry, "stop": stop, "take": take},
        "scores": {"potential_pct": abs(take - entry) / entry * 100 if entry else None,
                   "risk_pct": risk / entry * 100 if entry else None,
                   "rr": abs(take - entry) / risk if risk > 0 else None},
        "adaptive": {"float_mode": FLOAT_MODE_UA[ADAPTIVE_FLOAT.mode], "stop_extended": sig["ext"] > 1.0,
                     "slope_pct": sig["slope"], "orig_stop": sig["stop_orig"], "ext_factor": sig["ext"],
                     "min_potential_eff": ADAPTIVE_FLOAT.min_potential(), "amount_cap": ADAPTIVE_FLOAT.amount_cap(),
                     "min_pot_adj": GOVERNOR.adj("min_pot_adj"), "be_trigger_adj": GOVERNOR.adj("be_trigger_adj")},
        "geometry": sig["geom"], "mtf": sig["mtf"],
        "trend": dict({"adx": sig["adx"]}, **(sig["trend"] or {})),
        "volatility": sig["vol"],
        "time": {"hour": now.hour, "weekday": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"][now.weekday()]},
        "real": amount_info or {},
        "excursions": {}, "events": {}, "result": {},
    }


class TradeEngine:
    """Бот: сканер у фоні + торговий цикл. Один відкритий трейд за раз."""

    def __init__(self):
        self.running = False
        self.rows = {}                    # sym -> рядок сканера
        self.scan_progress = ""
        self.status = "Зупинено"
        self.active = None                # поточна угода (dict) або None
        self.pending = None               # пастка, що чекає
        self.close_requested = False
        self.cancel_requested = False
        self.balance = None
        self.stats = load_json_safe(STATE_FILE, {}).get("stats", {"trades": 0, "win": 0, "loss": 0, "pnl": 0.0})
        self._threads = []
        self._last_hb = time.time()
        self._resumed = False

    # ── керування ──
    @property
    def live(self):
        return bool(TUNING.get("live_mode")) and bool(getattr(bingx_trader, "_ready", False))

    def mode_text(self):
        if TUNING.get("live_mode") and not getattr(bingx_trader, "_ready", False):
            return "LIVE (ключі не задано!)"
        return "LIVE" if self.live else "PAPER"

    def _save_state(self):
        save_json_safe(STATE_FILE, {"was_running": self.running, "stats": self.stats})

    def start(self):
        if self.running:
            return
        if TUNING.get("live_mode") and not getattr(bingx_trader, "_ready", False):
            print("[БОТ] LIVE без ключів BingX/ccxt — запуск неможливий. Додай ключі або вимкни LIVE.")
            self.status = "LIVE без ключів — запуск скасовано"
            return
        self.running = True
        self.cancel_requested = False
        self._save_state()
        self.status = "Запуск..."
        self._threads = [threading.Thread(target=self._guard, args=(self._scanner_loop, "сканер"), daemon=True),
                         threading.Thread(target=self._guard, args=(self._trade_loop, "торгівля"), daemon=True)]
        for t in self._threads:
            t.start()
        print(f"[БОТ] запущено, режим {self.mode_text()}")
        send_tg(f"▶️ <b>ASTERIA MOBILE</b> запущено, режим <b>{self.mode_text()}</b>")

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.cancel_requested = True
        self._save_state()
        self.status = "Зупинено"
        print("[БОТ] зупинено" + (" (відкрита угода лишається на біржі з TP/SL)" if self.active and self.live else ""))
        send_tg("⏹ <b>ASTERIA MOBILE</b> зупинено")

    def request_close(self):
        self.close_requested = True
        if self.pending:
            self.pending["cancel_req"] = True

    def _guard(self, fn, name):
        """Перезапускає цикл, якщо він упав: бот має жити сам."""
        while self.running:
            try:
                fn()
            except Exception as e:
                print(f"[БОТ] збій потоку «{name}»: {e}")
                traceback.print_exc()
                time.sleep(10)

    # ── сканер ──
    def _scanner_loop(self):
        while self.running:
            for i, sym in enumerate(ELITE_WHITELIST):
                if not self.running:
                    return
                self.scan_progress = f"{i + 1}/{len(ELITE_WHITELIST)} {sym}"
                try:
                    sig = compute_signal(sym)
                    if sig:
                        self.rows[sym] = {"sym": sym, "dir": sig["dir"], "price": sig["price"], "pot": sig["pot"],
                                          "rr": sig["rr"], "adx": sig["adx"], "conf": sig["conf"], "trap": sig["trap"],
                                          "ts": time.time()}
                except Exception as e:
                    print(f"[СКАНЕР] {sym}: {e}")
                time.sleep(0.25)
            time.sleep(2)

    def ranked(self):
        now = time.time()
        return sorted((r for r in self.rows.values() if now - r["ts"] < 600), key=lambda r: r["pot"], reverse=True)

    # ── відбір ──
    def _gate(self, sig):
        """(ok, reason, shadow_code)."""
        if sig["pot"] < ADAPTIVE_FLOAT.min_potential():
            return False, f"потенціал {sig['pot']:.2f}% < {ADAPTIVE_FLOAT.min_potential():.2f}%", "LOW_POT"
        ok, why = shield_check(sig["adx"], sig["price"])
        if not ok:
            return False, why, "SHIELD"
        if TUNING.get("rr_filter_enabled", False) and sig["rr"] < float(TUNING.get("rr_min", 2.5)):
            return False, f"R:R 1:{sig['rr']:.2f} < {format_rr(TUNING.get('rr_min', 2.5))}", "RR_FILTER"
        return True, "", ""

    def _pick(self):
        fails = 0
        for row in self.ranked():
            sig = compute_signal(row["sym"])          # свіжий розрахунок
            if not sig:
                continue
            ok, why, code = self._gate(sig)
            if ok:
                return sig
            SHADOW.log_signal(sig["sym"], sig["tf"], sig["dir"], sig["entry"], sig["stop"], sig["take"], code,
                              {"adx": sig["adx"], "pot": sig["pot"], "rr": sig["rr"]})
            fails += 1
            if fails >= 3:
                break
        return None

    # ── торговий цикл ──
    def _trade_loop(self):
        if not self._resumed:
            self._resumed = True
            self._resume_if_any()
        while self.running:
            self._housekeeping()
            ok, why = GOVERNOR.entries_allowed()
            if not ok:
                self.status = f"{why} — нові входи на паузі"
                time.sleep(10)
                continue
            if not self.rows:
                self.status = "Сканер збирає дані..."
                time.sleep(3)
                continue
            self.status = "Чекаю підходящий сигнал..."
            sig = self._pick()
            if not sig:
                time.sleep(8)
                continue
            self._open_and_manage(sig)
            time.sleep(3)

    def _housekeeping(self):
        hb = float(TUNING.get("heartbeat_hours", 6)) * 3600
        if hb > 0 and time.time() - self._last_hb >= hb:
            self._last_hb = time.time()
            s = self.stats
            send_tg(f"💓 <b>ASTERIA MOBILE</b> працює | {self.mode_text()} | угод {s['trades']}, Σ {s['pnl']:+.2f}% | "
                    f"поплавок: {FLOAT_MODE_UA[ADAPTIVE_FLOAT.mode]}")
        if self.live and (self.balance is None or time.time() - self.balance.get("t", 0) > 120):
            b = bingx_trader.get_balance()
            if b:
                b["t"] = time.time()
                self.balance = b

    # ── відкриття ──
    def _open_and_manage(self, sig):
        live = self.live
        amount = float(TUNING.get("amount", 10.0))
        leverage = int(TUNING.get("leverage", 1))
        use_sl = bool(TUNING.get("use_sl", True))
        sym, direction = sig["sym"], sig["dir"]
        entry_ref, stop, take = sig["entry"], sig["stop"], sig["take"]
        risk_d = abs(entry_ref - stop)
        rr_now = abs(take - entry_ref) / risk_d if risk_d > 0 else 0.0
        stop_pct = risk_d / entry_ref * 100 if entry_ref else 0.0
        mult = calc_amount_mult(rr_now, stop_pct, cap=ADAPTIVE_FLOAT.amount_cap())
        ext = sig["ext"]
        desired = amount * mult / (ext if (SLOPE_KEEP_RISK and ext > 1.0) else 1.0)
        self.status = f"Відкриваю {direction} {sym}..."
        if sig["ext"] > 1.0:
            print(f"[СТОП-НАХИЛ] {sym} {direction}: нахил {sig['slope']:+.3f}%/св > {SLOPE_THRESHOLD} → стоп ×{ext:.2f}")

        if live:
            if sig["trap"]:
                ok, oid, entry, qty, extra = self._enter_trap_live(sym, direction, desired, leverage, entry_ref,
                                                                   take, stop, use_sl, amount)
            else:
                ok, oid, entry, qty, extra = bingx_trader.open_order(sym, direction, desired, leverage, take,
                                                                     stop if use_sl else None, use_sl,
                                                                     fallback_amount=amount)
        else:
            ok, oid, entry, qty, extra = self._enter_paper(sig, desired, leverage, entry_ref, take, stop)
        if not ok:
            self.status = f"Вхід не відбувся: {str(oid)[:60]}"
            print(f"[БОТ] {sym}: {oid}")
            time.sleep(5)
            return

        td = {"sym": sym, "dir": direction, "entry": entry, "tp": take, "sl": stop, "qty": qty,
              "amount": extra.get("amount_used", amount), "leverage": leverage, "mult": mult, "use_sl": use_sl,
              "sl_orig": stop, "tp_id": extra.get("tp_id"), "sl_id": extra.get("sl_id"),
              "be_done": False, "be_next_try": 0, "tp1": extra.get("tp1"), "qty1": extra.get("qty1", 0.0),
              "qty2": extra.get("qty2", qty), "tp1_done": False, "max_prg": 0.0, "min_prg": 0.0,
              "open_time": datetime.now().strftime("%d.%m %H:%M"), "live": live, "opened_ts": time.time(),
              "last_check_ms": int(time.time() * 1000)}
        kind = ("РЕАЛЬНА ПАСТКА" if sig["trap"] else "РЕАЛЬНА УГОДА") if live else ("ЛОВЛЯ ПАСТКИ" if sig["trap"] else "НОВИЙ ВХІД")
        try:
            rec = build_snapshot("real" if live else "radar", sig, kind, entry, stop, take,
                                 {"amount_usdt": td["amount"], "leverage": leverage, "amount_mult": mult, "qty": qty,
                                  "use_sl": use_sl, "tp1": td["tp1"], "qty1": td["qty1"], "qty2": td["qty2"]})
            td["snap_id"] = TRADE_SNAPSHOTS.begin(rec)
        except Exception as e:
            print(f"[ЗНІМОК] {e}")
        self.active = td
        self.stats["trades"] += 1
        self._persist_active()
        send_tg(f"{'🟢' if live else '🧪'} <b>[{'LIVE' if live else 'PAPER'} {sym}]</b> {direction}\n"
                f"Вхід <code>{entry:.6g}</code> | Тейк <code>{take:.6g}</code> | Стоп <code>{stop:.6g}</code>\n"
                f"Сума {td['amount']:.1f}$ ×{mult}, плече {leverage}")
        self.status = f"Угода {direction} {sym}"
        self._manage(td)

    def _persist_active(self):
        if self.active:
            save_json_safe(ACTIVE_FILE, self.active)
        else:
            try:
                os.remove(ACTIVE_FILE)
            except OSError:
                pass

    # ── PAPER: вхід ──
    def _enter_paper(self, sig, desired, leverage, entry_ref, take, stop):
        sym, direction = sig["sym"], sig["dir"]
        m = 1 if direction == "LONG" else -1
        px = DATA.price(sym) or sig["price"]
        fill = None
        if sig["trap"]:
            self.pending = {"sym": sym, "dir": direction, "entry": entry_ref, "cancel_req": False}
            t0, last_ms, best = time.time(), int(time.time() * 1000), None
            outcome = None
            while self.running:
                if self.pending.get("cancel_req"):
                    outcome = "cancelled"; break
                k = DATA.klines(sym, "1m", 5) or []
                for c in k:
                    if c[0] + 60000 <= last_ms:
                        continue
                    hit = (c[3] <= entry_ref) if direction == "LONG" else (c[2] >= entry_ref)
                    if hit:
                        outcome = "filled"; fill = entry_ref; break
                    ex = c[3] if direction == "LONG" else c[2]
                    best = ex if best is None else (min(best, ex) if direction == "LONG" else max(best, ex))
                if outcome:
                    break
                last_ms = int(time.time() * 1000) - 60000
                now = DATA.price(sym)
                left = max(0, int((TRAP_TIMEOUT_SEC - (time.time() - t0)) / 60))
                self.status = f"ПАСТКА {sym} {direction}: ліміт {entry_ref:.6g}, зараз {now or 0:.6g}, ще {left} хв"
                if time.time() - t0 > TRAP_TIMEOUT_SEC:
                    outcome = "timeout"; break
                time.sleep(5)
            self.pending = None
            if outcome != "filled":
                if outcome == "timeout" and best is not None:
                    now = DATA.price(sym)
                    gap = abs(best - entry_ref) / entry_ref * 100
                    levels_ok = now is not None and ((stop < now < take) if direction == "LONG" else (take < now < stop))
                    if gap <= NEAR_MISS_TOLERANCE_PCT and levels_ok and abs(now - entry_ref) / entry_ref * 100 <= NEAR_MISS_TOLERANCE_PCT:
                        fill = now
                        print(f"[ДИНАМІЧНИЙ ВХІД] {sym}: ціна не дійшла {gap:.2f}% — входжу за ринком")
                if fill is None:
                    return False, {"timeout": "пастка не спрацювала за годину", "cancelled": "пастку скасовано"}.get(outcome, "зупинено"), 0.0, 0.0, {}
        if fill is None:
            fill = px
        amount_used = desired
        qty = amount_used * leverage / fill
        qty1 = qty * TP1_CLOSE_SHARE
        qty2 = qty - qty1
        tp1 = calc_tp1_price(direction, fill, take)
        two = qty1 > 0 and qty2 > 0 and abs(tp1 - take) > 1e-12
        extra = {"amount_used": amount_used, "tp1": tp1 if two else None, "qty1": qty1 if two else 0.0,
                 "qty2": qty2 if two else qty}
        return True, "paper", fill, qty, extra

    # ── LIVE: пастка (порт з десктопної версії) ──
    def _enter_trap_live(self, sym, direction, desired, leverage, entry_plan, take, stop, use_sl, amount):
        ok, oid, qty, ex0 = bingx_trader.place_limit_entry(sym, direction, desired, leverage, entry_plan,
                                                           fallback_amount=amount)
        if not ok:
            return False, oid, 0.0, 0.0, {}
        self.pending = {"sym": sym, "oid": oid, "entry": entry_plan, "dir": direction, "cancel_req": False}
        save_json_safe(ACTIVE_FILE, {"pending": self.pending, "live": True})
        send_tg(f"⚡ <b>[ПАСТКА LIVE:{sym}]</b> {direction}\nЛіміт <code>{entry_plan:.6g}</code> | "
                f"Тейк <code>{take:.6g}</code> | Стоп <code>{stop:.6g}</code>")
        t0 = time.time(); best = None; outcome = None; fill_px = None; fill_qty = 0.0
        while True:
            if not self.running:
                outcome = "stopped"; break
            if self.pending.get("cancel_req"):
                outcome = "cancelled"; break
            info = bingx_trader.check_entry(sym, oid)
            if info["status"] == "filled":
                outcome, fill_px, fill_qty = "filled", (info["avg"] or entry_plan), info["filled"] or qty; break
            if info["status"] == "canceled":
                outcome, fill_qty, fill_px = "canceled_ext", info["filled"], info["avg"]; break
            now = bingx_trader.last_price(sym)
            if now:
                best = now if best is None else (min(best, now) if direction == "LONG" else max(best, now))
                left = max(0, int((TRAP_TIMEOUT_SEC - (time.time() - t0)) / 60))
                self.status = f"ПАСТКА {sym} {direction}: ліміт {entry_plan:.6g}, зараз {now:.6g}, ще {left} хв"
            if time.time() - t0 > TRAP_TIMEOUT_SEC:
                outcome = "timeout"; break
            time.sleep(3)
        if outcome != "filled":
            bingx_trader.cancel_entry(sym, oid)
            info = bingx_trader.check_entry(sym, oid)
            if info["filled"] > 0:
                outcome, fill_px, fill_qty = "filled", (info["avg"] or entry_plan), info["filled"]
        self.pending = None
        if outcome == "filled":
            exits = bingx_trader.attach_exits(sym, direction, fill_qty, fill_px, take, stop if use_sl else None, use_sl)
            exits["amount_used"] = ex0.get("amount_used", amount); exits["trap"] = True
            send_tg(f"✅ <b>[ПАСТКА ЗАПОВНЕНА:{sym}]</b> вхід <code>{fill_px:.6g}</code>, кількість {fill_qty}")
            return True, oid, fill_px, fill_qty, exits
        if outcome == "timeout" and best is not None:
            gap = abs(best - entry_plan) / entry_plan * 100
            now = bingx_trader.last_price(sym)
            levels_ok = now is not None and ((stop < now < take) if direction == "LONG" else (take < now < stop))
            if gap <= NEAR_MISS_TOLERANCE_PCT and levels_ok and abs(now - entry_plan) / entry_plan * 100 <= NEAR_MISS_TOLERANCE_PCT:
                send_tg(f"🎯 <b>[ДИНАМІЧНИЙ ВХІД:{sym}]</b> ціна не дійшла {gap:.2f}% — вхід за ринком")
                ok2, oid2, px2, q2, ex2 = bingx_trader.open_order(sym, direction, desired, leverage, take,
                                                                  stop if use_sl else None, use_sl, fallback_amount=amount)
                if ok2:
                    ex2["trap"] = "near_miss"
                return ok2, oid2, px2, q2, ex2
        reason = {"timeout": "пастка не спрацювала за годину", "cancelled": "пастку скасовано вручну",
                  "stopped": "бота зупинено", "canceled_ext": "ліміт скасовано на біржі"}.get(outcome, str(outcome))
        try:
            os.remove(ACTIVE_FILE)
        except OSError:
            pass
        send_tg(f"⌛ <b>[ПАСТКА СКАСОВАНА:{sym}]</b> {reason}")
        return False, reason, 0.0, 0.0, {}

    # ── супровід ──
    def _manage(self, td):
        self.close_requested = False
        try:
            if td.get("live"):
                self._manage_live(td)
            else:
                self._manage_paper(td)
        finally:
            self.active = None
            self._persist_active()
            self._save_state()

    def _finish(self, td, hit_tp, hit_sl, manual, price, pnl):
        m = 1 if td["dir"] == "LONG" else -1
        if td.get("tp1_done"):
            q1, q2 = td.get("qty1", 0.0), td.get("qty2", 0.0)
            sh = q1 / (q1 + q2) if (q1 + q2) > 0 else TP1_CLOSE_SHARE
            pnl = sh * (m * (td["tp1"] - td["entry"]) / td["entry"] * 100) + (1 - sh) * pnl
        if manual:
            msg = f"✋ ЗАКРИТО ВРУЧНУ {td['sym']} {pnl:+.2f}%"
            self.stats["win" if pnl > 0 else "loss"] += 1
        elif hit_tp:
            self.stats["win"] += 1
            msg = (f"✅ ТЕЙК 1+ТЕЙК 2 {td['sym']} {pnl:+.2f}%" if td.get("tp1_done") else f"✅ ТЕЙК! {td['sym']} {pnl:+.2f}%")
        elif td.get("tp1_done"):
            tail = "беззбиток" if td.get("be_done") else "стоп"
            if pnl > 0:
                self.stats["win"] += 1
                msg = f"💰 ТЕЙК 1 + {tail} решти {td['sym']} {pnl:+.2f}%"
            else:
                self.stats["loss"] += 1
                msg = f"💥 ТЕЙК 1 + {tail} решти (сумарно в мінус) {td['sym']} {pnl:+.2f}%"
        elif td.get("be_done"):
            msg = f"🟰 БЕЗЗБИТОК {td['sym']} {pnl:+.2f}%"
        else:
            self.stats["loss"] += 1
            msg = f"🛑 СТОП. {td['sym']} {pnl:+.2f}%"
        self.stats["pnl"] += pnl
        tag = "LIVE" if td.get("live") else "PAPER"
        send_tg(f"{msg} [{tag}]\nМакс. прогрес до тейку: {td.get('max_prg', 0.0):.0f}%")
        print(f"[УГОДА] {msg}")
        try:
            TRADE_SNAPSHOTS.finish(
                td.get("snap_id"),
                result={"outcome": msg, "exit_price": price, "pnl_pct": pnl, "be_done": bool(td.get("be_done")),
                        "tp1_done": bool(td.get("tp1_done")), "trail_enabled": bool(TUNING.get("trail_enabled", True))},
                excursions={"max_prg": td.get("max_prg"), "min_prg": td.get("min_prg")},
                levels={"entry_final": td.get("entry"), "tp1_price": td.get("tp1"), "stop_final": td.get("sl")})
        except Exception as e:
            print(f"[ЗНІМОК] закриття: {e}")
        self.status = msg

    def _be_or_tp1_updates(self, td, price, hit_tp, hit_sl, now_t, live):
        """Спільна логіка TP1 і беззбитку. Для LIVE змінює стоп на біржі."""
        prg_cur = calc_progress_pct(td["dir"], td["entry"], td["tp"], price)
        trail = TUNING.get("trail_enabled", True)
        # TP1
        if td.get("tp1") and not td.get("tp1_done") and prg_cur >= TP1_PATH_PCT and not (hit_tp or hit_sl) \
                and now_t >= td.get("tp1_next_try", 0):
            try:
                done, sz = True, td["qty2"]
                if live:
                    sz = bingx_trader.position_size(td["sym"])
                    done = sz is not None and 0 < sz <= td["qty2"] * 1.05
                if done:
                    td["tp1_done"] = True
                    td["qty"] = sz
                    new_sl = calc_be_stop(td["dir"], td["entry"], price) if trail else td.get("sl_orig", td["sl"])
                    if new_sl is not None:
                        if live and td.get("use_sl"):
                            ok_mv, new_id, new_adj = bingx_trader.move_stop(td["sym"], td["dir"], sz, new_sl, td.get("sl_id"))
                        else:
                            ok_mv, new_id, new_adj = True, None, new_sl
                        if ok_mv:
                            td["sl"], td["sl_id"], td["be_done"] = new_adj, new_id, bool(trail)
                    send_tg(f"🎯 <b>[ТЕЙК 1:{td['sym']}]</b> закрито {int(TP1_CLOSE_SHARE * 100)}% на "
                            f"<code>{td['tp1']:.5g}</code>, стоп решти <code>{td['sl']:.5g}</code>")
                    self._persist_active()
                else:
                    td["tp1_next_try"] = now_t + 5
            except Exception as e:
                print(f"[TP1] {e}")
                td["tp1_next_try"] = now_t + 30
        # беззбиток
        if trail and not td.get("be_done") and not (hit_tp or hit_sl) and now_t >= td.get("be_next_try", 0):
            try:
                if prg_cur >= ADAPTIVE_FLOAT.be_trigger():
                    new_sl = calc_be_stop(td["dir"], td["entry"], price)
                    if new_sl is not None:
                        if live and td.get("use_sl"):
                            ok_mv, new_id, new_adj = bingx_trader.move_stop(td["sym"], td["dir"], td["qty"], new_sl, td.get("sl_id"))
                        else:
                            ok_mv, new_id, new_adj = True, None, new_sl
                        if ok_mv:
                            td["sl"], td["sl_id"], td["be_done"] = new_adj, new_id, True
                            self.status = f"Стоп {td['sym']} → беззбиток {new_adj:.5g}"
                            self._persist_active()
                        else:
                            td["be_next_try"] = now_t + 30
            except Exception as e:
                print(f"[БЕЗЗБИТОК] {e}")
                td["be_next_try"] = now_t + 30

    def _manage_live(self, td):
        while True:
            try:
                price = bingx_trader.last_price(td["sym"])
                if price is None:
                    time.sleep(3); continue
                m = 1 if td["dir"] == "LONG" else -1
                pnl = m * (price - td["entry"]) / td["entry"] * 100
                hit_tp = (price >= td["tp"]) if td["dir"] == "LONG" else (price <= td["tp"])
                hit_sl = td.get("sl") and ((price <= td["sl"]) if td["dir"] == "LONG" else (price >= td["sl"]))
                prg = calc_progress_pct(td["dir"], td["entry"], td["tp"], price)
                td["max_prg"], td["min_prg"] = max(td["max_prg"], prg), min(td["min_prg"], prg)
                TRADE_SNAPSHOTS.touch(td.get("snap_id"), td["max_prg"], td["min_prg"])
                self.status = f"{td['dir']} {td['sym']}: {pnl:+.2f}% | прогрес {prg:.0f}%"
                manual = self.close_requested
                if not manual:
                    self._be_or_tp1_updates(td, price, hit_tp, hit_sl, time.time(), True)
                if hit_tp or hit_sl or manual:
                    if not bingx_trader.close_order(td["sym"], td["dir"], td["qty"]):
                        warn = f"ТЕЙК/СТОП ДОСЯГНУТО, але закриття не пройшло: {td['sym']} — перевір на біржі!"
                        print("[!] " + warn)
                        send_tg("⚠️ " + warn)
                        time.sleep(3)
                        continue
                    self._finish(td, hit_tp, hit_sl, manual, price, pnl)
                    return
                if not self.running:
                    return          # бота зупинено: позиція лишається з TP/SL на біржі
            except Exception as e:
                print(f"[СУПРОВІД] {e}")
            time.sleep(3)

    def _manage_paper(self, td):
        m = 1 if td["dir"] == "LONG" else -1
        while True:
            try:
                k = DATA.klines(td["sym"], "1m", 5) or []
                price = DATA.price(td["sym"]) or (k[-1][4] if k else td["entry"])
                lo = min([c[3] for c in k if c[0] + 60000 > td["last_check_ms"]] + [price])
                hi = max([c[2] for c in k if c[0] + 60000 > td["last_check_ms"]] + [price])
                td["last_check_ms"] = int(time.time() * 1000) - 60000
                fav, adv = (hi, lo) if m == 1 else (lo, hi)
                prg_f = calc_progress_pct(td["dir"], td["entry"], td["tp"], fav)
                prg_a = calc_progress_pct(td["dir"], td["entry"], td["tp"], adv)
                td["max_prg"], td["min_prg"] = max(td["max_prg"], prg_f), min(td["min_prg"], prg_a)
                TRADE_SNAPSHOTS.touch(td.get("snap_id"), td["max_prg"], td["min_prg"])
                hit_sl = bool(td.get("sl")) and ((lo <= td["sl"]) if m == 1 else (hi >= td["sl"]))
                hit_tp = (hi >= td["tp"]) if m == 1 else (lo <= td["tp"])
                if hit_sl and hit_tp:
                    hit_tp = False                         # у межах одного опитування — консервативно стоп
                pnl_now = m * (price - td["entry"]) / td["entry"] * 100
                self.status = f"[PAPER] {td['dir']} {td['sym']}: {pnl_now:+.2f}% | прогрес {calc_progress_pct(td['dir'], td['entry'], td['tp'], price):.0f}%"
                manual = self.close_requested
                if not (hit_tp or hit_sl or manual):
                    self._be_or_tp1_updates(td, fav, False, False, time.time(), False)
                if hit_tp or hit_sl or manual:
                    exit_px = td["tp"] if hit_tp else (td["sl"] if hit_sl else price)
                    pnl = m * (exit_px - td["entry"]) / td["entry"] * 100
                    self._finish(td, hit_tp, hit_sl, manual, exit_px, pnl)
                    return
                if not self.running:
                    return
            except Exception as e:
                print(f"[PAPER] {e}")
            time.sleep(5)

    # ── відновлення після перезапуску ──
    def _resume_if_any(self):
        d = load_json_safe(ACTIVE_FILE, None)
        if not d:
            return
        if d.get("pending"):                         # лімітний вхід пастки: безпечно скасовуємо
            p = d["pending"]
            try:
                if d.get("live") and getattr(bingx_trader, "_ready", False):
                    bingx_trader.cancel_entry(p["sym"], p["oid"])
            except Exception:
                pass
            print(f"[ВІДНОВЛЕННЯ] пастку {p.get('sym')} скасовано після перезапуску")
            send_tg(f"♻️ Після перезапуску скасовано лімітну пастку {p.get('sym')}. Перевір ордери на біржі.")
            try:
                os.remove(ACTIVE_FILE)
            except OSError:
                pass
            return
        if d.get("sym"):
            if d.get("live") and not getattr(bingx_trader, "_ready", False):
                print("[ВІДНОВЛЕННЯ] є відкрита LIVE-угода, але ключів немає — перевір вручну на біржі")
                return
            print(f"[ВІДНОВЛЕННЯ] продовжую супровід {d['sym']}")
            send_tg(f"♻️ Відновлено супровід угоди {d['sym']} після перезапуску")
            d.setdefault("last_check_ms", int(time.time() * 1000))
            self.active = d
            self.status = f"Відновлено {d['sym']}"
            self._manage(d)


ENGINE = TradeEngine()


# ════════════════════════════════════════════════════════
#  ANDROID: екран не гасне, wake lock, виняток з оптимізації батареї
# ════════════════════════════════════════════════════════
def android_keep_awake():
    if not IS_ANDROID:
        return
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        Context = autoclass("android.content.Context")
        pm = activity.getSystemService(Context.POWER_SERVICE)
        PowerManager = autoclass("android.os.PowerManager")
        wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "asteria:bot")
        wl.acquire()
        LayoutParams = autoclass("android.view.WindowManager$LayoutParams")
        activity.runOnUiThread(lambda: activity.getWindow().addFlags(LayoutParams.FLAG_KEEP_SCREEN_ON))
        print("[ANDROID] wake lock і «екран не гасне» увімкнено")
    except Exception as e:
        print(f"[ANDROID] wake lock: {e}")


def android_battery_exemption():
    """Просить систему не обмежувати застосунок економією батареї (один раз)."""
    if not IS_ANDROID:
        return
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        activity = PythonActivity.mActivity
        intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
        intent.setData(Uri.parse("package:" + activity.getPackageName()))
        activity.startActivity(intent)
    except Exception as e:
        print(f"[ANDROID] виняток з оптимізації батареї: {e}")



# ════════════════════════════════════════════════════════
#  ІНТЕРФЕЙС (Kivy): чотири екрани з нижньою навігацією + екран-заставка
# ════════════════════════════════════════════════════════
def _run_ui():
    from kivy.app import App
    from kivy.clock import Clock
    from kivy.core.window import Window
    from kivy.metrics import dp, sp
    from kivy.graphics import Color, RoundedRectangle, Rectangle
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.label import Label
    from kivy.uix.popup import Popup
    from kivy.uix.screenmanager import ScreenManager, Screen, NoTransition
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.spinner import Spinner
    from kivy.uix.switch import Switch
    from kivy.uix.textinput import TextInput

    BG, CARD = (0.05, 0.05, 0.07, 1), (0.09, 0.09, 0.12, 1)
    GREEN, RED, GOLD, CYAN = (0.0, 0.90, 0.55, 1), (1.0, 0.30, 0.30, 1), (1.0, 0.80, 0.2, 1), (0.2, 0.85, 1.0, 1)
    DIM, WHITE = (0.55, 0.57, 0.62, 1), (0.92, 0.93, 0.95, 1)
    Window.clearcolor = BG

    def mk_label(text="", size=14, color=WHITE, bold=False, mono=False, halign="left", **kw):
        lb = Label(text=text, font_size=sp(size), color=color, bold=bold, halign=halign, valign="top",
                   markup=True, size_hint_y=None, **kw)
        if mono:
            lb.font_name = "RobotoMono-Regular"
        lb.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        lb.bind(texture_size=lambda w, v: setattr(w, "height", v[1] + dp(4)))
        return lb

    def mk_btn(text, cb, bg=CARD, color=WHITE, h=48, **kw):
        b = Button(text=text, on_release=lambda *_: cb(), background_normal="", background_color=bg,
                   color=color, font_size=sp(14), size_hint_y=None, height=dp(h), bold=True, **kw)
        return b

    class Card(BoxLayout):
        def __init__(self, **kw):
            kw.setdefault("orientation", "vertical")
            kw.setdefault("size_hint_y", None)
            kw.setdefault("padding", dp(10))
            kw.setdefault("spacing", dp(6))
            super().__init__(**kw)
            self.bind(minimum_height=self.setter("height"))
            with self.canvas.before:
                Color(*CARD)
                self._r = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(10)])
            self.bind(pos=self._upd, size=self._upd)

        def _upd(self, *_):
            self._r.pos, self._r.size = self.pos, self.size

    def scroll_column(spacing=8):
        sv = ScrollView(do_scroll_x=False)
        col = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(spacing), padding=dp(10))
        col.bind(minimum_height=col.setter("height"))
        sv.add_widget(col)
        return sv, col

    def popup_msg(title, text, yes=None):
        box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
        box.add_widget(mk_label(text, 14))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        pop = Popup(title=title, content=box, size_hint=(0.9, None), height=dp(260), auto_dismiss=True)
        if yes:
            row.add_widget(mk_btn("ТАК", lambda: (pop.dismiss(), yes()), bg=(0.0, 0.45, 0.3, 1)))
            row.add_widget(mk_btn("НІ", pop.dismiss))
        else:
            row.add_widget(mk_btn("OK", pop.dismiss))
        box.add_widget(row)
        pop.open()

    def labeled_switch(parent, text, key, on_change=None):
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        lb = mk_label(text, 13)
        lb.size_hint_y = 1
        lb.valign = "middle"
        row.add_widget(lb)
        sw = Switch(active=bool(TUNING.get(key, False)), size_hint_x=None, width=dp(80))
        def _chg(_w, v):
            TUNING[key] = bool(v)
            save_tuning()
            if on_change:
                on_change(v)
        sw.bind(active=_chg)
        row.add_widget(sw)
        parent.add_widget(row)
        return sw

    def labeled_input(parent, text, value, hint="", password=False, numeric=False, h=44):
        parent.add_widget(mk_label(text, 12, DIM))
        ti = TextInput(text=str(value), multiline=False, password=password, hint_text=hint, size_hint_y=None,
                       height=dp(h), font_size=sp(14), background_color=(0.13, 0.13, 0.18, 1),
                       foreground_color=WHITE, cursor_color=GREEN, padding=[dp(8), dp(10)])
        if numeric:
            ti.input_filter = "float"
        parent.add_widget(ti)
        return ti

    def pnl_color(v):
        return GREEN if v > 0 else (RED if v < 0 else DIM)

    def hexc(c):
        return "".join(f"{int(x * 255):02x}" for x in c[:3])

    # ───────── ЕКРАН: ТОРГІВЛЯ ─────────
    class TradeScreen(Screen):
        def __init__(self, **kw):
            super().__init__(name="trade", **kw)
            sv, col = scroll_column()
            self.add_widget(sv)
            c1 = Card(); col.add_widget(c1)
            self.mode_lb = mk_label("", 13, GOLD, bold=True); c1.add_widget(self.mode_lb)
            self.status_lb = mk_label("", 14); c1.add_widget(self.status_lb)
            self.go_btn = mk_btn("СТАРТ", self.toggle, bg=(0.0, 0.5, 0.32, 1), h=60)
            c1.add_widget(self.go_btn)
            c2 = Card(); col.add_widget(c2)
            self.trade_lb = mk_label("", 13); c2.add_widget(self.trade_lb)
            c2.add_widget(mk_btn("Закрити угоду зараз", self.close_now, bg=(0.45, 0.1, 0.1, 1), h=44))
            c3 = Card(); col.add_widget(c3)
            self.stats_lb = mk_label("", 13); c3.add_widget(self.stats_lb)
            c4 = Card(); col.add_widget(c4)
            c4.add_widget(mk_label("ТОП СИГНАЛІВ", 12, DIM, bold=True))
            self.sig_lb = mk_label("", 12, mono=True); c4.add_widget(self.sig_lb)
            c5 = Card(); col.add_widget(c5)
            c5.add_widget(mk_label("ЖУРНАЛ", 12, DIM, bold=True))
            self.log_lb = mk_label("", 10, DIM, mono=True); c5.add_widget(self.log_lb)

        def toggle(self):
            if ENGINE.running:
                popup_msg("Зупинити бота?", "Нові входи припиняться. Відкрита LIVE-угода лишиться на біржі з TP/SL.",
                          yes=ENGINE.stop)
            else:
                ENGINE.start()

        def close_now(self):
            if ENGINE.active or ENGINE.pending:
                popup_msg("Закрити зараз?", "Закрити відкриту угоду (або скасувати пастку) негайно?",
                          yes=ENGINE.request_close)

        def refresh(self):
            e = ENGINE
            live = TUNING.get("live_mode")
            self.mode_lb.text = f"РЕЖИМ: {e.mode_text()}" + ("  |  реальні гроші!" if e.live else "  |  віртуальні угоди")
            self.mode_lb.color = RED if e.live else GOLD
            self.status_lb.text = e.status
            self.go_btn.text = "СТОП" if e.running else "СТАРТ"
            self.go_btn.background_color = (0.55, 0.12, 0.12, 1) if e.running else (0.0, 0.5, 0.32, 1)
            td = e.active
            if td:
                price = DATA.price(td["sym"]) if not td.get("live") else bingx_trader.last_price(td["sym"])
                m = 1 if td["dir"] == "LONG" else -1
                pnl = m * (price - td["entry"]) / td["entry"] * 100 if price else 0.0
                self.trade_lb.text = (f"[b]{td['dir']} {td['sym']}[/b]  [color={hexc(pnl_color(pnl))}]{pnl:+.2f}%[/color]\n"
                                      f"вхід {td['entry']:.6g} | тейк {td['tp']:.6g} | стоп {td['sl']:.6g}\n"
                                      f"сума {td['amount']:.1f}$ ×{td['mult']} | плече {td['leverage']}"
                                      + ("\nТЕЙК 1 виконано" if td.get("tp1_done") else "")
                                      + ("\nстоп у беззбитку" if td.get("be_done") else ""))
            elif e.pending:
                p = e.pending
                self.trade_lb.text = f"[b]ПАСТКА {p['dir']} {p['sym']}[/b]\nлімітний вхід {p['entry']:.6g} чекає"
            else:
                self.trade_lb.text = "Відкритої угоди немає"
            s = e.stats
            done = s["win"] + s["loss"]
            wr = (s["win"] / done * 100) if done else 0.0
            bal = f"\nбаланс: {e.balance['total']:.2f}$ (вільно {e.balance['free']:.2f}$)" if (e.live and e.balance) else ""
            self.stats_lb.text = (f"угод: {s['trades']} | плюс {s['win']} / мінус {s['loss']} | WR {wr:.0f}%\n"
                                  f"[color={hexc(pnl_color(s['pnl']))}]Σ результат {s['pnl']:+.2f}%[/color]{bal}\n"
                                  f"поплавок: {FLOAT_MODE_UA[ADAPTIVE_FLOAT.mode]}")
            ok, why = GOVERNOR.entries_allowed()
            if not ok:
                self.stats_lb.text += f"\n[color={hexc(RED)}]{why}[/color]"
            top = e.ranked()[:7]
            self.sig_lb.text = ("\n".join(f"{r['sym'].split('/')[0]:<7}{r['dir']:<6}{r['pot']:>5.2f}% ADX{r['adx']:>3.0f} "
                                          f"{'*' * r['conf']}{' trap' if r['trap'] else ''}" for r in top)
                                or f"сканування: {e.scan_progress or '—'}")
            self.log_lb.text = "\n".join(list(LOG_BUFFER)[-12:])

    # ───────── ЕКРАН: САМОРОЗВИТОК ─────────
    class AutoScreen(Screen):
        def __init__(self, **kw):
            super().__init__(name="auto", **kw)
            sv, col = scroll_column()
            self.add_widget(sv)
            c1 = Card(); col.add_widget(c1)
            c1.add_widget(mk_label("АВТОПІЛОТ", 13, GREEN, bold=True))
            labeled_switch(c1, "Автопілот (поплавок v2, автостоп, автовідкат)", "autopilot_enabled")
            labeled_switch(c1, "Щоденний дайджест у Telegram", "digest_enabled")
            labeled_switch(c1, "AI-аудит (раз на 30 угод)", "ai_audit_enabled")
            c2 = Card(); col.add_widget(c2)
            c2.add_widget(mk_label("НЕЙРОМЕРЕЖА", 13, CYAN, bold=True))
            self._labels = {v["label"]: k for k, v in PROVIDER_DEFAULTS.items()}
            self.prov = Spinner(text=PROVIDER_DEFAULTS.get(AI_PROVIDER, PROVIDER_DEFAULTS["anthropic"])["label"],
                                values=list(self._labels), size_hint_y=None, height=dp(44), font_size=sp(14),
                                background_normal="", background_color=(0.13, 0.13, 0.18, 1))
            c2.add_widget(self.prov)
            self.model = labeled_input(c2, "Модель (порожньо = за замовчуванням)", AI_MODEL)
            self.key = labeled_input(c2, "API-ключ", CLAUDE_API_KEY, password=True)
            c2.add_widget(mk_btn("Зберегти", self.save_ai, bg=(0.0, 0.45, 0.3, 1)))
            c2.add_widget(mk_btn("Тест AI", self.test_ai))
            self.ai_msg = mk_label("", 12, DIM); c2.add_widget(self.ai_msg)
            c3 = Card(); col.add_widget(c3)
            c3.add_widget(mk_btn("AI-аудит зараз", self.audit_now))
            c3.add_widget(mk_btn("Надіслати дайджест зараз", self.digest_now))
            c3.add_widget(mk_btn("Зняти автостоп", lambda: (GOVERNOR.clear_halt(), self.refresh()), bg=(0.45, 0.1, 0.1, 1)))
            c3.add_widget(mk_btn("Скинути корекції до бази", lambda: (GOVERNOR.reset_adj(), self.refresh())))
            c4 = Card(); col.add_widget(c4)
            self.status_lb = mk_label("", 11, mono=True); c4.add_widget(self.status_lb)

        def _bg(self, fn, busy):
            self.ai_msg.text = busy
            def w():
                try:
                    ok, txt = fn()
                except Exception as ex:
                    ok, txt = False, str(ex)[:140]
                Clock.schedule_once(lambda dt: setattr(self.ai_msg, "text", ("OK: " if ok else "Помилка: ") + txt[:300]))
            threading.Thread(target=w, daemon=True).start()

        def save_ai(self):
            global CLAUDE_API_KEY, AI_PROVIDER, AI_MODEL
            prov = self._labels.get(self.prov.text, "anthropic")
            key, model = self.key.text.strip(), self.model.text.strip()
            save_json_safe(_CFG_FILE, {"provider": prov, "key": key, "model": model})
            CLAUDE_API_KEY, AI_PROVIDER, AI_MODEL = key, prov, model
            self.ai_msg.text = "Збережено"

        def test_ai(self):
            self.save_ai()
            def fn():
                txt, err = ai_complete("Відповідай одним словом: працює?", 30)
                return (not err), (txt[:80] if not err else err)
            self._bg(fn, "перевіряю...")

        def audit_now(self):
            self.save_ai()
            def fn():
                s, err = GOVERNOR.run_audit()
                return (not err), (s if not err else err)
            self._bg(fn, "AI-аудит...")

        def digest_now(self):
            def fn():
                GOVERNOR.send_digest()
                return True, "дайджест надіслано (якщо задано Telegram)"
            self._bg(fn, "дайджест...")

        def refresh(self):
            self.status_lb.text = GOVERNOR.status_text()

    # ───────── ЕКРАН: СТАТИСТИКА ─────────
    class StatsScreen(Screen):
        def __init__(self, **kw):
            super().__init__(name="stats", **kw)
            sv, col = scroll_column()
            self.add_widget(sv)
            c1 = Card(); col.add_widget(c1)
            self.win_lb = mk_label("", 13); c1.add_widget(self.win_lb)
            c2 = Card(); col.add_widget(c2)
            c2.add_widget(mk_label("ОСТАННІ УГОДИ", 12, DIM, bold=True))
            self.trades_lb = mk_label("", 11, mono=True); c2.add_widget(self.trades_lb)
            c3 = Card(); col.add_widget(c3)
            c3.add_widget(mk_label("ТІНЬОВІ СИГНАЛИ (7 діб)", 12, DIM, bold=True))
            self.shadow_lb = mk_label("", 11, mono=True); c3.add_widget(self.shadow_lb)

        def refresh(self):
            pairs = closed_pnl_series()
            s, l = window_stats(pairs[-GOV_SHORT_WINDOW:]), window_stats(pairs)
            self.win_lb.text = (f"[b]Коротке вікно[/b] ({s['n']}/{GOV_SHORT_WINDOW}): WR {s['wr']:.0%}, {s['exp']:+.3f}% на угоду\n"
                                f"[b]Довге вікно[/b] ({l['n']}/{GOV_LONG_WINDOW}): WR {l['wr']:.0%}, {l['exp']:+.3f}% на угоду\n"
                                f"Σ довгого вікна: {l['sum']:+.2f}%\n"
                                f"У пам'яті {len(TRADE_SNAPSHOTS.closed)}/{TRADE_SNAPSHOTS.MAX_CLOSED}, в архіві {ARCHIVE_COUNT[0]}")
            lines = []
            for rec in TRADE_SNAPSHOTS.closed_tail(25)[::-1]:
                r = rec.get("result") or {}
                pnl = r.get("pnl_pct")
                if pnl is None:
                    continue
                tag = "L" if rec.get("source") == "real" else "P"
                out = str(r.get("outcome", ""))
                word = "ТЕЙК" if "ТЕЙК" in out else ("СТОП" if "СТОП" in out else ("БЗ" if "БЕЗЗБИТОК" in out else "—"))
                col = hexc(pnl_color(pnl))
                lines.append(f"[color={col}]{(rec.get('ts_open') or '')[5:16].replace('T', ' ')} {tag} "
                             f"{str(rec.get('symbol', '')).split('/')[0]:<6}{str(rec.get('direction', ''))[:1]} "
                             f"{pnl:+6.2f}% {word}[/color]")
            self.trades_lb.text = "\n".join(lines) or "угод ще немає"
            sh = SHADOW.stats(time.time() - 7 * 86400)
            sl = [f"очікують {sh['pending']}, вирішено {sh['total']['rejected']}"]
            for reason, b in sh["by"].items():
                wr = (b["wins"] / b["filled"]) if b["filled"] else 0.0
                sl.append(f"{REASON_UA.get(reason, reason):<18}{b['rejected']:>3} відхил. "
                          f"WR {wr:.0%} Σ {b['sum']:+.1f}%")
            self.shadow_lb.text = "\n".join(sl)

    # ───────── ЕКРАН: НАЛАШТУВАННЯ ─────────
    class SettingsScreen(Screen):
        def __init__(self, app, **kw):
            super().__init__(name="settings", **kw)
            self.app = app
            sv, col = scroll_column()
            self.add_widget(sv)
            c1 = Card(); col.add_widget(c1)
            c1.add_widget(mk_label("РЕЖИМ ТОРГІВЛІ", 13, GOLD, bold=True))
            self.live_sw = Switch(active=bool(TUNING.get("live_mode")), size_hint_x=None, width=dp(80))
            row = BoxLayout(size_hint_y=None, height=dp(44))
            row.add_widget(mk_label("LIVE (реальні ордери BingX). Вимкнено = PAPER", 13))
            row.add_widget(self.live_sw)
            c1.add_widget(row)
            self._live_guard = False
            self.live_sw.bind(active=self._live_changed)
            self.amount = labeled_input(c1, "Сума угоди, USDT", TUNING.get("amount", 10), numeric=True)
            self.lev = labeled_input(c1, "Плече", TUNING.get("leverage", 1), numeric=True)
            self.rr = labeled_input(c1, "Фільтр R:R: мінімум (1 : X)", TUNING.get("rr_min", 2.5), numeric=True)
            labeled_switch(c1, "Фільтр R:R увімкнено", "rr_filter_enabled")
            labeled_switch(c1, "Стоп-лос на біржі", "use_sl")
            labeled_switch(c1, "Підтягування стопу (беззбиток)", "trail_enabled")
            labeled_switch(c1, "Розширення стопу за нахилом", "slope_ext_enabled")
            labeled_switch(c1, "Адаптивний поплавок", "float_enabled")
            labeled_switch(c1, "Автозапуск бота після перезапуску", "autostart")
            c1.add_widget(mk_btn("Зберегти параметри", self.save_params, bg=(0.0, 0.45, 0.3, 1)))
            c2 = Card(); col.add_widget(c2)
            c2.add_widget(mk_label("КЛЮЧІ BINGX (права: торгівля, БЕЗ виведення!)", 13, RED, bold=True))
            self.api_key = labeled_input(c2, "API key", TRADE_CFG.get("api_key", ""), password=True)
            self.api_sec = labeled_input(c2, "API secret", TRADE_CFG.get("api_secret", ""), password=True)
            c2.add_widget(mk_btn("Зберегти ключі", self.save_keys, bg=(0.0, 0.45, 0.3, 1)))
            c2.add_widget(mk_btn("Перевірити біржу (баланс)", self.test_exchange))
            self.ex_msg = mk_label("", 12, DIM); c2.add_widget(self.ex_msg)
            c3 = Card(); col.add_widget(c3)
            c3.add_widget(mk_label("TELEGRAM", 13, CYAN, bold=True))
            self.tg_token = labeled_input(c3, "Токен бота", TG_CFG.get("token", ""), password=True)
            self.tg_chat = labeled_input(c3, "Chat ID", TG_CFG.get("chat_id", ""))
            c3.add_widget(mk_btn("Зберегти і надіслати тест", self.save_tg, bg=(0.0, 0.45, 0.3, 1)))
            self.tg_msg = mk_label("", 12, DIM); c3.add_widget(self.tg_msg)
            c4 = Card(); col.add_widget(c4)
            c4.add_widget(mk_btn("Екран-заставка (береже AMOLED)", lambda: app.show_saver()))
            c4.add_widget(mk_btn("Дозволити роботу у фоні (батарея)", android_battery_exemption))
            c4.add_widget(mk_label(f"Asteria Mobile v{APP_VERSION} | дані: {STATS_DIR}", 10, DIM))

        def refresh(self):
            pass

        def _live_changed(self, _w, v):
            if self._live_guard:
                return
            if v:
                self._live_guard = True
                self.live_sw.active = False
                self._live_guard = False
                def _yes():
                    TUNING["live_mode"] = True
                    save_tuning()
                    self._live_guard = True
                    self.live_sw.active = True
                    self._live_guard = False
                popup_msg("УВІМКНУТИ LIVE?", "Бот торгуватиме РЕАЛЬНИМИ грошима. Спершу перевір ключі й малу суму. Продовжити?", yes=_yes)
            else:
                TUNING["live_mode"] = False
                save_tuning()

        def save_params(self):
            try:
                TUNING["amount"] = max(1.0, float(self.amount.text or 10))
                TUNING["leverage"] = max(1, int(float(self.lev.text or 1)))
                rr = parse_rr_input(self.rr.text)
                if rr:
                    TUNING["rr_min"] = rr
                save_tuning()
                popup_msg("Збережено", f"Сума {TUNING['amount']}$, плече {TUNING['leverage']}, R:R мін. {format_rr(TUNING['rr_min'])}")
            except Exception as e:
                popup_msg("Помилка", str(e))

        def save_keys(self):
            TRADE_CFG["api_key"], TRADE_CFG["api_secret"] = self.api_key.text.strip(), self.api_sec.text.strip()
            save_json_safe(TRADE_CFG_FILE, TRADE_CFG)
            ready = reload_trader()
            self.ex_msg.text = "Ключі збережено. " + ("Біржа готова." if ready else "Біржа недоступна (немає ключів або ccxt).")

        def test_exchange(self):
            self.ex_msg.text = "перевіряю..."
            def w():
                reload_trader()
                b = bingx_trader.get_balance() if getattr(bingx_trader, "_ready", False) else None
                txt = (f"Баланс: {b['total']:.2f}$ (вільно {b['free']:.2f}$)" if b else "Не вдалося: перевір ключі/ccxt/права")
                Clock.schedule_once(lambda dt: setattr(self.ex_msg, "text", txt))
            threading.Thread(target=w, daemon=True).start()

        def save_tg(self):
            TG_CFG["token"], TG_CFG["chat_id"] = self.tg_token.text.strip(), self.tg_chat.text.strip()
            save_json_safe(TG_CFG_FILE, TG_CFG)
            send_tg("✅ Asteria Mobile: Telegram підключено")
            self.tg_msg.text = "Збережено, тестове повідомлення надіслано"

    # ───────── ЕКРАН-ЗАСТАВКА (захист AMOLED) ─────────
    class SaverScreen(Screen):
        def __init__(self, app, **kw):
            super().__init__(name="saver", **kw)
            self.app = app
            with self.canvas.before:
                Color(0, 0, 0, 1)
                self._bg = Rectangle(pos=self.pos, size=self.size)
            self.bind(pos=lambda *_: setattr(self._bg, "pos", self.pos), size=lambda *_: setattr(self._bg, "size", self.size))
            self.lb = Label(text="", font_size=sp(14), color=(0.25, 0.3, 0.3, 1), halign="center",
                            size_hint=(None, None), size=(dp(260), dp(80)))
            self.add_widget(self.lb)
            self._k = 0

        def on_touch_down(self, touch):
            self.app.go("trade")
            return True

        def refresh(self):
            self._k += 1
            s = ENGINE.stats
            self.lb.text = f"{datetime.now().strftime('%H:%M')}\n{ENGINE.mode_text()} | {s['trades']} угод | {s['pnl']:+.2f}%"
            # повільно зсуваємо текст, щоб не вигорали пікселі
            fx = ((self._k * 37) % 100) / 100.0
            fy = ((self._k * 53) % 100) / 100.0
            self.lb.pos = (max(0, (self.width - self.lb.width) * fx), max(0, (self.height - self.lb.height) * fy))

    class AsteriaMobileApp(App):
        title = "Asteria Mobile"

        def build(self):
            root = BoxLayout(orientation="vertical")
            self.sm = ScreenManager(transition=NoTransition())
            self.screens = {"trade": TradeScreen(), "auto": AutoScreen(), "stats": StatsScreen(),
                            "settings": SettingsScreen(self), "saver": SaverScreen(self)}
            for s in self.screens.values():
                self.sm.add_widget(s)
            root.add_widget(self.sm)
            self.nav = BoxLayout(size_hint_y=None, height=dp(58), spacing=dp(2))
            self.nav_btns = {}
            for key, title in (("trade", "ТОРГІВЛЯ"), ("auto", "РОЗВИТОК"), ("stats", "СТАТИСТИКА"), ("settings", "НАЛАШТ.")):
                b = Button(text=title, on_release=lambda _b, k=key: self.go(k), background_normal="",
                           background_color=CARD, color=DIM, font_size=sp(12), bold=True)
                self.nav.add_widget(b)
                self.nav_btns[key] = b
            root.add_widget(self.nav)
            self.go("trade")
            Clock.schedule_interval(self.tick, 1.5)
            return root

        def go(self, name):
            self.sm.current = name
            self.nav.height = 0 if name == "saver" else dp(58)
            self.nav.opacity = 0 if name == "saver" else 1
            for k, b in self.nav_btns.items():
                b.color = GREEN if k == name else DIM
            self.tick(0)

        def show_saver(self):
            self.go("saver")

        def tick(self, _dt):
            try:
                self.screens[self.sm.current].refresh()
            except Exception as e:
                print(f"[UI] {e}")

        def on_start(self):
            android_keep_awake()
            if IS_ANDROID and not TUNING.get("battery_asked"):
                TUNING["battery_asked"] = True
                save_tuning()
                Clock.schedule_once(lambda dt: android_battery_exemption(), 2)
            st = load_json_safe(STATE_FILE, {})
            if st.get("was_running") and TUNING.get("autostart", True):
                Clock.schedule_once(lambda dt: ENGINE.start(), 3)

        def on_pause(self):
            return True          # не закриваємо застосунок, коли його згорнули

        def on_stop(self):
            ENGINE._save_state()

    AsteriaMobileApp().run()


if __name__ == "__main__":
    _run_ui()
