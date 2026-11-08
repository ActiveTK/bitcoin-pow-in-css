"""CSS だけで Bitcoin 方式のマイニング (SHA-256 を 2 回) をするスタイルシートの生成器。

式を小さな木で表し、生成時に次のことを自動で行う。
  - 定数の畳み込み (K、初期値、パディング語など。ビット単位の XOR も)
  - 静的 / 動的の振り分け。ジョブのヘッダーだけで決まる値は親要素 (#job) で 1 回だけ計算し、
    nonce に依存する値だけをアニメーションのたびに計算する子要素 (.eng) に置く。
    足し算は静的な項と動的な項に分け、静的な部分を親要素で先に足しておく。

制約 (sha256css.py と同じ): 値は 999999 以下、calc の + - の前後は空白。
"""
import re

# SHA-256 の定数
K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]
H0 = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]

STATIC, DYNAMIC = 0, 1


# ---------------------------------------------------------------- 式の木
class Node:
    pass


class Lit(Node):
    def __init__(self, v):
        self.v = v


class Var(Node):
    def __init__(self, name, stage):
        self.name, self.stage = name, stage


class Add(Node):
    def __init__(self, items):
        self.items = items  # (係数, Node) の並び


class Mod(Node):
    def __init__(self, x, m):
        self.x, self.m = x, m


class FDiv(Node):  # floor(x / d)
    def __init__(self, x, d):
        self.x, self.d = x, d


class Prod(Node):  # x * y (どちらも非定数)
    def __init__(self, x, y):
        self.x, self.y = x, y


class Maj3(Node):  # round(sum / 3) — 3 つのビットの多数決
    def __init__(self, x):
        self.x = x


def stage(n):
    if isinstance(n, Lit):
        return STATIC
    if isinstance(n, Var):
        return n.stage
    if isinstance(n, Add):
        return max((stage(x) for _, x in n.items), default=STATIC)
    if isinstance(n, (Mod, FDiv, Maj3)):
        return stage(n.x)
    if isinstance(n, Prod):
        return max(stage(n.x), stage(n.y))
    raise TypeError(n)


def add(*terms):
    """terms: Node か (係数, Node)。定数をまとめ、入れ子の Add を平らにする。"""
    const, items = 0, []

    def put(c, n):
        nonlocal const
        if c == 0:
            return
        if isinstance(n, Lit):
            const += c * n.v
        elif isinstance(n, Add):
            for c2, n2 in n.items:
                put(c * c2, n2)
        else:
            items.append((c, n))

    for t in terms:
        if isinstance(t, tuple):
            put(*t)
        else:
            put(1, t)
    if not items:
        return Lit(const)
    if const:
        items.append((const, Lit(1)))
    if len(items) == 1 and items[0][0] == 1:
        return items[0][1]
    return Add(items)


def mod(x, m):
    if isinstance(x, Lit):
        return Lit(x.v % m)
    return Mod(x, m)


