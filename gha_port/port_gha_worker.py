"""
國際港口資料收集 ── GitHub Actions worker（自給自足，只用 requests + 標準函式庫）
================================================================================
在每個 GitHub Actions runner 上跑一組被 WAF 擋住的港口網站，
用不同的 IP（runner 自帶 + Cloudflare WARP）和多種策略嘗試取得船期資料。

結果寫成 JSON，由 workflow 上傳成 artifact，port-collector VM 定期下載合併。

策略（依序嘗試，第一個成功就停）
  1. 直連＋瀏覽器 User-Agent＋完整標頭
  2. Session 模式（先 GET 首頁拿 cookie，再 GET 目標頁）
  3. 帶 Referer（Google / 該站首頁）
  4. 換另一組 User-Agent（Firefox / Edge / Safari）
  5. 加 sec-ch-ua / sec-fetch-* 標頭（模擬 Chrome navigation）

每個策略之間間隔 3～5 秒。

用法
  python port_gha_worker.py --runner-id 3 --out out/runner_03
  python port_gha_worker.py --ports algeciras,savannah --out out/test
  PORTS=algeciras,savannah python port_gha_worker.py --out out/test
"""
import argparse, gzip, hashlib, json, os, random, re, sys, time, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# 港口定義：每個港口有多個 URL 和解析方法
# ---------------------------------------------------------------------------

PORTS = {
    # === 歐洲 ===
    "algeciras": {
        "name": "阿爾赫西拉斯 APBA",
        "protection": "F5 WAF",
        "urls": [
            {"url": "https://www.apba.es/", "label": "homepage"},
        ],
        "parse": "html_table",
    },
    "felixstowe": {
        "name": "費利克斯托 Hutchison",
        "protection": "Cloudflare",
        "urls": [
            {"url": "https://www.portoffelixstowe.co.uk/", "label": "homepage"},
            {"url": "https://www.portoffelixstowe.co.uk/shipping/vessel-schedule/", "label": "vessel_schedule"},
        ],
        "parse": "html_table",
    },
    "lehavre": {
        "name": "勒阿弗爾 HAROPA",
        "protection": "Imperva",
        "urls": [
            {"url": "https://www.haropaport.com/", "label": "homepage"},
            {"url": "https://drakhar.haropaport.com/fr/liste/221/navires-attendus", "label": "navires_attendus"},
        ],
        "parse": "html_table",
    },

    # === 美洲 ===
    "longbeach": {
        "name": "長灘 POLB",
        "protection": "Akamai",
        "urls": [
            {"url": "https://polb.com/port-info/vessel-schedule/", "label": "vessel_schedule"},
            {"url": "https://www.polb.com/port-info/anchorages/", "label": "anchorages"},
        ],
        "parse": "html_table",
    },
    "savannah": {
        "name": "薩凡納 GPA",
        "protection": "Cloudflare",
        "urls": [
            {"url": "https://gaports.com/", "label": "homepage"},
            {"url": "https://gaports.com/facilities/port-of-savannah/garden-city-terminal/vessels-in-berth-and-at-anchor/", "label": "vessels_in_berth"},
            {"url": "http://savn4.gaports.com/gpa/vessel/vessel_gct.html", "label": "vessel_schedule"},
        ],
        "parse": "html_table",
    },
    "vancouver": {
        "name": "溫哥華 VFPA",
        "protection": "Cloudflare",
        "urls": [
            {"url": "https://www.portvancouver.com/", "label": "homepage"},
            {"url": "https://ehub.portvancouver.com/GPSDashboard", "label": "gps_dashboard"},
        ],
        "parse": "html_table",
    },

    # === 亞洲 ===
    "saigon": {
        "name": "西貢新港 SNP",
        "protection": "WAF",
        "urls": [
            {"url": "https://eport.saigonnewport.com.vn/", "label": "homepage"},
            {"url": "https://eport.saigonnewport.com.vn/Pages/Operation/Default", "label": "operation"},
        ],
        "parse": "html_table",
    },
    "lcit": {
        "name": "林查班 LCIT (DP World)",
        "protection": "Cloudflare",
        "urls": [
            {"url": "https://www.lcit.com/", "label": "homepage"},
        ],
        "parse": "html_table",
    },
    "hpt": {
        "name": "林查班 HPT (Hutchison)",
        "protection": "Cloudflare",
        "urls": [
            {"url": "https://hutchisonports.co.th/", "label": "homepage"},
            {"url": "https://online.hutchisonports.co.th", "label": "online_service"},
        ],
        "parse": "html_table",
    },
    "northport": {
        "name": "巴生北港 Northport",
        "protection": "WAF",
        "urls": [
            {"url": "https://www.northport.com.my/", "label": "homepage"},
        ],
        "parse": "html_table",
    },
    "dubai": {
        "name": "杜拜 Dubai Trade",
        "protection": "WAF",
        "urls": [
            {"url": "https://www.dubaitrade.ae/", "label": "homepage"},
        ],
        "parse": "html_table",
    },
}

