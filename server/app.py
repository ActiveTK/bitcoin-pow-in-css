"""sha256-on-css のサーバー。

- /{name}: public/ の静的ファイル (miner.css)
- /: CSS だけで採掘するページ。読み込むたびに作業 (extranonce2) を 1 つ割り当て、
  ヘッダーの値とシェア送信用のルールを HTML に書く。<meta refresh> で定期的に読み直す。
- /s/<wid>/<slice>/<x>: シェアの通知。CSS が条件に合ったときだけ読む画像 URL。
  同じ slice の通知を集めて nonce を復元し、検証して記録する。
- 上流: Stratum プール (POOL_MODE=stratum) か、ローカルの疑似プール (POOL_MODE=fake)。
  上流からはジョブだけを受け取り、シェアの難易度はこちらで決める (二重プール)。
  上流の難易度を満たすシェアが見つかったときだけ上流へ送る。
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import secrets
import struct
import time
import urllib.parse
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = Path(os.environ.get("PUBLIC_DIR", ROOT / "public"))
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
META = json.loads((PUBLIC / "miner.json").read_text())
POOL_MODE = os.environ.get("POOL_MODE", "fake")
POOL_HOST = os.environ.get("POOL_HOST", "solo.ckpool.org")
POOL_PORT = int(os.environ.get("POOL_PORT", "3333"))
POOL_USER = os.environ.get("POOL_USER", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa.css")
SHARE_BITS = int(os.environ.get("SHARE_BITS", "10"))  # こちらのシェア: ハッシュの先頭がこのビット数だけ 0
# 計測用の当たり: CSS はこれを満たすハッシュを全部知らせ、サーバーはその数からハッシュレートを推定する
PROOF_BITS = int(os.environ.get("PROOF_BITS", "4"))
NOMINAL_RATE = 8.2  # 1 コアでの実測値 (H/s)。計測できるまでの目安
K = META["k_engines"]
L = META["slice_bits"]
SLICES = -(-META["rate"] * META["duration"] // (1 << L))
# 並列用の作業ホスト (別オリジン)。例: https://bitcoin-pow-in-css-w{i}.activetk.jp を 8 個
WORKERS = [os.environ["WORKER_URL"].format(i=i + 1) for i in range(int(os.environ.get("WORKERS", "8")))] \
    if os.environ.get("WORKER_URL") else []
PAR_CHOICES = (1, 2, 4, 8)
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")  # OGP の絶対 URL。空ならリクエストのホストから作る
GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
DIFF1 = 0x00000000FFFF0000000000000000000000000000000000000000000000000000

log = logging.getLogger("css-miner")


def dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def bits_to_target(nbits_hex):
    n = int(nbits_hex, 16)
    return (n & 0xFFFFFF) << (8 * ((n >> 24) - 3))


class Pool:
    """上流から受け取った最新のジョブを持つ。"""

    def __init__(self):
        self.job = None
        self.extranonce1 = ""
        self.en2_size = 8
        self.difficulty = 1.0
        self.status = "接続していません"
        self.writer = None
        self.msg_id = 10
        self.submitted = []

    # ---- 疑似プール
    async def run_fake(self):
        self.extranonce1 = secrets.token_hex(4)
        self.difficulty = 1.0
        self.status = "疑似プール"
        while True:
            self.job = {
                "job_id": secrets.token_hex(4),
                "prevhash": secrets.token_hex(32),
                "coinb1": "01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff20" + secrets.token_hex(8),
                "coinb2": secrets.token_hex(12) + "ffffffff0100f2052a01000000" + "1976a914" + secrets.token_hex(20) + "88ac00000000",
                "branches": [secrets.token_hex(32) for _ in range(3)],
                "version": "20000000",
                "nbits": "17034219",
                "ntime": f"{int(time.time()):08x}",
                "clean": True,
                "received": time.time(),
            }
            await asyncio.sleep(60)

    # ---- Stratum
    async def run_stratum(self):
        backoff = 5
        while True:
            try:
                self.status = f"{POOL_HOST}:{POOL_PORT} に接続中"
                reader, writer = await asyncio.wait_for(asyncio.open_connection(POOL_HOST, POOL_PORT), 15)
                self.writer = writer
                await self.send({"id": 1, "method": "mining.subscribe", "params": ["sha256-on-css/1.0"]})
                await self.send({"id": 2, "method": "mining.authorize", "params": [POOL_USER, "x"]})
                while True:
                    line = await asyncio.wait_for(reader.readline(), 600)
                    if not line:
                        raise ConnectionError("切断されました")
                    self.handle(json.loads(line))
                    backoff = 5
            except Exception as e:  # noqa: BLE001 再接続する
                self.status = f"上流との接続エラー: {e!r}"
                log.warning("stratum: %r", e)
                self.writer = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def send(self, obj):
        self.writer.write((json.dumps(obj) + "\n").encode())
        await self.writer.drain()

    def handle(self, msg):
        if msg.get("id") == 1 and msg.get("result"):
            self.extranonce1, self.en2_size = msg["result"][1], msg["result"][2]
        elif msg.get("id") == 2:
            self.status = f"{POOL_HOST}:{POOL_PORT} 認証 {'成功' if msg.get('result') else '失敗'}"
        elif msg.get("method") == "mining.set_difficulty":
            self.difficulty = float(msg["params"][0])
        elif msg.get("method") == "mining.notify":
            p = msg["params"]
            self.job = {
                "job_id": p[0], "prevhash": p[1], "coinb1": p[2], "coinb2": p[3], "branches": p[4],
                "version": p[5], "nbits": p[6], "ntime": p[7], "clean": p[8], "received": time.time(),
            }
        elif msg.get("id", 0) >= 10:
            self.submitted.append({"id": msg["id"], "result": msg.get("result"), "error": msg.get("error")})

    async def submit(self, job_id, en2, ntime, nonce_hex):
        if POOL_MODE != "stratum" or not self.writer:
            return
        self.msg_id += 1
        await self.send({"id": self.msg_id, "method": "mining.submit",
                         "params": [POOL_USER, job_id, en2, ntime, nonce_hex]})


def build_header(job, extranonce1, en2_hex):
    coinbase = bytes.fromhex(job["coinb1"] + extranonce1 + en2_hex + job["coinb2"])
    root = dsha(coinbase)
    for br in job["branches"]:
        root = dsha(root + bytes.fromhex(br))
    prev = bytes.fromhex(job["prevhash"])
    prev = b"".join(prev[i:i + 4][::-1] for i in range(0, 32, 4))
    return (bytes.fromhex(job["version"])[::-1] + prev + root
            + bytes.fromhex(job["ntime"])[::-1] + bytes.fromhex(job["nbits"])[::-1] + b"\0\0\0\0")


class Miner:
    def __init__(self, pool):
        self.pool = pool
        self.works = {}
        self.en2_counter = secrets.randbits(32)
        self.pending = {}
        self.shares = []
        self.started = time.time()
        self.pages = 0
        self.sessions = {}      # sid -> {"start": 時刻, "proofs": [(時刻, ビット数)], "shares": 数, "ip": 発行先}
        self.credited = {}      # (wid, nonce 語) -> 時刻。数えた当たり
        self.recent = []        # サイト全体の計測用の当たり (時刻, ビット数)
        DATA.mkdir(parents=True, exist_ok=True)
        self.share_log = DATA / "shares.jsonl"
        if self.share_log.exists():
            for line in self.share_log.read_text().splitlines():
                try:
                    self.shares.append(json.loads(line))
                except ValueError:
                    pass

    def new_work(self, share_bits, sid, par=1):
        job = self.pool.job
        self.en2_counter += 1
        en2 = (self.en2_counter % (1 << (8 * self.pool.en2_size))).to_bytes(self.pool.en2_size, "big").hex()
        header = build_header(job, self.pool.extranonce1, en2)
        wid = secrets.token_hex(6)
        self.works[wid] = {"wid": wid, "job": job, "en2": en2, "header": header, "created": time.time(), "bits": share_bits,
                           # 並列数が 2 倍になるごとに計測用の当たりを 1 ビット厳しくして、1 人あたりの
                           # 通知の数を並列数によらずほぼ一定にする (CDN のレート制限にかからないように)
                           "proof_bits": min(PROOF_BITS + par.bit_length() - 1, share_bits), "sid": sid,
                           "extranonce1": self.pool.extranonce1, "difficulty": self.pool.difficulty}
        cutoff = time.time() - 3600
        for k in [k for k, w in self.works.items() if w["created"] < cutoff]:
            del self.works[k]
        self.pages += 1
        if sid in self.sessions:
            self.sessions[sid]["seen"] = time.time()
        for k in [k for k, v in self.sessions.items() if v["seen"] < cutoff]:
            del self.sessions[k]
        for k in [k for k, t in self.credited.items() if t < cutoff]:
            del self.credited[k]
        return wid, self.works[wid]

    def new_session(self, ip):
        sid = secrets.token_hex(6)
        self.sessions[sid] = {"start": time.time(), "seen": time.time(), "proofs": [], "shares": 0, "ip": ip}
        return sid

    def session_ok(self, sid, ip):
        """開始ボタン (POST /start) で発行し、同じ IP から使われているセッションか"""
        ses = self.sessions.get(sid)
        return bool(ses) and ses["ip"] == ip

    def session_rate(self, sid):
        """(推定ハッシュレート, 当たりの数)。当たりが少ないうちは None"""
        ses = self.sessions.get(sid)
        if not ses or len(ses["proofs"]) < 3:
            return None, len(ses["proofs"]) if ses else 0
        elapsed = max(1.0, time.time() - ses["start"])
        return sum(1 << b for _, b in ses["proofs"]) / elapsed, len(ses["proofs"])

    def site_rate(self, window=600):
        now = time.time()
        self.recent = [(t, b) for t, b in self.recent if t > now - window]
        span = min(window, now - self.started)
        if len(self.recent) < 3 or span <= 0:
            return None, len(self.recent)
        return sum(1 << b for _, b in self.recent) / span, len(self.recent)

    def note(self, wid, sl, x):
        key = (wid, sl)
        ev = self.pending.get(key)
        if ev is None:
            ev = self.pending[key] = {"pings": set(), "bits": {}}  # bits: k -> 届いた値の集合
            asyncio.get_running_loop().call_later(2.0, lambda: asyncio.ensure_future(self.verify(key)))
        if x.startswith("p"):
            ev["pings"].add(int(x[1:]))
        elif x.startswith("b"):
            ev["bits"].setdefault(int(x[1:-1]), set()).add(int(x[-1]))

    def credit(self, work, nonce_word):
        """検証済みの計測用の当たりを 1 個数える (ハッシュレートの推定に使う)。同じ nonce は二度数えない"""
        key = (work["wid"], nonce_word)
        if key in self.credited:
            return False
        self.credited[key] = time.time()
        now = time.time()
        self.recent.append((now, work["proof_bits"]))
        if work["sid"] in self.sessions:
            self.sessions[work["sid"]]["proofs"].append((now, work["proof_bits"]))
        return True

    async def verify(self, key):
        wid, sl = key
        ev = self.pending.pop(key, None)
        work = self.works.get(wid)
        if not ev or not work:
            return
        # nonce が 1 つに決まるときだけ受け付ける: 下位ビットが全部届いていて (同じ区間で 2 回当たって
        # 両方の値が届いたビットや、キャッシュで届かなかったビットがない)、どのエンジンかの通知もあること。
        # サーバーが候補を探すと、CSS が計算していない nonce まで数えてしまう (通知を偽造した水増しもできる)
        known = {k: next(iter(vs)) for k, vs in ev["bits"].items() if len(vs) == 1}
        if len(known) != L or not ev["pings"]:
            log.info("ambiguous ping (bits %s, pings %s): %s slice %s", ev["bits"], ev["pings"], wid, sl)
            return
        n = sl * (1 << L) + sum(v << k for k, v in known.items())
        hdr = bytearray(work["header"])
        found = []
        for j in sorted(ev["pings"]):
            nonce_word = (K * n + j) & 0xFFFFFFFF
            hdr[76:80] = nonce_word.to_bytes(4, "big")
            d = dsha(bytes(hdr))
            value = int.from_bytes(d, "little")
            if value < (1 << (256 - work["proof_bits"])) and self.credit(work, nonce_word):
                found.append((nonce_word, d, value, 0))
        for nonce_word, d, value, n_unknown in found:
            if value >= (1 << (256 - work["bits"])):
                continue
            job = work["job"]
            upstream_target = int(DIFF1 / work["difficulty"]) if work["difficulty"] else 0
            share = {
                "time": time.time(), "wid": wid, "job_id": job["job_id"], "extranonce2": work["en2"],
                "nonce": nonce_word.to_bytes(4, "big")[::-1].hex(),  # Stratum の nonce (ヘッダーは little-endian)
                "hash": d[::-1].hex(), "zero_bits": 256 - value.bit_length(),
                "share_bits": work["bits"], "pool": POOL_MODE, "guessed_bits": n_unknown,
                "meets_upstream": value <= upstream_target, "meets_block": value <= bits_to_target(job["nbits"]),
            }
            if any(s["wid"] == wid and s["nonce"] == share["nonce"] for s in self.shares[-200:]):
                continue
            self.shares.append(share)
            if work["sid"] in self.sessions:
                self.sessions[work["sid"]]["shares"] += 1
            with self.share_log.open("a") as f:
                f.write(json.dumps(share) + "\n")
            log.info("share %s", share)
            if share["meets_upstream"]:
                await self.pool.submit(job["job_id"], work["en2"], job["ntime"], share["nonce"])
        if not found:
            log.info("ping without valid share: %s slice %s %s", wid, sl, ev)


# ---------------------------------------------------------------- 表示用
UNIVERSE_YEARS = 13.787e9  # 宇宙の年齢
YEAR = 365.25 * 86400
JP_UNITS = [(1e68, "無量大数"), (1e64, "不可思議"), (1e60, "那由他"), (1e56, "阿僧祇"), (1e52, "恒河沙"),
            (1e48, "極"), (1e44, "載"), (1e40, "正"), (1e36, "澗"), (1e32, "溝"), (1e28, "穣"), (1e24, "𥝱"),
            (1e20, "垓"), (1e16, "京"), (1e12, "兆"), (1e8, "億"), (1e4, "万")]
EN_UNITS = [(1e33, " decillion"), (1e30, " nonillion"), (1e27, " octillion"), (1e24, " septillion"),
            (1e21, " sextillion"), (1e18, " quintillion"), (1e15, " quadrillion"), (1e12, " trillion"),
            (1e9, " billion"), (1e6, " million"), (1e3, " thousand")]
BOOTSTRAP = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
DARK_VERSION = hashlib.sha256((PUBLIC / "dark.css").read_bytes()).hexdigest()[:10]
# 文書の color-scheme を親と iframe でそろえる (違うと iframe に不透明な背景が付く)
HEAD_COMMON = (f'<meta name="color-scheme" content="light dark"><link rel="stylesheet" href="{BOOTSTRAP}">'
               f'<link rel="stylesheet" href="/dark.css?v={DARK_VERSION}">')

T = {
    "ja": {
        "title": "CSSだけでBitcoinをマイニングしよう！！", "connecting": "プールに接続中",
        "desc": "JavaScriptを使わず、CSSだけでBitcoinのProof of Work（SHA-256を2回）を計算してマイニングします。", "start": "開始", "stop": "停止",
        "par": "{n}並列", "running": "{n}並列で計算中",
        "jswarn": "JavaScriptが有効になっています。<br>インターネットはとても危険が危ないので無効にしましょう。",
        "rate": "ハッシュレート", "shares_found": "見つけたシェア", "shares_n": "{n}個", "to_share": "シェア1個まで",
        "vs_net": "世界全体との差", "vs_net_v": "{x}分の1", "to_block": "ブロック発見まで（平均）",
        "universe": "宇宙誕生（約138億年）をあと何回繰り返す？", "universe_v": "{x}回", "measuring": "計測中",
        "site_rate": "このサイトの現在のハッシュレート", "net_rate": "世界全体のハッシュレート",
        "found": "見つかったシェア", "time": "時刻", "hash": "ハッシュ", "zeros": "先頭の0", "other": "English",
        "tweet": "ツイートする", "tweet_text": "CSSだけでBitcoinをマイニングして{k}回ハッシュ計算しました！",
        "tweet_plain": "CSSだけでBitcoinをマイニング",
        "sec": "{s}秒", "min": "{m}分{s}秒", "hour": "{h}時間{m}分", "day": "{d}日", "year": "{y}年",
    },
    "en": {
        "title": "Bitcoin's Proof of Work, written in CSS", "connecting": "Connecting to the pool",
        "desc": "Mine Bitcoin in your browser with CSS alone: the Proof of Work (double SHA-256) is computed by CSS. No JavaScript.", "start": "Start", "stop": "Stop",
        "par": "{n} cores", "par1": "1 core", "running": "Mining on {n} cores", "running1": "Mining on 1 core",
        "jswarn": "JavaScript is enabled.<br>The Internet is very dangerously dangerous, so please turn it off.",
        "rate": "Hashrate", "shares_found": "Shares found", "shares_n": "{n}", "to_share": "Time to a share",
        "vs_net": "Share of the network", "vs_net_v": "1 in {x}", "to_block": "Time to a block (average)",
        "universe": "How many more times must the universe's 13.8 billion years repeat?", "universe_v": "{x} times", "measuring": "Measuring",
        "site_rate": "This site's current hashrate", "net_rate": "Network hashrate",
        "found": "Shares found", "time": "Time (UTC)", "hash": "Hash", "zeros": "Leading zeros", "other": "日本語",
        "tweet": "Tweet", "tweet_text": "I mined Bitcoin with nothing but CSS and computed {k} hashes!",
        "tweet_plain": "Mining Bitcoin with nothing but CSS",
        "sec": "{s} s", "min": "{m} min {s} s", "hour": "{h} h {m} min", "day": "{d} days", "year": "{y} years",
    },
}


def lang_of(request):
    """?lang= があればそれ、なければ Accept-Language の最初の言語"""
    q = request.query.get("lang")
    if q in T:
        return q
    first = request.headers.get("Accept-Language", "").split(",")[0].strip().lower()
    return "ja" if first.startswith("ja") else "en"


NO_JS = "<!-- YES!! We have no JavaScript tag on this page!! -->\n"


def page(lang, title, body, extra_head="", site="", index=False):
    """index=True のトップページだけを検索に載せ、OGP を付ける"""
    t = T[lang]
    if index:
        meta = (f'<meta name="robots" content="all">\n<meta name="description" content="{html.escape(t["desc"])}">\n'
                f'<link rel="canonical" href="{site}/?lang={lang}">\n'
                f'<link rel="alternate" hreflang="ja" href="{site}/?lang=ja">\n'
                f'<link rel="alternate" hreflang="en" href="{site}/?lang=en">\n'
                f'<link rel="alternate" hreflang="x-default" href="{site}/">\n'
                f'<meta property="og:type" content="website">\n<meta property="og:url" content="{site}/">\n'
                f'<meta property="og:title" content="{html.escape(title)}">\n'
                f'<meta property="og:description" content="{html.escape(t["desc"])}">\n'
                f'<meta property="og:image" content="{site}/og.png">\n'
                f'<meta property="og:locale" content="{"ja_JP" if lang == "ja" else "en_US"}">\n'
                '<meta name="twitter:card" content="summary_large_image">\n<meta name="twitter:site" content="@ActiveTK5929">')
    else:
        meta = '<meta name="robots" content="noindex, nofollow">'
    return NO_JS + f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{meta}
<title>{title}</title>
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" href="/favicon-32.png" type="image/png" sizes="32x32">
<link rel="icon" href="/icon-192.png" type="image/png" sizes="192x192">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
{HEAD_COMMON}
<style>
.stats{{height:27rem}}.stats.idle{{height:19rem}}
.btn-x{{background:#000;color:#fff;font-weight:700}}.btn-x:hover{{background:#333;color:#fff}}
@media (prefers-color-scheme:dark){{.btn-x{{background:#fff;color:#000}}.btn-x:hover{{background:#ddd;color:#000}}}}
.ghr{{position:absolute;top:0;right:0;width:11rem;height:11rem;overflow:hidden;pointer-events:none;z-index:10}}
.ghr a{{position:absolute;top:2.9rem;right:-2.6rem;width:13rem;transform:rotate(45deg);pointer-events:auto;
background:#24292f;color:#fff;text-align:center;font-weight:700;font-size:.85rem;line-height:2.4;text-decoration:none;
outline:1px dashed rgba(255,255,255,.5);outline-offset:-4px}}
@media (prefers-color-scheme:dark){{.ghr a{{background:#f0f6fc;color:#24292f;outline-color:rgba(36,41,47,.5)}}}}
@media (max-width:575.98px){{.ghr{{width:8.5rem;height:8.5rem}}.ghr a{{top:2.1rem;right:-2rem;width:11rem;font-size:.7rem;line-height:2}}}}
@media (max-width:1250px){{.lang{{padding-right:7rem}}}}@media (max-width:575.98px){{.lang{{padding-right:5rem}}}}
@media (max-width:767.98px){{.stats{{height:34rem}}.stats.idle{{height:26rem}}}}
@media (max-width:575.98px){{.stats{{height:40rem}}.stats.idle{{height:30rem}}}}
</style>
{extra_head}
</head>
<body class="fs-5">
<div class="ghr"><a href="https://github.com/ActiveTK/bitcoin-pow-in-css">Fork me on GitHub</a></div>
<main class="container py-4" style="max-width:56rem">
{body}
<footer class="mt-5 pt-3 border-top">(c) 2026 <a href="https://x.com/ActiveTK5929">ActiveTK.</a></footer>
</main>
</body>
</html>
"""


