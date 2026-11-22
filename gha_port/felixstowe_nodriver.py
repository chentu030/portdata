"""
Felixstowe bypass — nodriver（FlareSolverr 的備案）
依序嘗試多種啟動方式，第一個連得上的拿來抓頁面。
"""
import asyncio
import json, os, shutil, subprocess, sys, tempfile, time, traceback
from datetime import datetime, timezone
from pathlib import Path

URLS = [
    ("https://www.portoffelixstowe.co.uk/", "homepage"),
]

CHALLENGE_TITLES = [
    "just a moment", "checking your browser", "please wait",
    "attention required", "one more step", "access denied",
]

BASE_ARGS = ["--disable-dev-shm-usage", "--window-size=1280,900", "--lang=en-GB"]


def find_chrome():
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def is_real_page(html, title):
    t = (title or "").lower()
    return not any(ct in t for ct in CHALLENGE_TITLES) and len(html) > 5000


async def start_builtin(uc, chrome, headless):
    return await uc.start(
        headless=headless,
        sandbox=False,
        browser_executable_path=chrome,
        user_data_dir=tempfile.mkdtemp(prefix="nd_"),
        browser_args=BASE_ARGS,
    )


async def start_manual(uc, chrome):
    """自己開 Chrome 的 remote debugging port，再讓 nodriver 接上去。"""
    port = 9333
    log = open(Path(os.environ.get("OUT_DIR", "out/nodriver")) / "chrome_stderr.log", "w")
    proc = subprocess.Popen(
        [chrome, f"--remote-debugging-port={port}", "--remote-debugging-host=127.0.0.1",
         f"--user-data-dir={tempfile.mkdtemp(prefix='ndm_')}", "--no-sandbox",
         "--no-first-run", "--no-default-browser-check", *BASE_ARGS, "about:blank"],
        stdout=log, stderr=log,
    )
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
            break
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError(f"chrome exited rc={proc.returncode}")
            time.sleep(1)
    return await uc.start(host="127.0.0.1", port=port)


async def fetch(browser, out_dir, result):
    for url, label in URLS:
        print(f"\n  [{label}] {url}")
        info = {"url": url, "label": label, "success": False}
        html, title = "", ""
        try:
            tab = await browser.get(url)
            await asyncio.sleep(3)
            verified = False
            for sec in range(90):
                try:
                    html = await tab.get_content()
                    title = await tab.evaluate("document.title")
                except Exception:
                    pass
                if is_real_page(html, title):
                    info.update(success=True, title=str(title)[:200], size=len(html), seconds=sec)
                    (out_dir / f"felixstowe_{label}.html").write_text(html, encoding="utf-8")
                    print(f"    OK ({sec}s) title='{str(title)[:60]}' size={len(html):,}")
                    break
                if not verified and sec >= 5 and hasattr(tab, "verify_cf"):
                    verified = True
                    try:
                        await tab.verify_cf()
                        print("    [verify_cf] clicked")
                    except Exception as e:
                        print(f"    [verify_cf] {e}")
                if sec % 15 == 0:
                    print(f"    wait {sec}s title='{str(title)[:50]}' size={len(html)}")
                await asyncio.sleep(1)
            else:
                info.update(title=str(title)[:200], size=len(html))
                (out_dir / f"felixstowe_{label}_raw.html").write_text(html, encoding="utf-8")
                print(f"    TIMEOUT title='{str(title)[:50]}'")
        except Exception as e:
            info["error"] = str(e)[:500]
            traceback.print_exc()
        result["urls"].append(info)


async def run():
    out_dir = Path(os.environ.get("OUT_DIR", "out/nodriver"))
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"method": "nodriver", "timestamp": datetime.now(timezone.utc).isoformat(),
              "attempts": [], "urls": [], "success": False}

    import nodriver as uc
    chrome = find_chrome()
    print(f"[nodriver] version={getattr(uc, '__version__', '?')} chrome={chrome} uid={os.getuid()}")

    strategies = [
        ("manual_port", lambda: start_manual(uc, chrome)),
        ("builtin_headful", lambda: start_builtin(uc, chrome, False)),
        ("builtin_headless", lambda: start_builtin(uc, chrome, True)),
    ]
    browser = None
    for name, fn in strategies:
        try:
            browser = await fn()
            result["attempts"].append({"strategy": name, "ok": True})
            result["strategy"] = name
            print(f"  [start] {name}: connected")
            break
        except Exception as e:
            result["attempts"].append({"strategy": name, "ok": False, "error": str(e)[:300]})
            print(f"  [start] {name}: FAIL {str(e).strip()[:150]}")

    if browser:
        try:
            await fetch(browser, out_dir, result)
        finally:
            try:
                browser.stop()
            except Exception:
                pass

    result["success"] = any(u.get("success") for u in result["urls"])
    (out_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n--- nodriver: {'OK' if result['success'] else 'FAIL'} ({result.get('strategy')}) ---")


if __name__ == "__main__":
    asyncio.run(run())
    sys.stdout.flush()
    os._exit(0)  # nodriver 收尾時會在已關閉的 event loop 上報錯
