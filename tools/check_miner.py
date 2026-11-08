# ランダムなヘッダー・nonce・engine 番号で H7 とシェア判定を Python と照合する (検証用 JS)
import sys, random, struct, hashlib, time
from playwright.sync_api import sync_playwright
path = sys.argv[1]; N = int(sys.argv[2]); K = int(sys.argv[3])
random.seed(7)
with sync_playwright() as pw:
    b = pw.chromium.launch(); p = b.new_page(); p.goto("file://" + path)
    ok = 0
    for case in range(N):
        hdr = bytearray(random.randbytes(80))
        nb = random.getrandbits(32); t = random.randint(0, 50000); j = random.randrange(K)
        zt = random.choice([1, 16, 256, 4096, 65536])
        words = struct.unpack(">20I", hdr)
        css = "#job{" + "".join(f"--hw{i}l:{w & 0xffff};--hw{i}h:{w >> 16};" for i, w in enumerate(words[:19]))
        css += f"--nbl:{nb & 0xffff};--nbh:{nb >> 16};--zt:{zt}}}#clk{{--t:{t}}}.eng{{--j:{j}}}"
        t0 = time.time()
        got = p.evaluate("""(css)=>{let s=document.getElementById('in');if(!s){s=document.createElement('style');s.id='in';document.body.appendChild(s)}
          s.textContent=css;const c=getComputedStyle(document.getElementById('h'));return ['zo7l','zo7h','found'].map(n=>parseFloat(c.getPropertyValue('--'+n)))}""", css)
        dt = time.time() - t0
        nw = (nb + K * t + j) & 0xffffffff
        hdr[76:80] = nw.to_bytes(4, "big")
        d = hashlib.sha256(hashlib.sha256(bytes(hdr)).digest()).digest()
        h7 = int.from_bytes(d[28:], "big")
        top = d[31] * 256 + d[30]
        exp = [h7 & 0xffff, h7 >> 16, 1 if top < zt else 0]
        good = got == exp
        ok += good
        if not good:
            print("NG", case, got, exp)
    print(f"{ok}/{N} OK (last {dt*1000:.0f} ms)")
    b.close()