def big(x, lang):
    """大きな数を言葉の単位で。日本語は万・億・兆…、英語は thousand, million, …"""
    units = JP_UNITS if lang == "ja" else EN_UNITS
    for u, name in units:
        if x >= u:
            v = x / u
            return (f"{v:,.0f}" if v >= 100 else f"{v:.1f}" if v >= 10 else f"{v:.2f}") + name
    return f"{x:,.0f}" if x >= 100 else f"{x:.1f}" if x >= 10 else f"{x:.2f}"


def fmt_rate(h, lang):
    sp = "" if lang == "ja" else " "  # 日本語では数値と単位の間に空白を入れない
    for u, name in [(1e21, "ZH/s"), (1e18, "EH/s"), (1e15, "PH/s"), (1e12, "TH/s"), (1e9, "GH/s"), (1e6, "MH/s"), (1e3, "kH/s")]:
        if h >= u:
            return f"{h / u:.3g}{sp}{name}"
    return f"{h:.3g}{sp}H/s"


def fmt_dur(sec, lang):
    t = T[lang]
    if sec < 60:
        return t["sec"].format(s=f"{sec:.0f}")
    if sec < 3600:
        return t["min"].format(m=f"{sec // 60:.0f}", s=f"{sec % 60:.0f}")
    if sec < 86400:
        return t["hour"].format(h=f"{sec // 3600:.0f}", m=f"{sec % 3600 // 60:.0f}")
    if sec < YEAR:
        return t["day"].format(d=f"{sec / 86400:.1f}")
    return t["year"].format(y=big(sec / YEAR, lang))