# ---------------------------------------------------------------------------
# User-Agent 池（真實瀏覽器）
# ---------------------------------------------------------------------------

UA_CHROME_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
UA_CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
UA_FIREFOX_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
    "Gecko/20100101 Firefox/133.0"
)
UA_EDGE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
UA_SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.2 Safari/605.1.15"
)

UA_POOL = [UA_CHROME_WIN, UA_CHROME_MAC, UA_FIREFOX_WIN, UA_EDGE, UA_SAFARI]

# ---------------------------------------------------------------------------
# 挑戰頁偵測
# ---------------------------------------------------------------------------

CHALLENGE_PATTERNS = [
    "just a moment",
    "checking your browser",
    "enable javascript",
    "challenge-platform",
    "_cf_chl",
    "cf-browser-verification",
    "request rejected",
    "_Incapsula_Resource",
    "incap_ses",
    "access denied",
    "are you a human",
    "bot detection",
    "security check",
    "captcha",
    "please verify",
    "automated access",
    "distil_r_captcha",
]


def is_challenge(text, status_code):
    if status_code in (403, 406, 429, 503):
        low = text[:5000].lower()
        for pat in CHALLENGE_PATTERNS:
            if pat in low:
                return True
    if status_code == 403 and len(text) < 10000:
        return True
    return False


def has_real_content(text, min_len=5000):
    if len(text) < min_len:
        return False
    low = text.lower()
    if any(p in low for p in CHALLENGE_PATTERNS[:8]):
        return False
    return True


# ---------------------------------------------------------------------------
# 請求策略
# ---------------------------------------------------------------------------

def _base_headers(ua):
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
    }


def _chrome_nav_headers(ua):
    h = _base_headers(ua)
    h.update({
        "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
    })
    return h


