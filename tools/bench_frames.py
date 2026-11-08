# K を変えたときの nonce/秒 を測る (miner.css を K ごとに作り直し、静的な HTML で動かす)
import sys, os, subprocess, json, re, struct, random, http.server, threading
from playwright.sync_api import sync_playwright
OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(s, *a, **k): super().__init__(*a, directory=OUT, **k)
    def log_message(s, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
res = {}
for k in [int(x) for x in sys.argv[2:]]:
    src = open("build_miner.py").read().replace("K_ENGINES = 2", f"K_ENGINES = {k}")
    open(os.path.join(OUT, "bm.py"), "w").write(src)
    subprocess.run(["python3", os.path.join(OUT, "bm.py"), OUT], check=True, capture_output=True, cwd=".",
                   env={**os.environ, "PYTHONPATH": os.getcwd()})
    words = struct.unpack(">20I", random.randbytes(80))
    job = "#job{" + "".join(f"--hw{i}l:{w & 0xffff};--hw{i}h:{w >> 16};" for i, w in enumerate(words[:19])) + "--nbl:0;--nbh:0;--zt:1}"
    eng = "".join(f'<div class="eng e{j}"></div>' for j in range(k))
    open(os.path.join(OUT, "p.html"), "w").write(f'<!doctype html><link rel=stylesheet href="miner.css"><style>{job}</style><div id=job><div id=clk>{eng}</div></div>')
    with sync_playwright() as pw:
        b = pw.chromium.launch(); p = b.new_page(); p.goto(f"http://127.0.0.1:{port}/p.html"); p.wait_for_timeout(2000)
        p.evaluate("window.__f=0;(function l(){window.__f++;requestAnimationFrame(l)})()")
        p.wait_for_timeout(10000); f = p.evaluate("window.__f"); b.close()
    res[k] = f / 10 * k
    print(f"K={k}: {f/10:.2f} frames/s -> {f/10*k:.2f} nonce/s", flush=True)
