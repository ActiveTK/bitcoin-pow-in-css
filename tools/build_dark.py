"""Bootstrap 5.3 のダークモード ([data-bs-theme=dark] で切り替える方式) を、
JS なしで OS の設定に合わせて効くように @media (prefers-color-scheme: dark) で包み直した public/dark.css を作る。

使い方: python3 build_dark.py bootstrap.min.css   (Bootstrap 5.3.3 の CSS)
"""
import os
import re
import sys

src = open(sys.argv[1]).read()
out = []
for m in re.finditer(r"([^{}]*\[data-bs-theme=dark\][^{}]*)\{([^{}]*)\}", src):
    sel, body = m.group(1), m.group(2)
    if "carousel" in sel:  # 使わない部品
        continue
    parts = [p.strip().replace("[data-bs-theme=dark]", ":root") for p in sel.split(",")]
    out.append(",".join(parts) + "{" + body + "}")
css = ":root{color-scheme:light dark}@media (prefers-color-scheme:dark){" + "".join(out) + "}"
path = os.path.join(os.path.dirname(__file__), "..", "public", "dark.css")
open(path, "w").write(css)
print(len(out), "rules", len(css), "bytes", file=sys.stderr)
