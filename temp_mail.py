"""temp_mail.py — 临时邮箱接码：创建临时邮箱，等待验证邮件并提取验证码

支持两个接码平台，用 --provider 选择：
  mailtm      mail.tm：需注册账号（会打印邮箱密码），域名随机选，收信快
  catchmail   catchmail.io：免注册，直接生成地址即可收信，但收信明显更慢

用法：
  python temp_mail.py                      # 默认 mailtm
  python temp_mail.py --provider catchmail
"""
from __future__ import annotations

import argparse
import html
import re
import secrets
import time

import requests

POLL_SECONDS = 3      # 每隔几秒检查一次
WAIT_SECONDS = 180    # 最多等待几秒


def print_mailbox_banner(address: str, extra_lines=()) -> None:
    """醒目打印当前临时邮箱，方便立刻往该地址发验证邮件"""
    print("=" * 48, flush=True)
    print(f"当前临时邮箱：{address}", flush=True)
    for line in extra_lines:
        print(line, flush=True)
    print("请现在往上面这个邮箱发送验证邮件，脚本正在等待收取……", flush=True)
    print("=" * 48, flush=True)
    print(f"最长等待 {WAIT_SECONDS} 秒", flush=True)


def collect_strings(value):
    """递归收集响应里的所有字符串字段（catchmail 邮件结构不固定，统一摊平）"""
    if isinstance(value, dict):
        for item in value.values():
            yield from collect_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from collect_strings(item)
    elif isinstance(value, str):
        yield value


def extract_code(content: str) -> str | None:
    """从文本里提取验证码：优先“验证码 / verification code”等关键词附近的 4–8 位数字，
    找不到关键词时退而匹配正文中的独立 4–8 位数字"""
    content = html.unescape(content)
    content = re.sub(r"<[^>]+>", " ", content)

    contextual = re.search(
        r"(?:验证码|校验码|verification\s*code|security\s*code|otp|code)"
        r"[^\d]{0,30}(\d{4,8})",
        content,
        flags=re.IGNORECASE,
    )
    if contextual:
        return contextual.group(1)

    fallback = re.search(r"(?<!\d)\d{4,8}(?!\d)", content)
    return fallback.group(0) if fallback else None


