"""マイニング用の静的スタイルシート public/miner.css と、サーバーが使う設定 public/miner.json を作る。

ページごとに変わるもの (ヘッダーの値、シェア送信用の URL) はサーバーが HTML の中に書く。

構造:
  #job  … ヘッダーの値 (--hw*) と、ジョブごとに 1 回だけの計算 (midstate など)
  #clk  … アニメーションで --t を進める
  .eng  … nonce ごとの計算。.e0, .e1, ... が並び、nonce 語 = ベース + K * floor(t) + j
          中の <i class="p"> と <i class="b{k}"> がシェア送信用 (サーバーが書くルールで画像を読む)
"""
import hashlib
import json
import os
import sys

from minercss import miner, prune, add, mod, fdiv, Var, STATIC, DYNAMIC

K_ENGINES = 2        # 1 フレームで計算する nonce の数
RATE = 40            # --t が 1 秒に進む量
DURATION = 150       # アニメーションの長さ (秒)。ページは REFRESH 秒で読み直す
REFRESH = 120
SLICE_BITS = 5       # n をこのビット数ずつの区間 (slice) に分けて URL を変える (1 区間 = 0.8 秒)

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "public")


def build():
    prog, h7, top = miner(K_ENGINES)
    n = Var("n", DYNAMIC)
    assert "n" in prog.stages
    prog.define("sl", fdiv(n, 1 << SLICE_BITS))
    for k in range(SLICE_BITS):
        prog.define(f"nb{k}", mod(fdiv(n, 1 << k), 2) if k else mod(n, 2))
    roots = ["found", "sl", "v", "top"] + [f"nb{k}" for k in range(SLICE_BITS)]
    prune(prog, roots)
    exp = prog.exported()
    inputs = [f"hw{i}{x}" for i in range(19) for x in "lh"] + ["nbl", "nbh", "zt"]

    def reg(name, inherits):
        return f'@property --{name}{{syntax:"<number>";inherits:{"true" if inherits else "false"};initial-value:0}}'

    css = []
    for name in inputs:
        css.append(reg(name, True))
    for st in (STATIC, DYNAMIC):
        for name, _ in prog.decls[st]:
            css.append(reg(name, name in exp))
    css.append(reg("t", True))
    css.append(reg("j", False))
    css.append(
        '@counter-style hex4{system:numeric;'
        'symbols:"0" "1" "2" "3" "4" "5" "6" "7" "8" "9" "a" "b" "c" "d" "e" "f";pad:4 "0"}')
    css.append("#job{" + "".join(f"--{n}:{e};" for n, e in prog.decls[STATIC]) + "}")
    css.append(f"@keyframes clk{{from{{--t:0}}to{{--t:{RATE * DURATION}}}}}")
    css.append(f"#clk{{animation:clk {DURATION}s linear forwards}}")
    css.append(".eng{" + "".join(f"--{n}:{e};" for n, e in prog.decls[DYNAMIC])
               + "counter-reset:v var(--v) top var(--top) found var(--found)}")
    for j in range(K_ENGINES):
        css.append(f".e{j}{{--j:{j}}}")
    css.append('.eng::before{content:"nonce " counter(v) "  先頭 16bit " counter(top,hex4)}')
    css.append(".eng i{display:inline-block;width:1px;height:1px}")
    text = "\n".join(css)
    with open(os.path.join(OUT, "miner.css"), "w") as f:
        f.write(text)
    meta = {
        "k_engines": K_ENGINES, "rate": RATE, "duration": DURATION, "refresh": REFRESH,
        "slice_bits": SLICE_BITS, "css_version": hashlib.sha256(text.encode()).hexdigest()[:10],
        "static_props": len(prog.decls[STATIC]), "dynamic_props": len(prog.decls[DYNAMIC]),
    }
    with open(os.path.join(OUT, "miner.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print(meta, len(text), file=sys.stderr)


if __name__ == "__main__":
    build()