def network(miner):
    target = bits_to_target(miner.pool.job["nbits"])
    return DIFF1 / target * 2 ** 32 / 600, 2 ** 256 / (target + 1)  # (ハッシュレート, ブロックに必要な回数)


def rows(items):
    """(項目, 値) を大きく並べる。狭い画面では項目の下に値"""
    return "".join(
        f"<div class='row py-2 border-bottom align-items-center'><div class='col-sm-5 fw-bold'>{k}</div>"
        f"<div class='col-sm-7 fs-3 fw-bold'>{v}</div></div>"
        for k, v in items)


def block_items(rate, block_hashes, lang):
    t = T[lang]
    if not rate:
        return [(t["to_block"], "—"), (t["universe"], "—")]
    years = block_hashes / rate / YEAR
    return [(t["to_block"], fmt_dur(block_hashes / rate, lang)),
            (t["universe"], t["universe_v"].format(x=big(years / UNIVERSE_YEARS, lang)))]


def share_table(shares, lang):
    t = T[lang]
    shown = [s for s in shares if s["pool"] == "stratum" and s.get("share_bits") == SHARE_BITS][-10:]
    if not shown:
        return ""
    offset = 9 * 3600 if lang == "ja" else 0  # 日本語は JST、英語は UTC
    body = "".join(
        f"<tr><td>{time.strftime('%m/%d %H:%M', time.gmtime(s['time'] + offset))}</td>"
        f"<td class='font-monospace'>{html.escape(s['hash'][:20])}…</td>"
        f"<td class='fw-bold'>{s['zero_bits']}</td></tr>"
        for s in reversed(shown))
    return (f"<h2 class='h4 fw-bold mt-5'>{t['found']}</h2>"
            f"<table class='table'><thead><tr><th>{t['time']}</th><th>{t['hash']}</th><th>{t['zeros']}</th></tr></thead>"
            f"<tbody>{body}</tbody></table>")