def fdiv(x, d):
    if isinstance(x, Lit):
        return Lit(x.v // d)
    if d == 1:
        return x
    return FDiv(x, d)


def prod(x, y):
    if isinstance(x, Lit):
        return add((x.v, y))
    if isinstance(y, Lit):
        return add((y.v, x))
    return Prod(x, y)


def bit_xor(bits):
    """0/1 の Node の XOR"""
    p, vs = 0, []
    for b in bits:
        if isinstance(b, Lit):
            p ^= b.v
        else:
            vs.append(b)
    if not vs:
        return Lit(p)
    if len(vs) == 1:
        return add((-1, vs[0]), Lit(1)) if p else vs[0]
    return mod(add(*vs, Lit(p)), 2)


def bit_maj(a, b, c):
    lits = [x.v for x in (a, b, c) if isinstance(x, Lit)]
    vs = [x for x in (a, b, c) if not isinstance(x, Lit)]
    if not vs:
        return Lit(1 if sum(lits) >= 2 else 0)
    if len(vs) == 1:  # 定数 2 つ: 同じならその値、違えば変数そのもの
        return Lit(lits[0]) if lits[0] == lits[1] else vs[0]
    if len(vs) == 2:  # maj(0,x,y)=x*y, maj(1,x,y)=x+y-x*y
        x, y = vs
        return Prod(x, y) if lits[0] == 0 else add(x, y, (-1, Prod(x, y)))
    return Maj3(add(*vs))


def render(n, top=False):
    if isinstance(n, Lit):
        return str(n.v)
    if isinstance(n, Var):
        return f"var(--{n.name})"
    if isinstance(n, Add):
        out = []
        for i, (c, x) in enumerate(n.items):
            if isinstance(x, Lit):
                s = str(abs(c))
            else:
                s = render(x)
                if isinstance(x, Add):
                    s = f"({s})"
                if abs(c) != 1:
                    s = f"{s}*{abs(c)}"
            if i == 0:
                out.append(s if c > 0 else f"-1*{s}" if not isinstance(x, Lit) else f"-{s}")
            else:
                out.append((" + " if c > 0 else " - ") + s)
        s = "".join(out)
        return f"calc({s})" if top else s
    if isinstance(n, Mod):
        return f"mod({render(n.x)},{n.m})"
    if isinstance(n, FDiv):
        x = render(n.x)
        if isinstance(n.x, Add):
            x = f"({x})"
        return f"round(down,{x}/{n.d})"
    if isinstance(n, Prod):
        x, y = render(n.x), render(n.y)
        if isinstance(n.x, Add):
            x = f"({x})"
        if isinstance(n.y, Add):
            y = f"({y})"
        s = f"{x}*{y}"
        return f"calc({s})" if top else s
    if isinstance(n, Maj3):
        return f"round(({render(n.x)})/3)"
    raise TypeError(n)


# ---------------------------------------------------------------- 定義の置き場所
class Program:
    def __init__(self):
        self.decls = {STATIC: [], DYNAMIC: []}
        self.stages = {}
        self.n = 0

    def input(self, name, st):
        self.stages[name] = st
        return Var(name, st)

    def define(self, name, node):
        """node を変数にする。定数や単独の変数ならそのまま返す。"""
        if isinstance(node, (Lit, Var)):
            return node
        st = stage(node)
        self.decls[st].append((name, render(node, top=True)))
        self.stages[name] = st
        return Var(name, st)

    def define_split(self, name, node):
        """足し算を静的な部分と動的な部分に分けて定義する。"""
        if isinstance(node, Add) and stage(node) == DYNAMIC:
            s_items = [(c, x) for c, x in node.items if stage(x) == STATIC]
            d_items = [(c, x) for c, x in node.items if stage(x) == DYNAMIC]
            if len(s_items) >= 2 or (s_items and not isinstance(s_items[0][1], Lit)):
                sv = self.define(name + "s", add(*s_items))
                return self.define(name, add(sv, *d_items))
        return self.define(name, node)

    def exported(self):
        """動的な式から参照される静的な変数 (親から子へ継承させる必要がある)"""
        used = set()
        for _, e in self.decls[DYNAMIC]:
            used.update(re.findall(r"var\(--([a-z0-9_]+)\)", e))
        return {u for u in used if self.stages.get(u) == STATIC}


# ---------------------------------------------------------------- 32bit 語
class Word:
    def __init__(self, prog, name, lo, hi):
        self.prog, self.name, self.lo, self.hi = prog, name, lo, hi
        self._bits = None

    @staticmethod
    def lit(prog, v):
        return Word(prog, None, Lit(v & 0xffff), Lit(v >> 16))

    def bits(self):
        if self._bits is None:
            out = []
            for i in range(32):
                limb = self.lo if i < 16 else self.hi
                k = i % 16
                if isinstance(limb, Lit):
                    out.append(Lit((limb.v >> k) & 1))
                    continue
                if k == 0:
                    e = mod(limb, 2)
                elif k == 15:
                    e = fdiv(limb, 32768)
                else:
                    e = mod(fdiv(limb, 1 << k), 2)
                out.append(self.prog.define(f"{self.name}b{i}", e))
            self._bits = out
        return self._bits


def limb_terms(per_bit, half):
    """per_bit(i) が返す Node の並びに 2^i を掛けたもの。静的な成分と動的な成分は分けておく。"""
    base = 0 if half == 0 else 16
    s_terms, d_terms = [], []
    for i in range(16):
        parts = per_bit(base + i)
        s = add(*[p for p in parts if stage(p) == STATIC])
        d = add(*[p for p in parts if stage(p) == DYNAMIC])
        if not (isinstance(s, Lit) and s.v == 0):
            s_terms.append((1 << i, s))
        if not (isinstance(d, Lit) and d.v == 0):
            d_terms.append((1 << i, d))
    return s_terms + d_terms


def add_words(prog, name, terms_lo, terms_hi):
    """lo / hi の項の和を mod 2^32 で 1 語にする。"""
    rlo = prog.define_split(name + "rl", add(*terms_lo))
    rhi = prog.define_split(name + "rh", add(*terms_hi))
    lo = prog.define(name + "l", mod(rlo, 65536))
    hi = prog.define(name + "h", mod(add(rhi, fdiv(rlo, 65536)), 65536))
    return Word(prog, name, lo, hi)


def xor3_bits(b, r1, r2, r3, shr=None):
    def f(i):
        idx = [(i + r1) % 32, (i + r2) % 32]
        if shr is None:
            idx.append((i + r3) % 32)
        elif i + shr < 32:
            idx.append(i + shr)
        return bit_xor([b[j] for j in idx])
    return f


def compress(prog, tag, W16, init, rounds=64, outputs="all"):
    """W16: 16 語 (Word)。init: 8 語 (Word)。
    outputs="all" なら 8 語の出力 (init と足したもの)、"h7" なら H7 だけを返す。"""
    W = list(W16)
    need_w = rounds
    for t in range(16, need_w):
        b2, b15 = W[t - 2].bits(), W[t - 15].bits()
        s1 = xor3_bits(b2, 17, 19, None, shr=10)
        s0 = xor3_bits(b15, 7, 18, None, shr=3)
        per_bit = lambda i, s1=s1, s0=s0: [s1(i), s0(i)]
        lo = [W[t - 7].lo, W[t - 16].lo] + limb_terms(per_bit, 0)
        hi = [W[t - 7].hi, W[t - 16].hi] + limb_terms(per_bit, 1)
        W.append(add_words(prog, f"{tag}w{t}", lo, hi))

    a, b, c, d, e, f, g, h = init
    for t in range(rounds):
        eb, fb, gb = e.bits(), f.bits(), g.bits()
        ab, bb, cb = a.bits(), b.bits(), c.bits()
        S1 = xor3_bits(eb, 6, 11, 25)
        S0 = xor3_bits(ab, 2, 13, 22)
        # Ch = g + e*(f - g)。g の分は語のまま 1 回足す
        t1b = lambda i, S1=S1, eb=eb, fb=fb, gb=gb: [S1(i), prod(eb[i], add(fb[i], (-1, gb[i])))]
        t2b = lambda i, S0=S0, ab=ab, bb=bb, cb=cb: [S0(i), bit_maj(ab[i], bb[i], cb[i])]
        kt = Word.lit(prog, K[t])
        r = f"{tag}r{t}"
        t1lo = prog.define_split(r + "al", add(h.lo, kt.lo, W[t].lo, g.lo, *limb_terms(t1b, 0)))
        t1hi = prog.define_split(r + "ah", add(h.hi, kt.hi, W[t].hi, g.hi, *limb_terms(t1b, 1)))
        t2lo = prog.define_split(r + "bl", add(*limb_terms(t2b, 0)))
        t2hi = prog.define_split(r + "bh", add(*limb_terms(t2b, 1)))
        ne_lo_raw = add(d.lo, t1lo)
        ne = Word(prog, r + "e", prog.define(r + "el", mod(ne_lo_raw, 65536)),
                  prog.define(r + "eh", mod(add(d.hi, t1hi, fdiv(ne_lo_raw, 65536)), 65536)))
        na_lo_raw = add(t1lo, t2lo)
        na = Word(prog, r + "a", prog.define(r + "nl", mod(na_lo_raw, 65536)),
                  prog.define(r + "nh", mod(add(t1hi, t2hi, fdiv(na_lo_raw, 65536)), 65536)))
        h, g, f, e, d, c, b, a = g, f, e, ne, c, b, a, na

    if outputs == "h7":
        # H7 = 初期値の h + 64 ラウンド後の h。これはラウンド 60 で作られた e なので、
        # rounds=61 で止めたときの e がそのまま使える
        assert rounds == 61
        return add_words(prog, f"{tag}o7", [init[7].lo, e.lo], [init[7].hi, e.hi])
    out = []
    for k, w in enumerate([a, b, c, d, e, f, g, h]):
        out.append(add_words(prog, f"{tag}o{k}", [init[k].lo, w.lo], [init[k].hi, w.hi]))
    return out


# ---------------------------------------------------------------- マイニング本体
def miner(n_engines=1):
    """ヘッダー (--hw{i}l / --hw{i}h, i = 0..18) と nonce 語 (--t, --j から) から
    SHA256d の H7 と、シェア判定 --found を計算するプログラム。"""
    prog = Program()
    hw = [Word(prog, f"hw{i}", prog.input(f"hw{i}l", STATIC), prog.input(f"hw{i}h", STATIC)) for i in range(19)]
    h0 = [Word.lit(prog, v) for v in H0]
    # 1 ブロック目: 静的 (ジョブごとに 1 回)
    mid = compress(prog, "x", hw[:16], h0)
    # 2 ブロック目: 3 語目が nonce
    t = prog.input("t", DYNAMIC)
    j = prog.input("j", DYNAMIC)
    n = prog.define("n", FDiv(t, 1))  # t は小数で進むので切り捨てが必要 (fdiv は ÷1 を省いてしまう)
    v = prog.define("v", add((n_engines, n), j))
    # nonce 語 = ベース (静的, --nbl/--nbh) + v
    nb = Word(prog, "nb", prog.input("nbl", STATIC), prog.input("nbh", STATIC))
    nonce = add_words(prog, "nw", [nb.lo, mod(v, 65536)], [nb.hi, fdiv(v, 65536)])
    pad2 = [Word.lit(prog, 0x80000000)] + [Word.lit(prog, 0)] * 10 + [Word.lit(prog, 640)]
    blk2 = compress(prog, "y", hw[16:19] + [nonce] + pad2, mid)
    # 2 回目の SHA-256: H7 だけ (最後の 3 ラウンドは不要)
    pad3 = [Word.lit(prog, 0x80000000)] + [Word.lit(prog, 0)] * 6 + [Word.lit(prog, 256)]
    h7 = compress(prog, "z", blk2 + pad3, h0, rounds=61, outputs="h7")
    # シェア判定: ハッシュを little-endian の 256bit 値とみたときの上位 16bit = bswap16(H7 の下位 16bit)
    top = prog.define("top", add((256, mod(h7.lo, 256)), fdiv(h7.lo, 256)))
    prog.input("zt", STATIC)  # 上位 16bit がこれ未満ならシェア (= 2^(16 - 先頭の 0 ビット数))
    prog.decls[DYNAMIC].append(("found", f"calc(1 - min(1, round(down,{render(top)}/var(--zt))))"))
    prog.stages["found"] = DYNAMIC
    return prog, h7, top


def prune(prog, roots):
    """roots から参照されない定義を消す。"""
    exprs = {n: e for st in (STATIC, DYNAMIC) for n, e in prog.decls[st]}
    keep, stack = set(), list(roots)
    while stack:
        n = stack.pop()
        if n in keep or n not in exprs:
            continue
        keep.add(n)
        stack.extend(re.findall(r"var\(--([a-z0-9_]+)\)", exprs[n]))
    for st in (STATIC, DYNAMIC):
        prog.decls[st] = [(n, e) for n, e in prog.decls[st] if n in keep]
    return keep
