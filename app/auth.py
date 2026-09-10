import asyncio
import ipaddress
import json
import secrets
import socket
import time
from urllib.parse import urlencode, urlsplit

import keyring

from app.config import CALLBACK
from app.errors import AppError
from app.http import APIClient, response_json

# 只拦截真正的内网地址段，避免误杀代理 Fake IP
LOCAL_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),  # IPv6 ULA
]


async def validate_instance(value):
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise ValueError()
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM
        )
        if not addresses:
            raise ValueError()
        # 只拦截会打到本机或内网的地址；代理软件的 Fake IP（如 198.18.0.0/15）需要放行
        for addr_info in addresses:
            ip = ipaddress.ip_address(addr_info[4][0])
            if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                raise ValueError()
            if ip in LOCAL_NETWORKS:
                raise ValueError()
    except (ValueError, OSError):
        raise AppError("请输入可访问的 HTTPS NeoDB 公网实例地址，不包含路径或端口。") from None
    return f"https://{parsed.hostname.encode('idna').decode('ascii').lower()}"


class CredentialStore:
    """Use the OS keyring. When unavailable keep credentials in memory only."""

    def __init__(self, namespace="Bgm2NeoDB", backend=keyring):
        self.namespace = namespace
        self.backend = backend
        self.memory = {}
        self.warning = ""
        if backend is keyring:
            try:
                candidate = keyring.get_keyring()
                module = type(candidate).__module__
                if not module.startswith(
                    (
                        "keyring.backends.Windows",
                        "keyring.backends.macOS",
                        "keyring.backends.SecretService",
                        "keyring.backends.libsecret",
                        "keyring.backends.kwallet",
                    )
                ):
                    raise ValueError("Not an OS credential store")
                self.backend = candidate
            except Exception:
                self.backend = None
                self.warning = "系统凭证库不可用，连接信息仅在本次运行保留，重启后需要重新连接。"

    def get(self, key):
        if key in self.memory:
            return self.memory[key]
        try:
            raw = self.backend.get_password(self.namespace, key)
            value = json.loads(raw) if raw else None
            if value:
                self.memory[key] = value
            return value
        except Exception:
            self.warning = "系统凭证库不可用，连接信息仅在本次运行保留，重启后需要重新连接。"
            return None

    def put(self, key, value):
        self.memory[key] = value
        try:
            self.backend.set_password(self.namespace, key, json.dumps(value))
        except Exception:
            self.warning = "系统凭证库不可用，连接信息仅在本次运行保留，重启后需要重新连接。"

    def clear(self, key):
        self.memory.pop(key, None)
        if self.backend is None:
            return
        try:
            if self.backend.get_password(self.namespace, key):
                self.backend.delete_password(self.namespace, key)
        except Exception:
            raise AppError("系统凭证库暂时不可用，无法确认已移除保存的连接。") from None


class OAuth:
    def __init__(self, store, client_factory=APIClient):
        self.store = store
        self.client_factory = client_factory
        self.pending = None

    async def begin(self, instance, session):
        async with self.client_factory(instance) as api:
            app = response_json(
                await api.request(
                    "POST",
                    "/api/v1/apps",
                    data={
                        "client_name": "Bgm2NeoDB",
                        "redirect_uris": CALLBACK,
                        "scopes": "read write",
                    },
                    retry=False,
                )
            )
        if not all(isinstance(app.get(k), str) and app[k] for k in ("client_id", "client_secret")):
            raise AppError("NeoDB 未返回有效的应用授权信息。")
        state = secrets.token_urlsafe(32)
        self.pending = {
            "state": state,
            "session": session,
            "instance": instance,
            "app": app,
            "expires": time.monotonic() + 600,
        }
        return (
            instance
            + "/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": app["client_id"],
                    "redirect_uri": CALLBACK,
                    "scope": "read write",
                    "state": state,
                }
            )
        )

    async def finish(self, state, code, session):
        pending = self.pending
        if (
            not pending
            or not secrets.compare_digest(state, pending["state"])
            or session != pending["session"]
            or time.monotonic() > pending["expires"]
        ):
            raise AppError("授权回调无效或已过期，请重新连接 NeoDB。")
        self.pending = None
        async with self.client_factory(pending["instance"]) as api:
            token = response_json(
                await api.request(
                    "POST",
                    "/oauth/token",
                    data={
                        "client_id": pending["app"]["client_id"],
                        "client_secret": pending["app"]["client_secret"],
                        "code": code,
                        "redirect_uri": CALLBACK,
                        "grant_type": "authorization_code",
                    },
                    retry=False,
                )
            )
        if not isinstance(token.get("access_token"), str) or not token["access_token"]:
            raise AppError("NeoDB 未返回有效的连接凭证。")
        return {"instance": pending["instance"], "token": token["access_token"]}