def par_select(lang):
    t = T[lang]
    n_max = len(WORKERS) if len(WORKERS) > 1 else 1
    opts = "".join(f"<option value='{n}'>{t.get('par1' if n == 1 else 'par', t['par']).format(n=n)}</option>"
                   for n in PAR_CHOICES if n <= n_max)
    return ("<form action='/start' method='post' class='d-flex gap-3 my-4'>"
            f"<input type='hidden' name='lang' value='{lang}'>"
            f"<select name='par' class='form-select form-select-lg w-auto'>{opts}</select>"
            f"<button class='btn btn-primary btn-lg px-5'>{t['start']}</button></form>")


def lang_link(lang, href):
    other = "en" if lang == "ja" else "ja"
    sep = "&amp;" if "?" in href else "?"
    return f"<div class='lang text-end mb-3'><a class='fw-bold' href='{href}{sep}lang={other}'>{T[lang]['other']}</a></div>"


# CSS の表示 (miner.css の内容を上書き): 計算中のハッシュの先頭 16bit だけを大きく
LIVE_STYLE = ("<style>#clk .eng::before{content:counter(top,hex4)}"
              "#clk .eng{font-size:2.5rem;font-weight:700;line-height:1.2;color:var(--bs-emphasis-color,#000)}</style>")


