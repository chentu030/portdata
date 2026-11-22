"""
Felixstowe bypass — FlareSolverr
================================
FlareSolverr is a self-hosted Cloudflare bypass using
undetected-chromedriver under the hood.
Run as GHA service container: ghcr.io/flaresolverr/flaresolverr:latest
API: POST http://localhost:8191/v1
"""
import json, os, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path
import urllib.request

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")

URLS = [
    ("https://www.portoffelixstowe.co.uk/", "homepage"),
    ("https://www.portoffelixstowe.co.uk/shipping/vessel-schedule/", "vessel_schedule"),
]

CHALLENGE_TITLES = ["just a moment", "checking your browser", "please wait",
                    "attention required", "one more step"]


def solve(url, max_timeout=120000):
    payload = json.dumps({
        "cmd": "request.get",
        "url": url,
        "maxTimeout": max_timeout,
    }).encode()
    req = urllib.request.Request(
        FLARESOLVERR_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=max_timeout // 1000 + 30) as resp:
        return json.loads(resp.read())


def extract_title(html):
    low = html.lower()
    s = low.find("<title>")
    e = low.find("</title>")
    if s > -1 and e > s:
        return html[s + 7:e].strip()
    return ""


def main():
    out_dir = Path(os.environ.get("OUT_DIR", "out/flaresolverr"))
    out_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "method": "flaresolverr",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "urls": [],
        "success": False,
    }

    print("[flaresolverr] Waiting for service...")
    health_url = FLARESOLVERR_URL.rsplit("/", 1)[0]
    ready = False
    for i in range(60):
        try:
            urllib.request.urlopen(health_url, timeout=3)
            print(f"  Ready ({i * 2}s)")
            ready = True
            break
        except Exception:
            time.sleep(2)
    if not ready:
        result["error"] = "FlareSolverr not ready after 120s"
        print(f"  ERROR: {result['error']}")
        (out_dir / "summary.json").write_text(json.dumps(result, indent=2))
        sys.exit(1)

    for url, label in URLS:
        print(f"\n  [{label}] {url}")
        info = {"url": url, "label": label, "success": False}
        try:
            t0 = time.time()
            resp = solve(url, max_timeout=120000)
            elapsed = time.time() - t0

            solution = resp.get("solution", {})
            status = solution.get("status", 0)
            html = solution.get("response", "")
            info["status"] = status
            info["size"] = len(html)
            info["elapsed"] = round(elapsed, 1)

            if status == 200 and len(html) > 5000:
                title = extract_title(html)
                if not any(ct in title.lower() for ct in CHALLENGE_TITLES):
                    info.update(success=True, title=title[:200])
                    (out_dir / f"felixstowe_{label}.html").write_text(html, encoding="utf-8")
                    print(f"    OK ({elapsed:.1f}s) title='{title[:60]}' size={len(html):,}")
                else:
                    info["title"] = title[:200]
                    (out_dir / f"felixstowe_{label}_raw.html").write_text(html, encoding="utf-8")
                    print(f"    Challenge present ({elapsed:.1f}s) title='{title[:60]}'")
            else:
                if html:
                    (out_dir / f"felixstowe_{label}_raw.html").write_text(html, encoding="utf-8")
                print(f"    FAIL status={status} size={len(html):,} ({elapsed:.1f}s)")

            info["flaresolverr_status"] = resp.get("status", "")
            info["flaresolverr_message"] = resp.get("message", "")[:200]

        except Exception as e:
            info["error"] = str(e)[:500]
            print(f"    ERROR: {e}")
            traceback.print_exc()
        result["urls"].append(info)
        time.sleep(3)

    result["success"] = any(u.get("success") for u in result["urls"])
    (out_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n--- flaresolverr: {'OK' if result['success'] else 'FAIL'} ---")


if __name__ == "__main__":
    main()
