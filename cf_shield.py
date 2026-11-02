#!/usr/bin/env python3
"""cf_shield.py — 通用 Cloudflare Turnstile 过盾求解器（自包含，整个 cf_shield/ 目录可拷走）

依赖：
  - DrissionPage（pip install DrissionPage，自带 websocket-client，无需 Node）
  - 一个 headed Chrome/Chromium（CF 能识别 headless，必须 headed）
  - Linux 无桌面时自动套 xvfb-run

站点差异全部走 CLI 参数（默认值即最常见的 Turnstile 接入形态）：
  --url            过盾页面（widget 挂载的页面）
  --click-text     需先点击某按钮 widget 才渲染时传按钮文本，可重复传；页面直接渲染 widget 则不用传
  --wait-text      点击后等待页面出现的文本（确认面板已打开），可选
  --host-selector  widget 宿主元素 CSS 选择器（CDP 坐标点击兜底定位用）
  --proxy          与业务请求同一出口 IP（http://user:pass@host:port），cf_clearance 与出口 IP 绑定
  --timeout        单次过盾最长等待秒数（起浏览器 + 过质询，实测约 17~30s）

用法：
  python cf_shield.py --url https://example.com/account --timeout 60
  python cf_shield.py --url ... --click-text "Create account" --wait-text "Pick a username"
  python cf_shield.py --url ... --proxy http://user:pass@host:port

输出（stdout 一行 JSON，调用方解析最后一行即可）：
  {"ok": true, "token": "...", "tokenLen": 816, "ua": "...", "cf_clearance": "...", "elapsed_s": 17.1}
  {"ok": false, "error": "..."}

也可作为模块导入：
  from cf_shield import solve
  solve(url="https://example.com/account", timeout_s=60, proxy=None,
        click_texts=("Create account",), wait_text="Pick a username")

两个非直觉点（实测）：
  - widget 常不在页面加载时渲染，要先点开注册/登录面板才挂载；
  - CF 的 challenge iframe 不在主文档 DOM 里（querySelectorAll 找不到，CDP target 列表里有），必须走 CDP 点击。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCH_DIR = HERE / "turnstile_patch"  # CF 补丁扩展（MV3，修 MouseEvent.screenX/Y）

# 页面里读 token：turnstile.getResponse() 或隐藏 input（Turnstile 标准隐藏域名）
TOKEN_JS = """
(() => {
  try { const t = turnstile.getResponse(); if (t && String(t).length > 20) return String(t); } catch (e) {}
  const inputs = document.querySelectorAll('[name="cf-turnstile-response"]');
  for (const input of inputs) { if (input.value && input.value.length > 20) return input.value; }
  return '';
})()
"""

WIDGET_INPUT = '[name="cf-turnstile-response"]'  # Turnstile 标准隐藏 input，挂载即出现


def emit(ok: bool, **extra) -> int:
    print(json.dumps({"ok": ok, **extra}, ensure_ascii=False))
    return 0 if ok else 1


def build_host_js(selector: str) -> str:
    """按调用方给的宿主选择器生成定位 JS：优先隐藏 input 的最近宿主，其次选择器本身"""
    sel = json.dumps(selector)
    return """(() => {
  const inp = document.querySelector(%s);
  const box = document.querySelector(%s);
  const host = (inp && (inp.closest(%s) || inp.parentElement)) || box;
  if (!host) return { found: false };
  const r = host.getBoundingClientRect();
  return { found: r.width >= 50 || !!(inp && inp.value), x: r.x, y: r.y, w: r.width, h: r.height };
})()""" % (json.dumps(WIDGET_INPUT), sel, sel)


# ---------------------------------------------------------------------------
# Chrome 定位（Windows / macOS / Linux）
# ---------------------------------------------------------------------------

def find_chrome(override: str | None = None) -> str | None:
    env = override or os.environ.get("CHROME_PATH") or os.environ.get("TURNSTILE_CHROME_PATH")
    if env and Path(env).is_file():
        return env
    candidates: list[Path] = []
    home = Path.home()
    if sys.platform == "darwin":
        candidates += [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    if sys.platform == "win32" or Path("/mnt/c/Windows").exists():
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        if local:
            candidates.append(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe")
        candidates.append(Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"))
    for name in ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def ensure_display() -> None:
    """Linux 无桌面时自动套 xvfb-run（Turnstile 必须 headed Chrome）"""
    if not sys.platform.startswith("linux") or os.environ.get("DISPLAY"):
        return
    if shutil.which("xvfb-run"):
        os.execvp("xvfb-run", ["xvfb-run", "-a", sys.executable, *sys.argv])


# ---------------------------------------------------------------------------
# CDP 客户端（纯 Python，websocket-client；DrissionPage 自带该依赖）
# ---------------------------------------------------------------------------

def json_list(port: int) -> list[dict]:
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as resp:
        return json.loads(resp.read())


def cdp_call(ws_url: str, method: str, params: dict | None = None, timeout: float = 12.0) -> dict:
    """向某个 CDP target 发一条命令并等结果（事件消息直接跳过）"""
    from websocket import create_connection

    ws = create_connection(ws_url, timeout=timeout)
    try:
        msg_id = 1
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ws.recv()
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("id") != msg_id:
                continue
            if data.get("error"):
                raise RuntimeError(f"CDP {method}: {data['error']}")
            return data.get("result") or {}
        raise TimeoutError(f"CDP {method} timeout")
    finally:
        ws.close()


def page_eval(port: int, page_id: str, expression: str, timeout: float = 12.0):
    ws = _tab_ws(port, page_id)
    res = cdp_call(ws, "Runtime.evaluate",
                   {"expression": expression, "returnByValue": True}, timeout=timeout)
    return (res.get("result") or {}).get("value")


def click_xy(port: int, page_id: str, x: float, y: float, timeout: float = 12.0) -> None:
    ws = _tab_ws(port, page_id)
    seq = [("mouseMoved", 0, "none", 0), ("mousePressed", 1, "left", 1), ("mouseReleased", 0, "left", 1)]
    for type_, buttons, button, clicks in seq:
        cdp_call(ws, "Input.dispatchMouseEvent",
                 {"type": type_, "x": x, "y": y, "button": button, "buttons": buttons,
                  "clickCount": clicks, "pointerType": "mouse"}, timeout=timeout)
        time.sleep(0.016)


def _tab_ws(port: int, target_id: str) -> str:
    for tab in json_list(port):
        if tab.get("id") == target_id and tab.get("webSocketDebuggerUrl"):
            return str(tab["webSocketDebuggerUrl"])
    raise RuntimeError(f"target {target_id} not on CDP port {port}")


def cf_iframes(port: int, page_id: str) -> list[dict]:
    """CF challenge iframe 只存在于 CDP target 列表，主文档 DOM 里查不到"""
    out = []
    for tab in json_list(port):
        if tab.get("type") != "iframe" or tab.get("parentId") != page_id:
            continue
        url = tab.get("url") or ""
        if "challenges.cloudflare.com" in url or "turnstile" in url:
            if tab.get("webSocketDebuggerUrl"):
                out.append(tab)
    return out


def click_cf_iframe(port: int, page_id: str) -> dict:
    """点 CF iframe 里的复选框（managed challenge 时需要人机交互的那一下）"""
    frames = cf_iframes(port, page_id)
    if not frames:
        return {"ok": False, "error": "cf_iframe_not_found"}
    frame = frames[-1]
    ws = str(frame["webSocketDebuggerUrl"])
    probe = cdp_call(ws, "Runtime.evaluate", {
        "expression": "({patched:!!window.__ts_patch, inner:[innerWidth,innerHeight], href:location.href})",
        "returnByValue": True})
    info = (probe.get("result") or {}).get("value") or {}
    inner = info.get("inner") or [300, 65]
    h = float(inner[1] or 65)
    x, y = 24.0, max(h, 20.0) / 2.0
    seq = [("mouseMoved", 0, "none", 0), ("mousePressed", 1, "left", 1), ("mouseReleased", 0, "left", 1)]
    for type_, buttons, button, clicks in seq:
        cdp_call(ws, "Input.dispatchMouseEvent",
                 {"type": type_, "x": x, "y": y, "button": button, "buttons": buttons,
                  "clickCount": clicks, "pointerType": "mouse"})
        time.sleep(0.016)
    return {"ok": True, "frameId": frame.get("id"), "x": x, "y": y,
            "patched": bool(info.get("patched")), "inner": inner}


# ---------------------------------------------------------------------------
# 代理认证扩展（DrissionPage 的 set_proxy 不支持带账号密码的代理）
# ---------------------------------------------------------------------------

def create_proxy_auth_extension(host: str, port: int, username: str, password: str) -> str:
    plugin_dir = tempfile.mkdtemp(prefix="proxy_plugin_")
    manifest = {
        "version": "1.0.0", "manifest_version": 2, "name": "Proxy Auth",
        "permissions": ["proxy", "tabs", "unlimitedStorage", "storage", "<all_urls>",
                        "webRequest", "webRequestBlocking"],
        "background": {"scripts": ["background.js"]},
    }
    background_js = f"""