def html_response(text):
    return web.Response(text=text, content_type="text/html",
                        headers={"Cache-Control": "no-store", "Vary": "Accept-Language"})


def client_ip(request):
    return request.headers.get("CF-Connecting-IP") or request.headers.get("X-Real-IP") or request.remote or ""


def site_url(request):
    return SITE_URL or f"{request.scheme}://{request.host}"


def int_param(value, choices, default):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if v in choices else default


async def start(request):
    """開始ボタン (POST)。セッションを発行してから採掘ページへ。
    採掘ページは発行済みのセッションでしか開けないので、リンクを踏ませて勝手に採掘させることはできない"""
    miner = request.app["miner"]
    form = await request.post()
    lang = form.get("lang") if form.get("lang") in T else lang_of(request)
    par = int_param(form.get("par"), [n for n in PAR_CHOICES if n <= max(1, len(WORKERS))], 1)
    sid = miner.new_session(client_ip(request))
    raise web.HTTPSeeOther(f"/?run=1&par={par}&sid={sid}&lang={lang}")


async def mine(request):
    miner = request.app["miner"]
    lang = lang_of(request)
    t = T[lang]
    site = site_url(request)
    running = request.query.get("run") == "1"
    par = int_param(request.query.get("par"), [n for n in PAR_CHOICES if n <= max(1, len(WORKERS))], 1)
    sid = request.query.get("sid", "")
    if not miner.pool.job:
        return html_response(page(lang, t["title"], f"<h1 class='fw-bold'>{t['connecting']}</h1>",
                                  '<meta http-equiv="refresh" content="10">'))
    if running and not miner.session_ok(sid, client_ip(request)):
        raise web.HTTPFound(f"/?lang={lang}")
    if not running:
        body = (lang_link(lang, "/")
                + f"<div id='jswarn' class='alert alert-danger fs-4 fw-bold'>{t['jswarn']}</div>"
                "<noscript><style>#jswarn{display:none}</style></noscript>"
                f"<h1 class='display-5 fw-bold'>{t['title']}</h1>"
                + par_select(lang)
                + f"<iframe src='/stats?lang={lang}' class='stats idle w-100 border-0'></iframe>"
                + f"<div class='my-4'><a class='btn btn-lg btn-x px-5' href='/tweet?lang={lang}' target='_blank' rel='noopener'>"
                f"{t['tweet']}</a></div>"
                + share_table(miner.shares, lang))
        return html_response(page(lang, t["title"], body, site=site, index=True))

    q = f"&amp;sid={sid}&amp;lang={lang}"
    again = f"/?run=1&amp;par={par}{q}"
    head = f'<meta http-equiv="refresh" content="{META["refresh"]};url={again}">'
    if WORKERS:
        # 採掘は sandbox 付きの iframe で行う。オリジンごとに別のプロセスになるので、並列なら別々の CPU コアで
        # 計算でき、1 並列でも親ページ (数字の表示) が採掘に邪魔されない
        live = "".join(
            f'<iframe sandbox src="{WORKERS[i]}/frame?par={par}{q}" class="border-0" style="width:7.5em;height:7rem"></iframe>'
            for i in range(par))
        live = f"<div class='d-flex flex-wrap'>{live}</div>"
    else:
        mhead, live = engine_markup(miner, SHARE_BITS, sid, par)
        head += mhead
    head += LIVE_STYLE  # miner.css より後に置く
    body = (lang_link(lang, again.replace(f"&amp;lang={lang}", ""))
            + "<div class='d-flex align-items-center justify-content-between my-3'>"
            f"<h1 class='display-5 fw-bold m-0'>{t.get('running1' if par == 1 else 'running', t['running']).format(n=par)}</h1>"
            f"<a class='btn btn-danger btn-lg px-5' href='/?lang={lang}'>{t['stop']}</a></div>"
            + live
            # 数字は小さな iframe だけを数秒ごとに読み直して更新する (採掘は止めない)
            + f"<iframe src='/stats?run=1&amp;par={par}{q}' class='stats w-100 border-0'></iframe>"
            # 回数は押したときにサーバーが計算して、X の投稿画面へ転送する
            + f"<div class='my-4'><a class='btn btn-lg btn-x px-5' href='/tweet?par={par}{q}' target='_blank' rel='noopener'>"
            f"{t['tweet']}</a></div>"
            + share_table(miner.shares, lang))
    return html_response(page(lang, t["title"], body, head))