def make_session():
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=1, status_forcelist=[500, 502, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


STRATEGIES = [
    {
        "name": "direct_chrome",
        "desc": "直連 Chrome UA＋完整標頭",
        "get_headers": lambda url: _chrome_nav_headers(UA_CHROME_WIN),
        "use_session": False,
        "prefetch_home": False,
        "referer": None,
    },
    {
        "name": "session_cookie",
        "desc": "Session 模式：先拿首頁 cookie 再訪問目標頁",
        "get_headers": lambda url: _chrome_nav_headers(UA_CHROME_WIN),
        "use_session": True,
        "prefetch_home": True,
        "referer": None,
    },
    {
        "name": "referer_google",
        "desc": "帶 Google Referer",
        "get_headers": lambda url: {**_chrome_nav_headers(UA_CHROME_MAC), "Referer": "https://www.google.com/"},
        "use_session": False,
        "prefetch_home": False,
        "referer": "google",
    },
    {
        "name": "referer_self",
        "desc": "帶自身站台 Referer",
        "get_headers": lambda url: {
            **_chrome_nav_headers(UA_CHROME_WIN),
            "Referer": f"{urlparse(url).scheme}://{urlparse(url).netloc}/",
            "sec-fetch-site": "same-origin",
        },
        "use_session": True,
        "prefetch_home": True,
        "referer": "self",
    },
    {
        "name": "firefox_ua",
        "desc": "Firefox UA＋基礎標頭",
        "get_headers": lambda url: _base_headers(UA_FIREFOX_WIN),
        "use_session": False,
        "prefetch_home": False,
        "referer": None,
    },
    {
        "name": "edge_ua",
        "desc": "Edge UA＋Chrome sec 標頭",
        "get_headers": lambda url: _chrome_nav_headers(UA_EDGE),
        "use_session": True,
        "prefetch_home": True,
        "referer": None,
    },
    {
        "name": "safari_ua",
        "desc": "Safari UA＋基礎標頭",
        "get_headers": lambda url: _base_headers(UA_SAFARI),
        "use_session": False,
        "prefetch_home": False,
        "referer": None,
    },
]

# ---------------------------------------------------------------------------
# 核心：嘗試所有策略取得一個 URL
# ---------------------------------------------------------------------------

TIMEOUT = 30


def try_fetch(url, strategies=None, delay_range=(3, 5)):
    strategies = strategies or STRATEGIES
    results = []
    for strat in strategies:
        time.sleep(random.uniform(*delay_range))
        result = {
            "strategy": strat["name"],
            "desc": strat["desc"],
            "url": url,
            "status": None,
            "size": 0,
            "is_challenge": None,
            "has_content": None,
            "error": None,
            "content_hash": None,
            "snippet": "",
        }
        try:
            sess = make_session() if strat["use_session"] else None
            headers = strat["get_headers"](url)
            requester = sess if sess else requests

            if strat["prefetch_home"] and sess:
                home = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
                if home != url:
                    try:
                        sess.get(home, headers=headers, timeout=TIMEOUT, allow_redirects=True)
                        time.sleep(random.uniform(1, 2))
                    except Exception:
                        pass

            resp = requester.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
            text = resp.text
            result["status"] = resp.status_code
            result["size"] = len(text)
            result["is_challenge"] = is_challenge(text, resp.status_code)
            result["has_content"] = has_real_content(text)
            result["content_hash"] = hashlib.sha256(text.encode()[:10000]).hexdigest()[:16]
            result["snippet"] = re.sub(r'\s+', ' ', text[:500]).strip()

            if resp.status_code == 200 and not result["is_challenge"]:
                result["content"] = text
                results.append(result)
                return results, result
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        results.append(result)

    return results, None


# ---------------------------------------------------------------------------
# 港口處理
# ---------------------------------------------------------------------------

def process_port(port_key, port_def, out_dir):
    print(f"\n{'='*60}")
    print(f"[{port_key}] {port_def['name']} ({port_def['protection']})")
    print(f"{'='*60}")

    port_result = {
        "port": port_key,
        "name": port_def["name"],
        "protection": port_def["protection"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "urls": [],
        "success": False,
        "winning_strategy": None,
        "data_saved": False,
    }

    for url_info in port_def["urls"]:
        url = url_info["url"]
        label = url_info["label"]
        print(f"\n  [{label}] {url}")

        attempts, winner = try_fetch(url)

        url_result = {
            "url": url,
            "label": label,
            "attempts": [{k: v for k, v in a.items() if k != "content"} for a in attempts],
            "success": winner is not None,
        }

        if winner:
            print(f"  ✅ 成功！策略: {winner['strategy']}, 大小: {winner['size']}")
            url_result["winning_strategy"] = winner["strategy"]
            url_result["response_size"] = winner["size"]
            port_result["success"] = True
            port_result["winning_strategy"] = winner["strategy"]

            content = winner.get("content", "")
            if content:
                fname = f"{port_key}_{label}.html"
                fpath = out_dir / fname
                if len(content) > 200_000:
                    fpath = out_dir / f"{fname}.gz"
                    fpath.write_bytes(gzip.compress(content.encode("utf-8")))
                else:
                    fpath.write_text(content, encoding="utf-8")
                url_result["saved_file"] = fname
                port_result["data_saved"] = True
                print(f"  💾 已存: {fname}")
        else:
            reasons = set()
            for a in attempts:
                if a["is_challenge"]:
                    reasons.add(f"challenge(s={a['status']})")
                elif a["error"]:
                    reasons.add(a["error"][:60])
                elif a["status"]:
                    reasons.add(f"http_{a['status']}")
            print(f"  ❌ 全部失敗: {', '.join(reasons) or 'unknown'}")

        port_result["urls"].append(url_result)

    meta_path = out_dir / f"{port_key}.json"
    meta_path.write_text(json.dumps(port_result, ensure_ascii=False, indent=2), encoding="utf-8")
    return port_result


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="國際港口資料收集 GHA worker")
    parser.add_argument("--runner-id", type=int, default=0)
    parser.add_argument("--ports", type=str, default="",
                        help="逗號分隔的港口代碼；空 = 全部")
    parser.add_argument("--out", type=str, default="out")
    parser.add_argument("--max-minutes", type=float, default=25)
    args = parser.parse_args()

    port_list = os.environ.get("PORTS", args.ports)
    if port_list.strip():
        keys = [k.strip() for k in port_list.split(",") if k.strip()]
    else:
        keys = list(PORTS.keys())

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.time() + args.max_minutes * 60

    # 顯示 runner 資訊
    try:
        my_ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception:
        my_ip = "unknown"
    print(f"Runner #{args.runner_id}  IP: {my_ip}")
    print(f"目標港口: {', '.join(keys)}")
    print(f"時限: {args.max_minutes} 分鐘")

    summary = {
        "runner_id": args.runner_id,
        "ip": my_ip,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ports_total": len(keys),
        "ports_success": 0,
        "ports_failed": 0,
        "results": {},
    }

    for key in keys:
        if time.time() > deadline:
            print(f"\n⏰ 時限到，剩餘 {len(keys) - list(keys).index(key)} 個港口未處理")
            break

        if key not in PORTS:
            print(f"\n⚠️ 未知港口: {key}")
            continue

        try:
            result = process_port(key, PORTS[key], out_dir)
            summary["results"][key] = {
                "success": result["success"],
                "winning_strategy": result["winning_strategy"],
                "data_saved": result["data_saved"],
            }
            if result["success"]:
                summary["ports_success"] += 1
            else:
                summary["ports_failed"] += 1
        except Exception as e:
            print(f"\n💥 {key} 例外: {e}")
            traceback.print_exc()
            summary["results"][key] = {"success": False, "error": str(e)}
            summary["ports_failed"] += 1

        time.sleep(random.uniform(2, 4))

    summary["elapsed_sec"] = round(time.time() - (deadline - args.max_minutes * 60), 1)

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"完成：成功 {summary['ports_success']}, 失敗 {summary['ports_failed']}")
    print(f"耗時 {summary['elapsed_sec']} 秒")
    print(f"結果: {out_dir}")


if __name__ == "__main__":
    main()
