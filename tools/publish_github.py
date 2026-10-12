"""建 GitHub 仓库（若不存在）、推送提交、打标签并发布 Release。

安全与网络约束：
* 只访问写死的 ``https://api.github.com``（禁重定向、禁其他 host）；
* 域名解析显式走公共 DNS，解析出的 IP 必须是公网地址（拒绝环回/私网/链路本地/保留地址），
  连接时用「IP + 正确 SNI/Host」，绕开本机 DNS 偶发把 github 域名污染成 127.0.0.1 的问题；
* 凭据从 git credential store 读取，不写入文件、不打印内容；
* git 子命令逐个参数传入，不经 shell。

用法：
    <MaiBot>/python-env/python.exe plugins/mai-is-mortis/tools/publish_github.py
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

API_HOST = "api.github.com"
OWNER = "erhan111119"
REPO = "mai-is-mortis"
DESCRIPTION = "MaiBot 插件：麦麦是多首的怪物。加分全部由模型判定，分数满了临时换人格、到点自动切回原人格，分数随时间衰减。"
TAG = "v1.0.2"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REMOTE_URL = f"https://github.com/{OWNER}/{REPO}.git"

PUBLIC_DNS = ["223.5.5.5", "119.29.29.29", "180.76.76.76"]
FALLBACK_IPS = {"api.github.com": ["20.205.243.168"], "github.com": ["20.205.243.166"]}

RELEASE_BODY = """**麦麦是多首的怪物。**

- `config.toml` 不再进版本库：宿主首次加载插件时会按配置模型自动生成它，WebUI 保存配置也会重写它，
  提交进去只会在保存后造成 git 冲突。改为提交模板 `config.toml.example`（同样由配置模型生成）。
- 生成工具 `tools/generate_default_config.py` 的产物改为该模板文件，并带上一段使用说明。

1.0.1：显示名改为 `mai is mortis（麦麦是多首的怪物）`；状态文件损坏不再拦插件加载；
删掉无效的「启用指令」配置项；「详细日志」补充隐私提醒。

1.0.0：模型判定加分（一次调用判完所有条件、模型只回编号）、分数满即临时换人格、到点自动切回、
分数随时间衰减；切换时只改写本次请求的人设/说话风格/规划行为风格，不写宿主配置；
`/人格` 支持查询、切换与重置。
"""


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        out += bytes([len(label)]) + label.encode("ascii")
    return out + b"\x00"


def resolve_public_a(host: str) -> List[str]:
    """用公共 DNS 查 A 记录，只返回公网 IP。"""

    query = struct.pack(">HHHHHH", secrets.randbelow(0x10000), 0x0100, 1, 0, 0, 0) + _encode_name(host)
    for server in PUBLIC_DNS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(5)
                sock.sendto(query, (server, 53))
                data, _ = sock.recvfrom(2048)
        except OSError:
            continue
        ips: List[str] = []
        try:
            ancount = struct.unpack(">H", data[6:8])[0]
            offset = 12
            while data[offset] != 0:
                offset += 1 + data[offset]
            offset += 5
            for _ in range(ancount):
                if data[offset] & 0xC0 == 0xC0:
                    offset += 2
                else:
                    while data[offset] != 0:
                        offset += 1 + data[offset]
                    offset += 1
                rtype, _, _, rdlength = struct.unpack(">HHIH", data[offset : offset + 10])
                offset += 10
                if rtype == 1 and rdlength == 4:
                    ips.append(".".join(str(b) for b in data[offset : offset + 4]))
                offset += rdlength
        except (struct.error, IndexError):
            continue
        public = [ip for ip in ips if _is_public(ip)]
        if public:
            return public
    return []


def _is_public(ip: str) -> bool:
    """拒绝环回/私网/链路本地/保留地址。"""

    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)


def resolve_host(host: str) -> str:
    """解析出可用的公网 IP；解析不到就退回已知 IP。"""

    ips = [ip for ip in resolve_public_a(host) if _is_public(ip)]
    if not ips:
        ips = [ip for ip in FALLBACK_IPS.get(host, []) if _is_public(ip)]
    if not ips:
        raise SystemExit(f"无法解析出 {host} 的公网 IP")
    return ips[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连到指定 IP，但 TLS SNI 与 Host 仍用真实域名。"""

    def __init__(self, ip: str, hostname: str, timeout: float = 40.0) -> None:
        super().__init__(ip, 443, timeout=timeout)
        self._sni = hostname

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self._sni)


