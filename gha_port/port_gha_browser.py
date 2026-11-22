"""
港口資料收集 — Patchright (反偵測 Playwright) headless browser worker v3
=====================================================================
專門對付 Cloudflare Managed Challenge + Turnstile。
在 GitHub Actions 上用 xvfb-run 跑（headless=False + 虛擬 X display）。

v3 改進：
  - 加入人類行為模擬（滑鼠移動、隨機點擊）
  - Turnstile iframe 偵測 + 點擊 checkbox
  - 輪詢 cf-turnstile-response token
  - viewport 1280×900（和成功的 TPEX bypass 一致）
  - 修正 is_challenge_page 假陽性（Long Beach Akamai analytics）

用法：
  xvfb-run --auto-servernum python port_gha_browser.py --runner-id 0 --out out/browser_00
"""
import argparse, json, os, random, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path

from patchright.sync_api import sync_playwright

PORTS = {
    "felixstowe": {
        "name": "費利克斯托 Hutchison",
        "protection": "Cloudflare Managed Challenge + Turnstile",
        "urls": [
            {"url": "https://www.portoffelixstowe.co.uk/", "label": "homepage"},
            {"url": "https://www.portoffelixstowe.co.uk/shipping/vessel-schedule/", "label": "vessel_schedule"},
        ],
    },
    "longbeach": {
        "name": "長灘 POLB",
        "protection": "Akamai Bot Manager",
        "urls": [
            {"url": "https://polb.com/", "label": "homepage"},
            {"url": "https://polb.com/port-info/anchorages/", "label": "anchorages"},
        ],
    },
}

CHALLENGE_TITLES = [
    "just a moment", "checking your browser", "please wait",
    "attention required", "one more step", "access denied",
    "you have been blocked", "error 1015", "error 1020",
]

CF_CHALLENGE_SIGNS = [
    "cf-browser-verification", "cf-chl-bypass",
    "cf_chl_opt", "_cf_chl_tk", "challenge-form",
    "challenge-platform/h/b/orchestrate",
]

AKAMAI_BLOCK_SIGNS = [
    "reference #", "you don't have permission to access",
    "your request has been blocked",
]


def is_challenge_page(html, title=""):
    """判斷是否為 WAF 挑戰/封鎖頁。"""
    title_low = title.lower()
    for t in CHALLENGE_TITLES:
        if t in title_low:
            return True

    if title and not any(t in title_low for t in CHALLENGE_TITLES) and len(html) > 20000:
        return False

    low = html.lower()
    for s in CF_CHALLENGE_SIGNS:
        if s in low:
            return True
    for s in AKAMAI_BLOCK_SIGNS:
        if s in low:
            return True
    return False