def as_list(data):
    """Mail.tm 列表接口以前返回 hydra 集合 {"hydra:member": [...]}，现在直接返回数组，两种都兼容"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("hydra:member") or []
    return []


def sender_of(value) -> str | None:
    """发件人字段可能是 dict、字符串或缺失，统一取出地址"""
    if isinstance(value, dict):
        return value.get("address") or value.get("name") or None
    if isinstance(value, str) and value:
        return value
    return None


def mailtm_content(message: dict) -> str:
    """mail.tm 邮件详情：subject/text 是字符串，html 可能是字符串或字符串列表"""
    parts = [message.get("subject") or "", message.get("text") or ""]
    html_content = message.get("html") or ""
    if isinstance(html_content, list):
        parts.extend(html_content)
    else:
        parts.append(html_content)
    return "\n".join(str(part) for part in parts)


class MailboxProvider:
    """接码平台统一接口：创建邮箱 -> 逐封返回新邮件 ->（公共轮询逻辑）提取验证码"""

    def __init__(self, session: requests.Session):
        self.session = session
        self._seen: set[str] = set()

    def create(self) -> str:
        """创建/生成一个临时邮箱，返回地址（内部会打印醒目横幅）"""
        raise NotImplementedError

    def next_new_message(self, address: str):
        """返回下一封新邮件的 (subject, sender, content)；没有新邮件返回 None"""
        raise NotImplementedError

    def wait_for_code(self, address: str):
        """轮询等待新邮件，返回 (code, subject, sender, content)；超时返回 None"""
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                message = self.next_new_message(address)
            except requests.RequestException as exc:
                print(f"请求失败：{exc}", flush=True)
                time.sleep(POLL_SECONDS)
                continue
            if message:
                subject, sender, content = message
                return extract_code(content), subject, sender, content
            time.sleep(POLL_SECONDS)
        return None


class MailTmProvider(MailboxProvider):
    """mail.tm：注册账号拿 token 再收信"""

    BASE_URL = "https://api.mail.tm"

    def __init__(self, session: requests.Session):
        super().__init__(session)
        self._token = ""

    def _api(self, method: str, path: str, **kwargs):
        response = self.session.request(
            method, f"{self.BASE_URL}{path}", timeout=20, **kwargs)
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def create(self) -> str:
        # 1) 随机选一个可用域名
        domains = [
            item["domain"]
            for item in as_list(self._api("GET", "/domains"))
            if item.get("isActive") and item.get("domain")
        ]
        if not domains:
            raise RuntimeError("Mail.tm 暂时没有返回可用域名。")
        domain = secrets.choice(domains)

        # 2) 随机用户名建号，冲突（409/422）就换一个再试
        account = None
        password = ""
        for _ in range(5):
            address = f"box{secrets.token_hex(6)}@{domain}"
            password = secrets.token_urlsafe(18)
            response = self.session.post(
                f"{self.BASE_URL}/accounts",
                json={"address": address, "password": password},
                timeout=20,
            )
            if response.status_code in (200, 201):
                account = response.json()
                break
            if response.status_code not in (409, 422):
                response.raise_for_status()
        if account is None:
            raise RuntimeError("创建邮箱失败，请稍后重试。")

        address = account["address"]

        # 3) 登录拿访问令牌
        token = self._api("POST", "/token",
                          json={"address": address, "password": password})["token"]
        self._token = token

        # 4) 只处理启动后新收到的邮件
        for item in as_list(self._api("GET", "/messages", headers=self._headers())):
            self._seen.add(str(item.get("id") or ""))

        print_mailbox_banner(address, extra_lines=[f"邮箱密码：{password}"])
        return address

    def next_new_message(self, address: str):
        for item in as_list(self._api("GET", "/messages", headers=self._headers())):
            message_id = str(item.get("id") or "")
            if not message_id or message_id in self._seen:
                continue
            self._seen.add(message_id)
            message = self._api("GET", f"/messages/{message_id}",
                                headers=self._headers())
            subject = message.get("subject") or "无主题"
            sender = sender_of(message.get("from")) or sender_of(item.get("from"))
            return subject, sender, mailtm_content(message)
        return None


class CatchMailProvider(MailboxProvider):
    """catchmail.io：免注册，直接生成地址即可收信；限速约 1 请求 / 1.1 秒。

    注意：catchmail 收信比 mail.tm 慢不少，验证邮件经常要等更久才入库，
    默认 WAIT_SECONDS=180 很可能等不到；对时效敏感的场景优先用 mailtm，
    坚持用 catchmail 时把顶部的 WAIT_SECONDS 调大（如 300~600）。
    """

    BASE_URL = "https://api.catchmail.io"

    def __init__(self, session: requests.Session):
        super().__init__(session)
        self._last_request_at = 0.0

    def _get_json(self, path: str, params: dict | None = None):
        # 官方限速：两次请求至少间隔 1.1 秒
        wait = 1.1 - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        response = self.session.get(f"{self.BASE_URL}{path}", params=params, timeout=20)
        self._last_request_at = time.monotonic()
        response.raise_for_status()
        return response.json()

    def _list(self, address: str) -> list:
        data = self._get_json("/api/v1/mailbox",
                              params={"address": address, "page": 1, "page_size": 50})
        if isinstance(data, dict):
            return data.get("messages") or []
        return data if isinstance(data, list) else []

    def create(self) -> str:
        address = f"box{secrets.token_hex(6)}@catchmail.io"
        # 只处理启动后新收到的邮件
        for item in self._list(address):
            self._seen.add(str(item.get("id") or ""))
        print_mailbox_banner(address)
        return address

    def next_new_message(self, address: str):
        for item in self._list(address):
            message_id = str(item.get("id") or "")
            if not message_id or message_id in self._seen:
                continue
            self._seen.add(message_id)
            message = self._get_json(f"/api/v1/message/{message_id}",
                                     params={"mailbox": address})
            subject = item.get("subject") or "无主题"
            sender = sender_of(item.get("from")) or sender_of(message.get("from"))
            return subject, sender, "\n".join(collect_strings(message))
        return None


PROVIDERS = {
    "mailtm": MailTmProvider,
    "catchmail": CatchMailProvider,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="临时邮箱接码：等待验证邮件并提取验证码")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="mailtm",
                        help="接码平台：mailtm（注册账号、收信快）或 "
                             "catchmail（免注册直接生成地址，但收信慢，需调大 WAIT_SECONDS），默认 mailtm")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    provider = PROVIDERS[args.provider](session)
    address = provider.create()

    result = provider.wait_for_code(address)
    if result is None:
        print("\n等待超时，暂时没有收到邮件。", flush=True)
        print("本次邮箱仍然有效；需要新邮箱时重新运行脚本即可。", flush=True)
        return

    code, subject, sender, content = result
    print("\n收到邮件", flush=True)
    print(f"发件人：{sender or '未知'}", flush=True)
    print(f"主题：{subject}", flush=True)
    if code:
        print(f"提取到的验证码：{code}", flush=True)
    else:
        print("邮件中没有识别到常见格式的数字验证码，请查看邮件正文。", flush=True)
        print(content, flush=True)


if __name__ == "__main__":
    main()
