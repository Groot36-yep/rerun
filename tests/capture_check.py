"""Record a channel for a while and check the stream is continuous.

    python tests/capture_check.py URL SECONDS OUTFILE
"""
import json
import subprocess
import sys
import time
import urllib.request

url, seconds, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
base = url.split("/stream/")[0]
for _ in range(60):
    try:
        urllib.request.urlopen(base + "/health", timeout=2)
        break
    except Exception:
        time.sleep(0.5)

t0 = time.time()
p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", url, "-t", str(seconds),
                    "-c", "copy", out], capture_output=True, text=True)
wall = time.time() - t0
warnings = [l for l in p.stderr.splitlines() if l.strip()]

probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                        "-of", "json", out], capture_output=True, text=True)
pts = [float(x["pts_time"]) for x in json.loads(probe.stdout or "{}").get("packets", []) if "pts_time" in x]
pts_sorted = sorted(pts)
gaps = [b - a for a, b in zip(pts_sorted, pts_sorted[1:])]
dur = (pts_sorted[-1] - pts_sorted[0]) if pts_sorted else 0
print(f"wall={wall:.1f}s  video={dur:.1f}s  frames={len(pts)}  max_gap={max(gaps, default=0):.3f}s  "
      f"warnings={len(warnings)}")
for w in warnings[:10]:
    print("  ", w)
