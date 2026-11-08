import sys, statistics
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b = pw.chromium.launch(); p = b.new_page(); p.goto("file://" + sys.argv[1]); p.wait_for_timeout(300)
    ts = p.evaluate("""()=>{const c=document.getElementById('clk'), hs=[...document.querySelectorAll('.eng')]; const out=[];
      for(let i=0;i<20;i++){const t=performance.now(); c.style.setProperty('--t', String(i*13));
        for(const h of hs) getComputedStyle(h).getPropertyValue('--found'); out.push(performance.now()-t)} return out}""")[3:]
    print(f"dynamic recompute median {statistics.median(ts):.1f} ms min {min(ts):.1f}")
    b.close()
