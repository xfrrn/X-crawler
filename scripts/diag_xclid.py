"""诊断 XClIdParseError: 复现 XClIdGen.create 的抓取路径，dump 返回的页面到底是什么。

用法: .venv/Scripts/python scripts/diag_xclid.py [username]
"""
import asyncio
import json
import os
import sqlite3
import sys

import bs4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "twscrape"))

from twscrape import xclid  # noqa: E402

ACCOUNTS_DB = os.path.join(os.path.dirname(__file__), "..", "data", "accounts.db")


def load_dotenv(path: str):
    """极简 .env 读取，避免引入额外依赖。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def load_account(username: str) -> tuple[dict, str | None]:
    con = sqlite3.connect(ACCOUNTS_DB)
    row = con.execute(
        "SELECT cookies, proxy FROM accounts WHERE username=?", (username,)
    ).fetchone()
    con.close()
    if row is None:
        sys.exit(f"账号 {username} 不存在于 accounts.db")
    return json.loads(row[0] or "{}"), row[1]


MARKERS = [
    ("x-web asset (ASSET_URL_RE)", r"https://[\w.-]+/x-web/[\w./-]+\.js"),
    ("entry-client-logged-out", r"entry-client-logged-out"),
    ("legacy hash map", r'\d+:"[0-9a-f]{7}"'),
    ("twitter-site-verification", r'name="twitter-site-verification"'),
    ("loading-x-anim svg", r"loading-x-anim"),
    ("document.location redirect", r">document.location ="),
    ("x/migrate form", r"action=\"https://x.com/x/migrate\""),
    ("Sign in / 登录页", r"Sign in to X|signin|/i/flow/login"),
    ("Something went wrong", r"Something went wrong|出错"),
    ("cf-ray / Cloudflare", r"cf-ray|Attention Required|Verify you are human|cf-chl"),
    ("gtm / challenge", r"px-captcha|arkose|hCaptcha|recaptcha"),
    ("X client token", r"client-transaction-id|x-client-transaction"),
]


def scan(text: str):
    found = []
    for name, pat in MARKERS:
        import re

        n = len(re.findall(pat, text, flags=re.I | re.S))
        found.append((name, n))
    return found


async def probe(label: str, proxy: str | None, cookies: dict | None, url: str):
    print(f"\n===== probe: {label} | proxy={proxy!r} | cookies={'yes' if cookies else 'no'} =====")
    clt = xclid._make_client(proxy=proxy, cookies=cookies)
    try:
        rep = await clt.get(url)
        text = rep.text
        print(f"status={rep.status_code}  url={rep.url}  content-type={rep.headers.get('content-type')}")
        print(f"length={len(text)}")
        title = ""
        soup = bs4.BeautifulSoup(text, "html.parser")
        if soup.title:
            title = soup.title.get_text()[:80]
        print(f"<title>: {title!r}")
        # 头部脚本/链接样张
        scripts = [s.get("src") for s in soup.find_all("script") if s.get("src")][:6]
        print("script srcs:", scripts)
        print("markers:")
        found_any = False
        for name, n in scan(text):
            if n:
                found_any = True
                print(f"   + {name}: {n}")
        if not found_any:
            print("   (no known markers — 纯未知页面)")
        # 保存原始 HTML 供人工查看
        safe = label.replace(" ", "_").replace("/", "_")
        with open(os.path.join(os.path.dirname(__file__), f"diag_{safe}.html"), "w", encoding="utf-8") as f:
            f.write(text)
        print(f"saved -> scripts/diag_{safe}.html")
    except Exception as e:
        print(f"   !! FAILED: {type(e).__name__}: {e}")
    finally:
        await clt.aclose()


async def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "Friedman"
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    cookies, account_proxy = load_account(username)
    print(f"账号={username}  cookies_keys={list(cookies.keys())}  account_proxy={account_proxy!r}")
    print(f"env TWS_PROXY={os.getenv('TWS_PROXY')!r}")
    proxy = account_proxy or os.getenv("TWS_PROXY")

    url = "https://x.com/tesla"

    # 1) 完全复刻 XClIdGen.create：proxy=None（当前 app 实际传入的路径）
    await probe("xclid-exact-no-proxy", None, cookies, url)
    # 2) 走 TWS_PROXY（app 进程里 httpx trust_env 可能等效于这个）
    await probe("with-TWS_PROXY", proxy, cookies, url)
    # 3) 匿名（无 cookies），对照看返回差异
    await probe("anon-no-cookies", proxy, None, url)

    # 4~6) 拆分 cookies 定位「错误页」到底是哪个字段触发的
    no_ct0 = {k: v for k, v in cookies.items() if k != "ct0"}
    await probe("cookies-no-ct0", proxy, no_ct0, url)
    no_auth = {k: v for k, v in cookies.items() if k != "auth_token"}
    await probe("cookies-no-auth_token", proxy, no_auth, url)
    guest_only = {k: v for k, v in cookies.items() if k in ("guest_id", "guest_id_ads")}
    await probe("guest-only", proxy, guest_only, url)


if __name__ == "__main__":
    asyncio.run(main())