def hashes_done(miner, sid, par):
    """このセッションでこれまでに計算したハッシュ数の概算。検証済みの当たりの数 × 2^ビット数。
    まだ当たりがなければ、1 コアの実測値 × 並列数 × 経過時間"""
    ses = miner.sessions.get(sid)
    if not ses:
        return 0
    if ses["proofs"]:
        return sum(1 << b for _, b in ses["proofs"])
    return NOMINAL_RATE * par * (time.time() - ses["start"])


async def tweet(request):
    miner = request.app["miner"]
    lang = lang_of(request)
    t = T[lang]
    par = int_param(request.query.get("par"), PAR_CHOICES, 1)
    k = round(hashes_done(miner, request.query.get("sid", ""), par))
    if k > 0:
        k_text = (f"{k:,}" if k < 10000 else big(k, lang)) if lang == "ja" else (f"{k:,}" if k < 1000000 else big(k, lang))
        first = t["tweet_text"].format(k=k_text)
    else:  # まだ一度も掘っていない人
        first = t["tweet_plain"]
    text = first + "\n" + (SITE_URL or site_url(request))
    raise web.HTTPFound("https://x.com/intent/post?" + urllib.parse.urlencode({"text": text}))


async def stats(request):
    """数字の表だけのページ。親ページの iframe の中で数秒ごとに読み直す。"""
    miner = request.app["miner"]
    lang = lang_of(request)
    t = T[lang]
    if not miner.pool.job:
        return web.Response(text="", content_type="text/plain")
    net_rate, block_hashes = network(miner)
    if request.query.get("run") == "1":
        par = int_param(request.query.get("par"), PAR_CHOICES, 1)
        sid = request.query.get("sid", "")
        rate, _ = miner.session_rate(sid)
        ses = miner.sessions.get(sid, {})
        r = rate or NOMINAL_RATE * par
        items = [(t["rate"], fmt_rate(rate, lang) if rate else t["measuring"]),
                 (t["shares_found"], t["shares_n"].format(n=ses.get("shares", 0))),
                 (t["to_share"], fmt_dur(2 ** SHARE_BITS / r, lang)),
                 (t["vs_net"], t["vs_net_v"].format(x=big(net_rate / r, lang)))] + block_items(r, block_hashes, lang)
        every = 3
    else:
        srate, _ = miner.site_rate()
        items = [(t["site_rate"], fmt_rate(srate, lang) if srate else "—"),
                 (t["net_rate"], fmt_rate(net_rate, lang))] + block_items(srate, block_hashes, lang)
        every = 5
    text = (NO_JS + f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta http-equiv="refresh" content="{every}">'
            f'{HEAD_COMMON}</head><body class="fs-5"><div class="container-fluid">{rows(items)}</div></body></html>')
    return html_response(text)