def get_token() -> str:
    """从 git 凭据存储取 github.com 的 token。"""

    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        shell=False,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password=") :]
    raise SystemExit("没有取到 github.com 凭据")


def api(path: str, token: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    """请求 api.github.com 的路径（固定域名 + 公网 IP 校验，禁重定向）。"""

    if not path.startswith("/") or "://" in path or path.startswith("//"):
        raise ValueError(f"路径不合法: {path!r}")
    ip = resolve_host(API_HOST)
    if not _is_public(ip):
        raise SystemExit(f"{API_HOST} 解析到非公网地址，拒绝请求: {ip}")
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Host": API_HOST,
        "User-Agent": "mai-is-mortis",
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Connection": "close",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn = _PinnedHTTPSConnection(ip, API_HOST)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, (json.loads(raw) if raw.strip() else {})
        except json.JSONDecodeError:
            return resp.status, {"raw": raw[:200]}
    finally:
        conn.close()


def git(*args: str) -> Tuple[int, str]:
    """跑一个 git 子命令（参数逐个传入，不经 shell）。"""

    proc = subprocess.run(["git", *args], cwd=str(PLUGIN_ROOT), capture_output=True, text=True, shell=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> None:
    """建仓、推送、打标签并发布 Release。"""

    token = get_token()
    print(f"api.github.com 解析到: {resolve_host(API_HOST)}")

    status, data = api(f"/repos/{OWNER}/{REPO}", token)
    if status == 200:
        print(f"1. 仓库已存在: {data.get('html_url')}")
    else:
        status, data = api(
            "/user/repos",
            token,
            method="POST",
            payload={
                "name": REPO,
                "description": DESCRIPTION,
                "private": False,
                "has_issues": True,
                "has_wiki": False,
                "has_projects": False,
                "auto_init": False,
            },
        )
        if status not in (200, 201):
            print(f"1. 创建仓库失败 HTTP {status}: {json.dumps(data, ensure_ascii=False)[:300]}")
            sys.exit(1)
        print(f"1. 仓库已创建: {data.get('html_url')}")

    code, out = git("remote", "get-url", "origin")
    if code != 0:
        git("remote", "add", "origin", REMOTE_URL)
        print("2. 已添加 remote origin")
    else:
        print(f"2. remote origin 已存在: {out}")

    pushed = False
    for attempt in range(1, 4):
        code, out = git("-c", "http.version=HTTP/1.1", "push", "-u", "origin", "main")
        if code == 0:
            pushed = True
            print(f"3. push main: 成功（第 {attempt} 次尝试）")
            break
        print(f"3. push main 第 {attempt} 次失败: {out[:200]}")
    if not pushed:
        print("3. push 失败，请手动执行: git push -u origin main")

    code, out = git("tag", "-l", TAG)
    if not out.strip():
        git("tag", "-a", TAG, "-m", f"mai is mortis {TAG}")
        print(f"4. 已打标签 {TAG}")
    else:
        print(f"4. 标签 {TAG} 已存在")
    if pushed:
        code, out = git("-c", "http.version=HTTP/1.1", "push", "origin", TAG)
        print(f"5. push tag: {'成功' if code == 0 else '失败 ' + out[:200]}")

    status, data = api(
        f"/repos/{OWNER}/{REPO}/releases",
        token,
        method="POST",
        payload={"tag_name": TAG, "name": TAG, "body": RELEASE_BODY, "draft": False, "prerelease": False},
    )
    if status in (200, 201):
        print(f"6. Release 已发布: {data.get('html_url')}")
    else:
        print(f"6. Release 发布失败 HTTP {status}: {json.dumps(data, ensure_ascii=False)[:300]}")


if __name__ == "__main__":
    main()
