# AGENTS.md — cf_shield 过盾模块用法

本目录是**自包含的 Cloudflare Turnstile 过盾模块**，可整体拷到其他项目集成。

## 它解决什么

带 Turnstile 的站点通常有两层门，`cf_shield` 负责第 2 层，第 1 层由业务代码自己负责：

1. **CF WAF 层**：请求必须带 `sec-fetch-*` 等浏览器同款请求头，否则直接 403 —— 业务代码照抄一份浏览器请求头即可
2. **应用层**：请求体必须带有效 `turnstile_token` → 本模块（**TTL 约 300 秒，现拿现用，不要缓存复用**）

## 目录内容

```
cf_shield/
├── AGENTS.md              # 本文档
├── cf_shield.py           # 求解器，站点差异全走 CLI 参数，本身与站点无关
└── turnstile_patch/       # CF 补丁扩展（修 MouseEvent.screenX/Y，否则 CDP 点击被识别）
```

**内部流程**：headed Chrome 打开 `--url` →（如需）点击 `--click-text` 按钮并等 `--wait-text` 出现 → 等 widget 挂载（隐藏 input 出现）→ 从 CDP target 列表找到 CF challenge iframe 并模拟点击复选框 → 页面 JS 读 token，同时取出浏览器 UA 和 `cf_clearance` cookie → 输出一行 JSON。

依赖仅：`DrissionPage` + 一个 headed Chrome/Chromium（Linux 无桌面自动套 `xvfb-run`，headless 会被 CF 识别、不行）。无需 Node。

## CLI 用法

```bash
python cf_shield/cf_shield.py --url https://站点/account --timeout 60 \
    [--proxy http://user:pass@host:port] [--click-text "Sign up"] [--wait-text "选择用户名"] \
    [--host-selector ".cf-turnstile, [data-sitekey]"] [--chrome /path/to/chrome]
```

| 参数 | 说明 | 换站点时 |
|---|---|---|
| `--url` | widget 挂载的页面 | 必改 |
| `--click-text` | 需先点某按钮 widget 才渲染（可重复传）；页面直接渲染则不用传 | 按目标站点 UI |
| `--wait-text` | 点击后等待出现的文本，确认面板已打开 | 按目标站点 UI |
| `--host-selector` | widget 宿主元素 CSS 选择器（坐标点击兜底定位） | 默认通常够用 |
| `--proxy` | 与业务请求同一出口 IP | 按代理池 |
| `--timeout` | 单次过盾最长等待秒数（起浏览器 + 过质询，实测约 17~30s） | |

**输出协议**（stdout 一行 JSON，调用方解析带 `"ok"` 的最后一行）：

```json
{"ok": true, "token": "...", "tokenLen": 816, "ua": "...", "cf_clearance": "...", "elapsed_s": 17.1}
{"ok": false, "error": "..."}
```

## 模块用法

把 `cf_shield/` 放到项目根目录时可直接 import（其他位置按实际路径调整）：

```python
from cf_shield import solve
r = solve(url="https://站点/account", timeout_s=60, proxy=None,
          click_texts=("Sign up",), wait_text="选择用户名")
# r = {"ok": True, "token": ..., "ua": ..., "cf_clearance": ..., ...}
```

注意：模块方式要求当前 Python 环境装了 DrissionPage；若业务跑在另一个虚拟环境，用下面的 CLI 方式更省心（可指定任意 Python 解释器）。

## 集成示例（子进程方式，无需把 DrissionPage 装进业务环境）

```python
import json, subprocess

proxy = "http://user:pass@host:port"  # 没有就传 None

proc = subprocess.run(
    ["python", "cf_shield/cf_shield.py",
     "--url", "https://站点/account",
     "--click-text", "Sign up", "--wait-text", "选择用户名",
     "--timeout", "60"] + (["--proxy", proxy] if proxy else []),
    capture_output=True, text=True, timeout=120,
)
# 解析 stdout 最后一行 JSON
line = [l.strip() for l in proc.stdout.splitlines()
        if l.strip().startswith("{") and '"ok"' in l][-1]
shield = json.loads(line)
assert shield["ok"], shield.get("error")

# 注入业务请求：UA → 请求头；cf_clearance → Cookie；token → 业务 body
import requests
s = requests.Session()
s.headers.update({
    "user-agent": shield["ua"],                 # 必须用求解器的 UA
    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
})
if shield["cf_clearance"]:
    s.cookies.set("cf_clearance", shield["cf_clearance"], domain="站点域名")
s.proxies.update({"http": proxy, "https": proxy})   # 与求解浏览器同一出口 IP

s.post("https://站点/api/signup", json={"turnstile_token": shield["token"], ...})
```

## 硬约束（改时别破坏）

- **token TTL ~300s**：每次现拿现用，绝不缓存复用。
- **同一出口 IP**：求解浏览器和 requests 会话必须走同一个代理，否则 token / cf_clearance 校验不稳。
- **三者一致**：cf_clearance 与「求解浏览器 UA + 出口 IP」绑定，业务请求必须复用求解器的 UA。
- **必须 headed Chrome**：CF 对 headless 识别很稳；服务器无桌面靠 `xvfb-run`（求解器自动检测 `DISPLAY`）。
- **widget 常不随页面加载渲染**：需先点开注册/登录面板才挂载（`--click-text` 存在的原因）。
- **CF challenge iframe 不在主文档 DOM 里**（`querySelectorAll` 找不到），只能从 CDP target 列表找到并点击。
- **带账号密码的代理**：DrissionPage 的 `set_proxy` 不支持，求解器会自动生成 MV2 代理认证扩展。