def engine_markup(miner, bits, sid, par=1):
    """作業を 1 つ割り当て、採掘用の <head> の中身と本体の HTML を返す。"""
    wid, work = miner.new_work(bits, sid, par)
    words = struct.unpack(">20I", work["header"])
    zt = 1 << (16 - work["proof_bits"])  # CSS は計測用の当たりを全部知らせる
    css = ["#job{" + "".join(f"--hw{i}l:{w & 0xffff};--hw{i}h:{w >> 16};" for i, w in enumerate(words[:19]))
           + f"--nbl:0;--nbh:0;--zt:{zt}}}"]
    for s in range(SLICES):
        for j in range(K):
            css.append(f"@container style(--found: 1) and style(--sl: {s}){{.e{j}>.p{{background-image:url(/s/{wid}/{s}/p{j})}}}}")
        for k in range(L):
            for v in (0, 1):
                css.append(f"@container style(--found: 1) and style(--sl: {s}) and style(--nb{k}: {v})"
                           f"{{.b{k}{{background-image:url(/s/{wid}/{s}/b{k}{v})}}}}")
    engines = "".join(
        f'<div class="eng e{j} font-monospace">' + '<i class="p"></i>' + "".join(f'<i class="b{k}"></i>' for k in range(L)) + "</div>"
        for j in range(K))
    head = (f'<link rel="stylesheet" href="/miner.css?v={META["css_version"]}">'
            f"<style>{''.join(css)}</style>")
    return head, f'<div id="job"><div id="clk">{engines}</div></div>'


