# 検証用: 既知のヘッダーと nonce で SHA256d の H7 とシェア判定を CSS で計算する
import sys, hashlib, struct
from minercss import miner, prune, STATIC, DYNAMIC

def page(header80, nonce_word, zt, path, n_engines=1):
    prog, h7, top = miner(n_engines)
    prune(prog, ['found', 'zo7l', 'zo7h'])
    names = [n for st in (STATIC, DYNAMIC) for n, _ in prog.decls[st]]
    exp = prog.exported()
    inputs = [f"hw{i}{x}" for i in range(19) for x in "lh"] + ["nbl", "nbh", "zt"]
    def reg(n, inh):
        return f'@property --{n}{{syntax:"<number>";inherits:{inh};initial-value:0}}'
    css = "".join(reg(n, "true" if (n in exp or n in inputs) else "false") for n in names + inputs)
    css += "".join(reg(n, "true" if n == "t" else "false") for n in ["t", "j"])
    words = struct.unpack(">20I", header80)
    css += "#job{" + "".join(f"--hw{i}l:{w & 0xffff};--hw{i}h:{w >> 16};" for i, w in enumerate(words[:19]))
    css += f"--nbl:{nonce_word & 0xffff};--nbh:{nonce_word >> 16};--zt:{zt};"
    css += "".join(f"--{n}:{e};" for n, e in prog.decls[STATIC]) + "}"
    css += " "
    css += ".eng{" + "".join(f"--{n}:{e};" for n, e in prog.decls[DYNAMIC]) + "}"
    open(path, "w").write(f"<!doctype html><style>{css}</style><div id=job><div id=clk><div class='eng e0' id=h></div></div></div>")
    print(f"static {len(prog.decls[STATIC])} dynamic {len(prog.decls[DYNAMIC])} exported {len(exp)} css {len(css)}", file=sys.stderr)

if __name__ == "__main__":
    # ジェネシスブロック
    hdr = bytes.fromhex(
        "01000000" + "00" * 32 +
        "3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a" +
        "29ab5f49" + "ffff001d" + "1dac2b7c")
    d = hashlib.sha256(hashlib.sha256(hdr).digest()).digest()
    print("expected hash", d[::-1].hex(), "H7", d[28:].hex(), file=sys.stderr)
    nonce_word = struct.unpack(">I", hdr[76:80])[0]
    page(hdr, nonce_word, int(sys.argv[2]), sys.argv[1])