var config = {{ mode: "fixed_servers",
    rules: {{ singleProxy: {{ scheme: "http", host: {json.dumps(host)}, port: parseInt({int(port)}) }} }} }};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
chrome.webRequest.onAuthRequired.addListener(
    function(details) {{ return {{ authCredentials: {{ username: {json.dumps(username)},
        password: {json.dumps(password)} }} }}; }},
    {{urls: ["<all_urls>"]}}, ['blocking']
);
"""
    Path(plugin_dir, "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    Path(plugin_dir, "background.js").write_text(background_js, encoding="utf-8")
    return plugin_dir


def split_proxy(proxy: str | None):
    """http://user:pass@host:port -> (host, port, user, pass)；无账号密码时 user/pass 为 None"""
    if not proxy:
        return None
    from urllib.parse import urlparse

    p = urlparse(proxy)
    if not p.hostname:
        return None
    return (p.hostname, p.port or 80, p.username, p.password)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _open_panel(page, click_texts, wait_text) -> bool:
    """点击触发 widget 渲染（很多站点要先点开注册/登录面板）；未配置 click_texts 视为页面直接渲染"""
    if not click_texts:
        return True
    wait_js = (f"document.body.innerText.includes({json.dumps(wait_text)})"
               if wait_text else "true")
    for text in click_texts:
        for el in page.eles(f"text:{text}"):
            try:
                if el.states and not el.states.is_displayed:
                    continue
                el.click()
                for _ in range(12):
                    time.sleep(0.5)
                    if page.run_js(wait_js, as_expr=True):
                        return True
            except Exception:
                continue
    return False