def simulate_human(page):
    """模擬人類行為：滑鼠移動 + 隨機停頓。"""
    try:
        vw = page.viewport_size or {"width": 1280, "height": 900}
        w, h = vw["width"], vw["height"]
        points = [
            (random.randint(100, w - 100), random.randint(100, h - 100))
            for _ in range(random.randint(3, 6))
        ]
        for x, y in points:
            page.mouse.move(x, y, steps=random.randint(5, 15))
            time.sleep(random.uniform(0.1, 0.4))
        page.mouse.move(w // 2, h // 2, steps=10)
    except Exception:
        pass


def try_click_turnstile(page):
    """嘗試找到並點擊 Turnstile iframe 內的 checkbox。"""
    try:
        frames = page.frames
        for frame in frames:
            if "challenges.cloudflare.com" not in (frame.url or ""):
                continue
            try:
                cb = frame.query_selector("input[type='checkbox']")
                if cb:
                    cb.click()
                    print("    [turnstile] clicked checkbox in iframe")
                    return True
                body = frame.query_selector("body")
                if body:
                    box = body.bounding_box()
                    if box and box["width"] > 10 and box["height"] > 10:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        page.mouse.click(cx, cy)
                        print(f"    [turnstile] clicked iframe center ({cx:.0f}, {cy:.0f})")
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def poll_turnstile_token(page):
    """檢查 cf-turnstile-response hidden input 是否有值。"""
    try:
        token = page.evaluate(
            '() => { const e = document.querySelector("[name=cf-turnstile-response]"); '
            'return e ? e.value : ""; }'
        )
        return token or ""
    except Exception:
        return ""


def wait_for_challenge(page, timeout_sec=90):
    """等待 Cloudflare Managed Challenge 通過。
    策略：模擬人類 → 輪詢 token → 點擊 Turnstile → 等 cf_clearance cookie。
    """
    deadline = time.time() + timeout_sec
    prev_title = ""
    clicked = False
    mouse_done = False

    while time.time() < deadline:
        try:
            title = page.title()
        except Exception:
            title = ""

        html = page.content()

        if not is_challenge_page(html, title) and len(html) > 2000:
            return True, html, title

        elapsed = timeout_sec - (deadline - time.time())

        if not mouse_done and elapsed > 1:
            simulate_human(page)
            mouse_done = True

        token = poll_turnstile_token(page)
        if token:
            print(f"    [turnstile] token obtained ({len(token)} chars), waiting for redirect...")
            time.sleep(3)
            html = page.content()
            title = page.title()
            if not is_challenge_page(html, title):
                return True, html, title
            time.sleep(3)
            html = page.content()
            title = page.title()
            return not is_challenge_page(html, title), html, title

        if not clicked and elapsed > 3:
            clicked = try_click_turnstile(page)

        if not clicked and elapsed > 10:
            try_click_turnstile(page)

        try:
            cookies = page.context.cookies()
            cf_clear = [c for c in cookies if c["name"] == "cf_clearance"]
            if cf_clear:
                time.sleep(2)
                html = page.content()
                title = page.title()
                if not is_challenge_page(html, title):
                    return True, html, title
        except Exception:
            pass

        if title != prev_title and prev_title:
            print(f"    title changed: '{prev_title[:40]}' -> '{title[:40]}'")
        prev_title = title
        time.sleep(2)

    html = page.content()
    title = page.title()
    return False, html, title


def fetch_with_browser(context, url, label, timeout_sec=90):
    """用 Patchright 訪問 URL，等 Managed Challenge 通過後取得 HTML。"""
    result = {
        "url": url, "label": label,
        "status": None, "size": 0, "is_challenge": False,
        "has_content": False, "error": None, "title": "",
    }

    try:
        page = context.new_page()

        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
        result["status"] = resp.status if resp else None

        passed, html, title = wait_for_challenge(page, timeout_sec)

        result["size"] = len(html)
        result["title"] = title[:200]
        result["is_challenge"] = is_challenge_page(html, title)
        result["has_content"] = passed and not result["is_challenge"]
        result["html"] = html

        page.close()
    except Exception as e:
        result["error"] = str(e)[:500]
        try:
            page.close()
        except Exception:
            pass

    return result


def collect_port(context, port_id, port_def, out_dir):
    """收集一個港口的所有 URL。用同一個 context 以共享 cookie。"""
    port_result = {
        "port": port_id,
        "name": port_def["name"],
        "protection": port_def["protection"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "urls": [],
        "success": False,
    }

    for url_def in port_def["urls"]:
        url = url_def["url"]
        label = url_def["label"]
        print(f"  [{port_id}] {label}: {url}")

        r = fetch_with_browser(context, url, label, timeout_sec=90)

        url_result = {k: v for k, v in r.items() if k != "html"}
        port_result["urls"].append(url_result)

        if r.get("has_content") and r.get("html"):
            fname = f"{port_id}_{label}.html"
            (out_dir / fname).write_text(r["html"], encoding="utf-8")
            print(f"    -> {fname} ({r['size']:,} bytes, title: {r.get('title', '')[:60]})")
            port_result["success"] = True
        elif r.get("html") and r["size"] > 0:
            fname = f"{port_id}_{label}_raw.html"
            (out_dir / fname).write_text(r["html"], encoding="utf-8")
            status_tag = "CHALLENGE" if r["is_challenge"] else "SMALL"
            print(f"    -> {fname} ({status_tag}: {r['size']:,} bytes, title: {r.get('title', '')[:40]})")
        else:
            print(f"    -> FAIL: {r.get('error', 'unknown')[:100]}")

        time.sleep(random.randint(2, 4))

    json_path = out_dir / f"{port_id}.json"
    json_path.write_text(json.dumps(port_result, ensure_ascii=False, indent=2), encoding="utf-8")
    return port_result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runner-id", type=int, default=0)
    ap.add_argument("--ports", default="")
    ap.add_argument("--out", default="out/browser_00")
    ap.add_argument("--max-minutes", type=float, default=15)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.ports:
        port_ids = [p.strip() for p in args.ports.split(",") if p.strip() in PORTS]
    else:
        port_ids = list(PORTS.keys())

    print(f"Runner #{args.runner_id} | Ports: {port_ids}")
    print("IP: ", end="", flush=True)
    try:
        import urllib.request
        ip = urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode()
        print(ip)
    except Exception:
        ip = "unknown"
        print(ip)

    deadline = time.time() + args.max_minutes * 60
    results = {}

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,900",
            ],
        )

        for port_id in port_ids:
            if time.time() > deadline:
                print(f"[!] 時間到，跳過 {port_id}")
                break
            print(f"\n=== {port_id} ({PORTS[port_id]['name']}) ===")

            locale = "en-GB" if port_id == "felixstowe" else "en-US"
            tz = "Europe/London" if port_id == "felixstowe" else "America/Los_Angeles"

            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale=locale,
                timezone_id=tz,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )

            try:
                r = collect_port(context, port_id, PORTS[port_id], out_dir)
                results[port_id] = {"success": r["success"], "urls": len(r["urls"])}
            except Exception as e:
                print(f"  ERROR: {e}")
                traceback.print_exc()
                results[port_id] = {"success": False, "error": str(e)[:200]}
            finally:
                context.close()

        browser.close()
    finally:
        pw.stop()

    summary = {
        "runner_id": args.runner_id,
        "ip": ip,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": "patchright_v3_turnstile",
        "results": results,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n--- Summary ---")
    for p, r in results.items():
        tag = "OK" if r.get("success") else "FAIL"
        print(f"  [{tag}] {p}")


if __name__ == "__main__":
    main()