async def frame(request):
    """並列用: iframe の中で採掘するだけのページ (読み直しは親ページが行う)。発行済みのセッションのときだけ"""
    miner = request.app["miner"]
    sid = request.query.get("sid", "")
    if not miner.pool.job or not miner.session_ok(sid, client_ip(request)):
        return web.Response(text="", content_type="text/plain", status=403)
    par = int_param(request.query.get("par"), PAR_CHOICES, 1)
    head, body = engine_markup(miner, SHARE_BITS, sid, par)
    text = (NO_JS + f'<!doctype html><html><head><meta charset="utf-8"><meta name="color-scheme" content="light dark">{head}{LIVE_STYLE}'
            "<style>body{margin:0;font-family:monospace}"
            "@media (prefers-color-scheme:dark){#clk .eng{color:#fff}}</style></head>"
            f"<body>{body}</body></html>")
    return web.Response(text=text, content_type="text/html", headers={"Cache-Control": "no-store"})


async def share_ping(request):
    miner = request.app["miner"]
    wid, sl, x = request.match_info["wid"], request.match_info["sl"], request.match_info["x"]
    if wid in miner.works and sl.isdigit() and len(x) <= 4:
        miner.note(wid, int(sl), x)
    return web.Response(body=GIF, content_type="image/gif", headers={"Cache-Control": "no-store"})


async def shares_json(request):
    return web.json_response(request.app["miner"].shares[-100:])


async def mine_redirect(request):
    """以前の URL (/mine) から / へ"""
    raise web.HTTPMovedPermanently("/" + (f"?{request.query_string}" if request.query_string else ""))


async def health(request):
    return web.Response(text="ok\n")


async def static(request):
    name = request.match_info["name"]
    path = (PUBLIC / name).resolve()
    if PUBLIC.resolve() not in path.parents or not path.is_file():
        raise web.HTTPNotFound()
    headers = {"Cache-Control": "public, max-age=31536000, immutable" if request.query.get("v") else "no-cache"}
    return web.FileResponse(path, headers=headers)


async def on_startup(app):
    pool = app["miner"].pool
    app["pool_task"] = asyncio.create_task(pool.run_stratum() if POOL_MODE == "stratum" else pool.run_fake())


WORKER_HOSTS = {u.split("/")[2].split(":")[0] for u in WORKERS}


@web.middleware
async def headers_mw(request, handler):
    resp = await handler(request)
    # 検索に載せるのはトップページ (採掘していない状態) だけ。ページの部品 (/stats, /frame, /s/ など)、
    # 採掘中のページ、並列用の作業ドメインは載せない
    if (request.path != "/" or "run" in request.query or "sid" in request.query
            or request.host.split(":")[0] in WORKER_HOSTS):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    # HTML と JSON は圧縮して送る (作業ごとの CSS が入るので 1 ページ数百 KB になる)
    if isinstance(resp, web.Response) and not isinstance(resp, web.FileResponse) \
            and resp.content_type in ("text/html", "application/json"):
        resp.enable_compression()
    return resp


def make_app():
    app = web.Application(middlewares=[headers_mw])
    app["miner"] = Miner(Pool())
    app.router.add_get("/health", health)
    app.router.add_get("/mine", mine_redirect)
    app.router.add_get("/frame", frame)
    app.router.add_get("/stats", stats)
    app.router.add_get("/shares.json", shares_json)
    app.router.add_get("/s/{wid}/{sl}/{x}", share_ping)
    app.router.add_get("/", mine)
    app.router.add_post("/start", start)
    app.router.add_get("/tweet", tweet)
    app.router.add_get("/{name}", static)
    app.on_startup.append(on_startup)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    web.run_app(make_app(), port=int(os.environ.get("PORT", "8080")))
