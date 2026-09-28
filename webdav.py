#!/usr/bin/env python3
"""蒸馏 WebDAV 服务(纯 Python,wsgidav)
文件中枢:data/ 目录,账号 distill
用途:手机文件管理器访问 inbox、Obsidian Remotely Save 同步笔记
"""
from pathlib import Path

from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.wsgidav_app import WsgiDAVApp
from cheroot.wsgi import Server as CherootServer

import yaml

BASE = Path(__file__).resolve().parent
cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
wd = cfg["webdav"]

config = {
    "provider_mapping": {"/": FilesystemProvider(str(BASE / "data"))},
    "http_authenticator": {
        "domain_controller": None,
        "accept_basic": True,
        # 只用 Basic 认证:去掉每请求一轮 digest 401 握手,Remotely Save 更快更稳
        "accept_digest": False,
    },
    "simple_dc": {
        "user_mapping": {"*": {wd["username"]: {"password": wd["password"]}}}
    },
    "verbose": 0,
    "dir_browser": {"enable": True},
}

app = WsgiDAVApp(config)
print(f"蒸馏 WebDAV: http://{wd['host']}:{wd['port']} (用户 {wd['username']})")
server = CherootServer((wd["host"], wd["port"]), app, numthreads=32)
server.start()