def solve(url: str, timeout_s: float = 60.0, proxy: str | None = None,
          click_texts=(), wait_text: str | None = None,
          host_selector: str = ".cf-turnstile, [data-sitekey]",
          chrome_path: str | None = None) -> dict:
    """过一次 Turnstile，返回 {ok, token, ua, cf_clearance, elapsed_s}；失败返回 {ok: False, error}"""
    try:
        from DrissionPage import Chromium, ChromiumOptions
    except ImportError:
        return {"ok": False, "error": "DrissionPage 未安装；pip install DrissionPage"}

    if not (PATCH_DIR / "manifest.json").is_file():
        return {"ok": False, "error": f"补丁扩展目录缺失: {PATCH_DIR}（应与本脚本同目录）"}
    chrome = find_chrome(chrome_path)
    if not chrome:
        return {"ok": False, "error": "找不到 Chrome；用 --chrome 指定或设置 CHROME_PATH"}

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    profile = tempfile.mkdtemp(prefix="cf-turnstile-")
    co = ChromiumOptions()
    co.set_paths(browser_path=chrome, local_port=port,
                 address=f"127.0.0.1:{port}", user_data_path=profile)
    co.add_extension(str(PATCH_DIR))
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--window-size=1920,1080")

    # 代理：与业务请求会话保持同一出口 IP（cf_clearance 与出口 IP 绑定）
    parts = split_proxy(proxy)
    if parts:
        host, pport, user, password = parts
        if user and password:
            co.add_extension(create_proxy_auth_extension(host, pport, user, password))
        else:
            co.set_proxy(f"http://{host}:{pport}")

    host_js = build_host_js(host_selector)
    browser = None
    t0 = time.time()
    try:
        browser = Chromium(co)
        page = browser.get_tabs()[-1]
        page.get(url, timeout=30)
        time.sleep(2.5)

        # 1) 点开注册/登录面板（页面有多个同名按钮时逐个试）
        if not _open_panel(page, click_texts, wait_text):
            return {"ok": False, "error": f"点击 {list(click_texts)} 后面板没打开",
                    "elapsed_s": round(time.time() - t0, 2)}

        # 2) 等 widget 挂载（隐藏 input 出现）
        mounted = False
        for _ in range(20):
            time.sleep(0.5)
            if page.run_js(f"document.querySelectorAll({json.dumps(WIDGET_INPUT)}).length", as_expr=True):
                mounted = True
                break
        if not mounted:
            return {"ok": False, "error": "widget 没挂载", "elapsed_s": round(time.time() - t0, 2)}

        # 3) 找到页面 target，之后走 CDP
        page_id = None
        for tab in json_list(port):
            if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                page_id = tab.get("id")
        if not page_id:
            return {"ok": False, "error": "CDP 端口上找不到页面 target"}

        # 4) 轮询：CDP 点 CF iframe + 点页面 widget 区域 + 读 token
        deadline = time.time() + timeout_s
        last_click = 0.0
        token = ""
        while time.time() < deadline:
            try:
                token = str(page_eval(port, page_id, TOKEN_JS) or "")
            except Exception:
                token = ""
            if len(token) > 20:
                break
            if time.time() - last_click >= 2.5:
                try:
                    click_cf_iframe(port, page_id)
                except Exception:
                    pass
                try:
                    box = page_eval(port, page_id, host_js) or {}
                    if box.get("found"):
                        x = float(box["x"]) + 28.0
                        y = float(box["y"]) + float(box.get("h") or 65) / 2.0
                        click_xy(port, page_id, x, y)
                except Exception:
                    pass
                last_click = time.time()
            time.sleep(0.4)

        elapsed = round(time.time() - t0, 2)
        if len(token) <= 20:
            return {"ok": False, "error": "超时没拿到 token", "elapsed_s": elapsed}

        # 5) 取浏览器 UA 和 cf_clearance（业务请求与浏览器同 UA；clearance 与出口 IP 绑定）
        ua = ""
        cf_clearance = ""
        try:
            ua = page.user_agent or ""
        except Exception:
            pass
        try:
            for c in page.cookies():
                if c.get("name") == "cf_clearance" and len(str(c.get("value") or "")) > 20:
                    cf_clearance = str(c["value"])
        except Exception:
            pass
        return {"ok": True, "token": token, "tokenLen": len(token), "ua": ua,
                "cf_clearance": cf_clearance, "elapsed_s": elapsed}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="通用 Cloudflare Turnstile 过盾求解器（自包含）")
    ap.add_argument("--url", required=True, help="widget 所在页面 URL")
    ap.add_argument("--timeout", type=float, default=60.0, help="单次过盾最长等待秒数")
    ap.add_argument("--proxy", default=None, help="http://user:pass@host:port，与业务请求同出口 IP")
    ap.add_argument("--click-text", action="append", dest="click_texts", default=None,
                    help="先点击该文本按钮 widget 才渲染，可重复传；页面直接渲染 widget 则不用传")
    ap.add_argument("--wait-text", default=None, help="点击后等待页面出现的文本（确认面板已打开）")
    ap.add_argument("--host-selector", default=".cf-turnstile, [data-sitekey]",
                    help="widget 宿主元素 CSS 选择器（坐标点击兜底定位用）")
    ap.add_argument("--chrome", default=None, help="Chrome/Chromium 路径（默认自动探测，也可用 CHROME_PATH）")
    args = ap.parse_args()
    result = solve(url=args.url, timeout_s=args.timeout, proxy=args.proxy,
                   click_texts=tuple(args.click_texts or ()), wait_text=args.wait_text,
                   host_selector=args.host_selector, chrome_path=args.chrome)
    return emit(result.get("ok", False),
                **{k: v for k, v in result.items() if k != "ok"})


if __name__ == "__main__":
    ensure_display()
    raise SystemExit(main())
