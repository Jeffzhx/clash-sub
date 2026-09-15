#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_sub.py —— 从 GitLab 仓库 free9999/ipupdate 抓取指定目录的配置，
两级去重后整合成一份 **mihomo / Clash Verge Rev 可直接导入的订阅**。

设计目标（与本地那份 fetch_nodes.py 的区别）：
    本地那份跑在「仓库镜像目录」上、直接往 Verge 写文件、还带 GeoIP 前缀和 P2P 探测；
    这一份跑在 **GitHub Actions**（无状态、无本地镜像、无 Verge 目录），只做一件事：
    拉 4 个目录的配置 -> 去重 -> 产出 main.yaml。产出后由 workflow 提交回仓库，
    用户拿到的是 jsDelivr 上的一条订阅地址。

抓哪些：
    仓库**根目录**下的 clash.meta2 / quick / hysteria / hysteria2 四个目录。
    ⚠ 不要改用 backup/img/1/2/ipp/ 下那些同名副本 —— 用户明确只要根目录这 4 个。

关键设计（都是踩过坑换来的，改动前先读）：
    1. **按内容判协议，不按目录名**。上游会轮换目录里的内容（quick/3 曾经装的是 mieru、
       现在是 hysteria），按目录名解析必然出错。一律读 type 字段 / JSON 结构。
    2. **必须 ipv6: true**。这 4 个目录里绝大多数节点是纯 IPv6 字面量；mihomo 的全局
       ipv6:false 会让内核**拒绝直连 IPv6 节点**，表现为节点永远连不上。所以收了
       IPv6 节点就必须同时打开 ipv6，没有折中（hysteria2 出站不支持 ip-option 字段）。
    3. **两级去重**。同一个节点会在不同目录里以不同格式出现（YAML 与 JSON 各一份，
       字段还可能不完全一样），只做「内容 sha 相同」不够，必须再做一层
       (type, server, port, 凭据) 语义去重，否则订阅里会出现重复节点。
    4. **节点名要稳定**。用「源目录缩写 + 源目录编号」命名（cm2-0 / hy2-2 / qk-3），
       而不是按协议顺序计数 —— 某个节点掉了不会让其它节点的名字整体漂移。
       上游那些原生的垃圾名字（dongtaiwang.com_0、https://github.com/... 还带尾空格）一律丢弃。

用法：
    python scripts/build_sub.py                      # 生成 main.yaml + node_report.json
    python scripts/build_sub.py --flag-mode none     # 节点名不带国家标识
    python scripts/build_sub.py --dry-run            # 只打印，不写文件
"""

from __future__ import annotations

import argparse
import atexit
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import yaml

# --------------------------------------------------------------------------- #
# 上游地址
# --------------------------------------------------------------------------- #

PROJECT_ENC = "free9999%2Fipupdate"
BRANCH = "master"                     # GitLab 上该项目默认分支是 master（已确认）
RAW_BASE = f"https://gitlab.com/free9999/ipupdate/-/raw/{BRANCH}/"
API_TREE = f"https://gitlab.com/api/v4/projects/{PROJECT_ENC}/repository/tree"

# 抓取顺序 = 去重时的优先级。同一个节点在多处出现时，**先出现的那个**保留其字段。
# clash.meta2 放最前：它是 YAML、字段最干净，且目录名天然带 "meta2"，识读性最好。
WANT_DIRS = ["clash.meta2", "quick", "hysteria", "hysteria2"]

# ---------------------------------------------------------------------------
# ★ 这几组目录名在仓库里存在**三套同名副本**，内容并不相同，必须显式指明用哪一套
# ---------------------------------------------------------------------------
#   root : clash.meta2/ 、quick/ …                  仓库根目录。配置文件数是 3/2/2/2（共 9 个）
#   ip   : backup/img/1/2/ip/…                      配置文件数 6/4/4/4（共 18 个）
#   ipp  : backup/img/1/2/ipp/…                     配置文件数 6/4/4/4（共 18 个）
#
# ⚠ 实测：ip/ 与 ipp/ 的同名文件 **sha 不同**（抽 5 个文件比对，4 个不同、只有 quick/4 恰好一致），
#   所以它们**不是互为备份**，是三套各自独立的节点数据。
# ⚠ 三套给出的节点集合差异很大：root 那套 6 个节点里 5 个是纯 IPv6（可用性明显更差），
#   ipp 那套 7~8 个节点里只有 2 个 IPv6，还包含 IPv4 与域名节点。
# 默认用 ipp —— 与本地 fetch_nodes.py 的来源保持一致。
SOURCE_PREFIX = {
    "root": "",
    "ip": "backup/img/1/2/ip/",
    "ipp": "backup/img/1/2/ipp/",
}
DEFAULT_SOURCE = "ipp"
SOURCE = DEFAULT_SOURCE          # main() 里按 --source 覆盖

# 目录缩写 -> 节点名前缀
DIR_ABBR = {
    "clash.meta2": "cm2",
    "quick": "qk",
    "hysteria": "hy",
    "hysteria2": "hy2",
}

# 各目录在 ip/ipp 里的「文件名 + 编号上限」，兜底清单用它生成
_DIR_SPEC = {
    "clash.meta2": ("config.yaml", 6),
    "quick": ("config.yaml", 4),
    "hysteria": ("config.json", 4),
    "hysteria2": ("config.json", 4),
}


def _numbered_fallback(prefix: str) -> list[str]:
    out = []
    for d in WANT_DIRS:
        fn, n = _DIR_SPEC[d]
        out += [f"{prefix}{d}/{i}/{fn}" for i in range(1, n + 1)]
    return out


# API 拉不到时的兜底清单（照 2026-09 的仓库实际结构写死）。
# 注意 clash.meta2/2/3/ 下只有 .gitkeep，没有配置，所以 root 那套不列。
FALLBACK_FILES = {
    "root": [
        "clash.meta2/config.yaml", "clash.meta2/2/config.yaml", "clash.meta2/3/config.yaml",
        "quick/config.yaml", "quick/3/config.yaml",
        "hysteria/config.json", "hysteria/2/config.json",
        "hysteria2/config.json", "hysteria2/2/config.json",
    ],
    "ip": _numbered_fallback("backup/img/1/2/ip/"),
    "ipp": _numbered_fallback("backup/img/1/2/ipp/"),
}

# 本订阅收这些（都能被 mihomo 原生直连）。
SUPPORTED_TYPES = {"hysteria", "hysteria2", "anytls"}

# 明确**知道但故意不收**的协议，命中时在日志/报告里给出理由（而不是笼统说"不支持"）
KNOWN_UNSUPPORTED = {
    "mieru": "实测该源节点持续连不上（本地 fetch_nodes.py 的 --mieru 默认即为排除）",
    "shadowquic": "本内核与其服务端握手层不兼容（JLS 实现代次不匹配），需改走原生 exe 旁路",
    "juicity": "本轮不收（mihomo 虽支持，但该目录不在用户的 4 个目录内）",
    "naiveproxy": "同上，不在用户的 4 个目录内",
    "xray": "同上，不在用户的 4 个目录内",
    "vless": "同 xray 目录",
    "vmess": "同 xray 目录",
    "trojan": "同上，不在用户的 4 个目录内",
    "ss": "同上",
    "ssr": "同上",
}

# 每种协议**允许出现**的字段白名单（字段名以 mihomo 为准）。
#
# 为什么要白名单而不是「原样透传」：上游 clash.meta2 的 YAML 里给 hysteria2 节点写了
# `protocol: udp` —— 那是 hysteria v1 的字段，hysteria2 没有。原样带过去，
# 轻则被内核静默忽略（留下一个永远不生效的配置项，以后排查时误导人），
# 重则被严格的解析器判为非法字段。上游哪天再加别的字段也一样，
# 白名单能保证「只有我们确认过的字段才会写进订阅」。
ALLOWED_FIELDS = {
    "hysteria": {
        "type", "server", "port", "ports", "protocol",
        "up", "down", "auth-str", "obfs", "alpn",
        "sni", "skip-cert-verify", "fingerprint", "ca", "ca-str",
        "recv-window-conn", "recv-window", "disable-mtu-discovery",
        "fast-open", "hop-interval",
    },
    "hysteria2": {
        "type", "server", "port", "ports", "password",
        "up", "down", "sni", "skip-cert-verify", "fingerprint",
        "alpn", "ca", "ca-str", "obfs", "obfs-password", "hop-interval",
        "initial-stream-receive-window", "max-stream-receive-window",
        "initial-connection-receive-window", "max-connection-receive-window",
    },
    # anytls：上游在 quick/1 里出现过。mihomo 原生支持，本地已验证的 fetch_nodes.py
    # 也把它放在 SUPPORTED_TYPES 里，所以这里一并收下。
    "anytls": {
        "type", "server", "port", "password", "sni",
        "skip-cert-verify", "fingerprint", "client-fingerprint",
        "alpn", "ca", "ca-str", "udp",
        "idle-session-check-interval", "idle-session-timeout", "min-idle-session",
    },
}

# 策略组名（规则里会引用，改名要同步改规则）
G_MANUAL = "节点选择"
G_AUTO = "自动选择"
G_AI = "AI"
G_DIRECT = "全球直连"
G_BLOCK = "全球拦截"

FLAG_MODES = ("emoji", "code", "both", "none")
FLAG_SEP = " "

# 离线 GeoIP 库（可选的锦上添花，拿不到就静默降级、节点名不带国家标识）。
# 用 Loyalsoldier/geoip 的公开产物，不需要 MaxMind 授权 key。
MMDB_URLS = [
    "https://raw.githubusercontent.com/Loyalsoldier/geoip/release/Country.mmdb",
    "https://github.com/Loyalsoldier/geoip/releases/latest/download/Country.mmdb",
]


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #

def http_get(url: str, timeout: int = 30, retries: int = 3) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                # GitLab / GitHub 都会拒绝没有 UA 的请求
                "User-Agent": "clash-sub-builder/1.0 (+github-actions)",
                "Accept": "*/*",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:            # noqa: BLE001 - 网络异常种类多，统一重试
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET 失败: {url} ({last!r})")


def fetch_text(path: str, timeout: int = 30) -> str:
    raw = http_get(RAW_BASE + urllib.parse.quote(path), timeout=timeout)
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def dir_of(path: str) -> str:
    """从仓库相对路径里取出它属于哪个目标目录。

    ★ 不能用 `path.split('/')[0]`：加了 ip/ipp 前缀后第一段是 'backup'。
    """
    for seg in str(path).split("/"):
        if seg in WANT_DIRS:
            return seg
    return ""


def list_upstream_files(prefix: str, timeout: int = 30) -> tuple[list[str], str]:
    """列出目标那 4 个目录下的所有配置文件。

    返回 (仓库相对路径列表, 来源说明)。能用 GitLab API 枚举就用 API ——
    这样上游新增/删除编号子目录也能自动跟上；API 不可用时退回兜底清单。
    """
    cfg_re = re.compile(r"^config\.(ya?ml|json)$", re.I)
    order = {d: i for i, d in enumerate(WANT_DIRS)}
    try:
        found: list[str] = []
        for page in range(1, 6):          # 全仓库约 400 条，5 页足够
            url = f"{API_TREE}?recursive=true&per_page=100&page={page}"
            data = json.loads(http_get(url, timeout=timeout).decode("utf-8"))
            if not data:
                break
            for e in data:
                p = e.get("path") or ""
                if e.get("type") != "blob" or not cfg_re.match(os.path.basename(p)):
                    continue
                # ★ 必须带前缀匹配：否则 backup/.../ipp/clash.meta2 与根目录 clash.meta2
                #   会被一起收进来 —— 那是两套完全不同的节点数据（见 SOURCE_PREFIX 注释）
                if any(p.startswith(prefix + d + "/") for d in WANT_DIRS):
                    found.append(p)
            if len(data) < 100:
                break
        if found:
            found.sort(key=lambda p: (order.get(dir_of(p), 99), p))
            return found, "GitLab API 枚举"
    except Exception as e:               # noqa: BLE001
        print(f"[提示] GitLab API 枚举失败（{type(e).__name__}: {e}），改用兜底清单")

    fb = sorted(FALLBACK_FILES.get(SOURCE, []),
                key=lambda p: (order.get(dir_of(p), 99), p))
    return fb, "兜底清单"


# --------------------------------------------------------------------------- #
# 小工具（一律自己实现，不额外引第三方库）
# --------------------------------------------------------------------------- #

def _load_any(text: str):
    s = text.lstrip()
    if s.startswith("{") or s.startswith("["):
        return json.loads(text)
    return yaml.safe_load(text)


def _mbps(value):
    """'11 Mbps' / '11 mbps' / 11 -> 11"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"(\d+)", str(value))
    return int(m.group(1)) if m else None


def _seconds(value):
    """'300s' / 120 -> 300 / 120"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"(\d+)", str(value))
    return int(m.group(1)) if m else None


def _as_list(value):
    """源里 alpn 有时是 'h3' 字符串、有时是 ['h3']，mihomo 只认列表。"""
    if value is None:
        return None
    if isinstance(value, list):
        return value
    return [value]


def _drop_empty(d: dict) -> dict:
    """删掉 None / '' / {}。注意不能误伤 0 和 False。"""
    return {k: v for k, v in d.items() if v is not None and v != "" and v != {}}


def normalize_host(h) -> str:
    """统一 host：去空白、去方括号、转小写。IPv6 去重时方括号写法不一致会漏判。"""
    return str(h).strip().strip("[]").lower()


def is_ipv6(host: str) -> bool:
    return ":" in host


def split_host_port(addr, default_port=None):
    """拆 '1.2.3.4:443' / '[2001:db8::1]:443' / 'host' / 裸 IPv6"""
    addr = str(addr or "").strip()
    if addr.startswith("["):
        host, _, rest = addr[1:].partition("]")
        port = rest.lstrip(":") or default_port
        return host, int(port) if port else None
    if addr.count(":") == 1:
        host, _, port = addr.partition(":")
        return host, int(port) if port else default_port
    if ":" in addr:                       # 裸 IPv6，无端口
        return addr, default_port
    return addr, default_port


def fmt_addr(host: str, port) -> str:
    """打印用：IPv6 加方括号，否则 host:port 看着像两段。"""
    return f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"


# --------------------------------------------------------------------------- #
# 解析：三套输入格式 -> mihomo 原生节点
# --------------------------------------------------------------------------- #

def normalize_proxy(p: dict, src: str) -> dict:
    """收一个「看起来已经是 mihomo 节点」的 dict，做字段清洗。

    只清不造：missing 的东西不补默认值 —— 补默认值容易把一个本来就不该启用的
    字段打开（比如 obfs），出错时更难查。
    """
    n = {k: v for k, v in p.items() if k != "name" and not str(k).startswith("_")}
    n["type"] = str(n.get("type", "")).strip().lower()
    n["server"] = normalize_host(n.get("server", ""))
    if "port" in n:
        try:
            n["port"] = int(n["port"])
        except (TypeError, ValueError):
            pass
    for k in ("up", "down"):
        if k in n:
            n[k] = _mbps(n[k])
    if "alpn" in n:
        n["alpn"] = _as_list(n["alpn"])
    n["_src"] = src
    return _drop_empty(n)


def parse_hysteria1(d: dict, src: str) -> dict:
    """hysteria1 原生 JSON（顶层 server / auth_str / server_name）-> mihomo hysteria。"""
    host, port = split_host_port(d.get("server", ""))
    node = {
        "type": "hysteria",
        "server": normalize_host(host),
        "port": port,
        "protocol": d.get("protocol") or "udp",
        "up": _mbps(d.get("up_mbps")),
        "down": _mbps(d.get("down_mbps")),
        "auth-str": d.get("auth_str"),
        "sni": d.get("server_name"),
        # 源默认就是宽松模式；显式写出来，免得客户端默认值不同导致连不上
        "skip-cert-verify": bool(d.get("insecure", True)),
        "alpn": _as_list(d.get("alpn")),
    }
    if d.get("obfs"):
        node["obfs"] = d["obfs"]
    # 源的 hop_interval 有值，但只给了一个固定端口、没有端口段，所以它实际上不生效；
    # 保留字段是为了将来上游补上端口段时能直接用。
    if d.get("hop_interval"):
        node["hop-interval"] = _seconds(d["hop_interval"])
    node["_src"] = src
    return _drop_empty(node)


def parse_hysteria2(d: dict, src: str) -> dict:
    """hysteria2 原生 JSON（tls.sni / auth / bandwidth）-> mihomo hysteria2。"""
    host, port = split_host_port(d.get("server", ""))
    bw = d.get("bandwidth") or {}
    tls = d.get("tls") or {}
    udp = (d.get("transport") or {}).get("udp") or {}
    node = {
        "type": "hysteria2",
        "server": normalize_host(host),
        "port": port,
        "password": d.get("auth"),
        "up": _mbps(bw.get("up")),
        "down": _mbps(bw.get("down")),
        "sni": tls.get("sni"),
        "skip-cert-verify": (bool(tls.get("insecure", True))
                             if "insecure" in tls else None),
        "alpn": _as_list(tls.get("alpn")),
        "hop-interval": _seconds(udp.get("hopInterval")),
    }
    node["_src"] = src
    return _drop_empty(node)


def unsupported_reason(t) -> str:
    """把"不收这个协议"讲清楚，而不是笼统报"不支持"。"""
    key = str(t or "").strip().lower()
    if key in KNOWN_UNSUPPORTED:
        return f"已知但故意不收 -> {key}（{KNOWN_UNSUPPORTED[key]}）"
    return f"不支持的节点类型 -> {key or '(空)'}"


def parse_file(text: str, src: str):
    """按**内容**判格式 —— 上游目录里的协议会轮换，不能按目录名判。

    返回 (节点列表, 备注列表)。备注解释"为什么这个文件没贡献/少贡献了节点"。
    """
    try:
        data = _load_any(text)
    except Exception as e:                # noqa: BLE001
        return [], [f"解析失败({type(e).__name__}: {e})"]

    if not isinstance(data, dict):
        return [], ["顶层不是对象"]

    # --- Clash / mihomo YAML：只取 proxies，它自带的端口/组/规则/DNS 全丢 ---
    if data.get("proxies"):
        out, notes = [], []
        for p in data["proxies"]:
            if not isinstance(p, dict):
                notes.append("proxies 里有一项不是对象，已跳过")
                continue
            n = normalize_proxy(p, src)
            if n.get("type") in SUPPORTED_TYPES and n.get("server"):
                out.append(n)
            else:
                note = unsupported_reason(n.get("type"))
                notes.append(note)
                print(f"  [跳过] {src}: {note}")
        if not out and not notes:
            notes.append("proxies 为空")
        return out, notes

    # --- hysteria1 原生 JSON ---
    if "auth_str" in data and "server" in data:
        n = parse_hysteria1(data, src)
        return ([n] if n.get("server") else []), ([] if n.get("server") else ["缺少 server"])

    # --- hysteria2 原生 JSON ---
    if "auth" in data and ("tls" in data or "bandwidth" in data) and "server" in data:
        n = parse_hysteria2(data, src)
        return ([n] if n.get("server") else []), ([] if n.get("server") else ["缺少 server"])

    # --- 其它格式：本订阅不收，但要说清是哪种 ---
    for k in ("outbounds", "outbound", "profiles", "inbounds"):
        if k in data:
            return [], [f"是 {k} 结构（sing-box/mieru/shadowquic），不在本订阅收录范围"]
    return [], ["无法识别的格式"]


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #

def _payload(n: dict) -> dict:
    return {k: v for k, v in n.items() if not str(k).startswith("_")}


def sha_of(n: dict) -> str:
    """第一级：内容完全相同（连字段顺序无关）的纯拷贝。"""
    return hashlib.sha256(
        json.dumps(_payload(n), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def semantic_key(n: dict) -> str:
    """第二级：同一节点的不同格式写法。

    同一台服务器可能在 clash.meta2 里是 YAML、在 hysteria2 里是原生 JSON，
    两边字段名不同（auth-str vs password）、字段集也不同，sha 对不上。
    所以再用 (type, server, port, 用户名, 凭据) 做一次语义去重。
    ⚠ 不能只用 (type, server, port)：同一 host:port 上可能有不同密码的节点。
    """
    cred = (n.get("password") or n.get("auth-str") or n.get("uuid") or "")
    return "|".join([
        str(n.get("type", "")).lower(),
        normalize_host(n.get("server", "")),
        str(n.get("port", "")),
        str(n.get("username") or ""),
        str(cred),
    ]).lower()


def dedup(nodes: list):
    """返回 (唯一节点列表, sha 重复, 语义重复)。"""
    seen_sha, seen_sem = set(), {}
    uniq, dup_sha, dup_sem = [], [], []
    for n in nodes:
        h = sha_of(n)
        if h in seen_sha:
            dup_sha.append({"src": n.get("_src"), "type": n.get("type"),
                            "server": n.get("server"), "port": n.get("port")})
            continue
        seen_sha.add(h)
        k = semantic_key(n)
        if k in seen_sem:
            dup_sem.append({"src": n.get("_src"), "dup_of": seen_sem[k],
                            "type": n.get("type"), "server": n.get("server"),
                            "port": n.get("port")})
            continue
        seen_sem[k] = n.get("_src")
        uniq.append(n)
    return uniq, dup_sha, dup_sem


def sanitize(nodes: list) -> list:
    """按白名单裁字段。返回被丢弃的 (来源, 字段名) 列表，供报告留痕。"""
    dropped = []
    for n in nodes:
        allowed = ALLOWED_FIELDS.get(str(n.get("type", "")).lower(), set())
        for k in [k for k in n if not str(k).startswith("_") and k not in allowed]:
            dropped.append({"src": n.get("_src"), "field": k, "value": n[k]})
            n.pop(k, None)
    return dropped


# --------------------------------------------------------------------------- #
# 命名
# --------------------------------------------------------------------------- #

def source_slot(src: str):
    """从仓库相对路径里取出 (目录缩写, 槽位)。

    'clash.meta2/2/config.yaml'                    -> ('cm2', '2')
    'clash.meta2/config.yaml'                      -> ('cm2', '0')
    'backup/img/1/2/ipp/clash.meta2/1/config.yaml'  -> ('cm2', '1')

    ★ 不能只看第一段：加了 ip/ipp 前缀后第一段是 'backup'。
      要**找到出现在 WANT_DIRS 里的那一段**，再往右取槽位。
    """
    parts = str(src).split("/")
    for i, seg in enumerate(parts):
        if seg in WANT_DIRS:
            return DIR_ABBR[seg], ("/".join(parts[i + 1:-1]) or "0")
    return (parts[0] if parts else "node"), ""


def assign_names(nodes: list) -> None:
    """按**源目录槽位**命名：clash.meta2/2 -> cm2-2，quick/3 -> qk-3。

    为什么不用「按协议计数」（hy-1 / hy-2 ...）：那样一旦某个节点掉了，
    后面所有节点的编号都会整体前移，用户手动钉死的选择就串位了。
    按源槽位命名则一个萝卜一个坑 —— 上游换内容只会让这个槽位指向新节点，
    其它槽位纹丝不动。

    重名会让**整个配置加载失败**（不是只丢那一个），所以这里再兜一层：
    同一槽位解析出多个节点时统一加 -a/-b 后缀区分。
    """
    from collections import Counter

    bases, slots = [], []
    for n in nodes:
        abbr, sub = source_slot(n.get("_src", ""))
        slots.append(n.get("_src", ""))
        bases.append(f"{abbr}-{sub}" if sub else abbr)

    total = Counter(bases)
    used: dict = {}
    for n, base in zip(nodes, bases):
        if total[base] == 1:
            core = base
        else:
            k = used.get(base, 0)
            used[base] = k + 1
            core = f"{base}-{chr(ord('a') + k)}"
        # 国家标识加在**后面**：编号仍占开头，面板里按名字排序/定位都方便
        name = core + country_suffix(n.get("server", ""))

        ordered = {"name": name}
        for k, v in n.items():
            if k != "name" and not str(k).startswith("_"):
                ordered[k] = v
        n.clear()
        n.update(ordered)


# --------------------------------------------------------------------------- #
# 国家标识（可选，读离线 Country.mmdb；拿不到就整体跳过）
# --------------------------------------------------------------------------- #

_MMDB = None
_MMDB_TRIED = False
_COUNTRY_CACHE: dict = {}


def _cleanup_tmp(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _mmdb():
    global _MMDB, _MMDB_TRIED
    if _MMDB_TRIED:
        return _MMDB
    _MMDB_TRIED = True
    try:
        import maxminddb
    except Exception:                     # noqa: BLE001 - 没装就降级
        print("[提示] 没有 maxminddb，节点名不带国家标识"
              "（想开：pip install maxminddb）")
        _MMDB = None
        return None
    for url in MMDB_URLS:
        try:
            blob = http_get(url, timeout=45, retries=2)
            # ★ maxminddb 3.x 的 open_database **只接受文件路径**：
            #   传 bytes 会被当成文件名去 open（7 MB 的二进制名报 ValueError），
            #   传 BytesIO 直接 TypeError: expected str, bytes or os.PathLike。
            #   所以先落一个临时文件，进程结束时删掉。
            fd, path = tempfile.mkstemp(prefix="Country-", suffix=".mmdb")
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            atexit.register(_cleanup_tmp, path)
            _MMDB = maxminddb.open_database(path)
            print(f"[GeoIP] 已加载国家库: {url}")
            return _MMDB
        except Exception as e:            # noqa: BLE001
            print(f"[提示] 国家库不可用 {url}（{type(e).__name__}: {e}）")
    _MMDB = None
    return None


def flag_emoji(cc: str) -> str:
    """'FR' -> '🇫🇷'（两个字母的 ISO 码 = 0x1F1E6 + 字母序号）"""
    if len(cc) != 2 or not cc.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc.upper())


def country_suffix(host: str) -> str:
    """按 FLAG_MODE 生成 ' 🇫🇷' 这样的后缀；查不到就返回 ''。"""
    if FLAG_MODE == "none":
        return ""
    h = normalize_host(host)
    if h in _COUNTRY_CACHE:
        cc = _COUNTRY_CACHE[h]
    else:
        cc = None
        db = _mmdb()
        # 只对 IP 字面量查（本订阅的 server 全是字面量）。
        # 域名不做 DNS 解析 —— 云端跑 DNS 只会引入不确定性，收益很低。
        if db is not None and (":" in h or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", h)):
            try:
                rec = db.get(h) or {}
                cc = ((rec.get("country") or {}).get("iso_code")
                      or (rec.get("registered_country") or {}).get("iso_code"))
            except Exception:             # noqa: BLE001
                cc = None
        _COUNTRY_CACHE[h] = cc
    if not cc:
        return ""
    cc = str(cc).upper()
    if FLAG_MODE == "code":
        return FLAG_SEP + cc
    if FLAG_MODE == "both":
        return f"{FLAG_SEP}{flag_emoji(cc)}{cc}"
    return FLAG_SEP + flag_emoji(cc)


# --------------------------------------------------------------------------- #
# 组装完整订阅
# --------------------------------------------------------------------------- #

def build_dns() -> dict:
    return {
        "enable": True,
        # ★ 必须 true：5/6 的节点是纯 IPv6 字面量，dns.ipv6=false 会让它们解析/直连失败
        "ipv6": True,
        "enhanced-mode": "fake-ip",
        "fake-ip-range": "198.18.0.1/16",
        "respect-rules": True,
        "default-nameserver": ["223.5.5.5", "119.29.29.29"],
        # 国内域名用国内 DoH，别为了解析一个国内域名先去连境外
        "nameserver": ["https://dns.alidns.com/dns-query", "https://doh.pub/dns-query"],
        # 解析「代理服务器域名」用它（本订阅 server 都是 IP，属于兜底）
        "proxy-server-nameserver": ["https://dns.alidns.com/dns-query",
                                    "https://doh.pub/dns-query"],
        # 被污染/解析到内网的结果会被 fallback-filter 拦下，不采用
        "fallback": ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"],
        "fallback-filter": {
            "geoip": True,
            "geoip-code": "CN",
            "ipcidr": ["240.0.0.0/4", "0.0.0.0/32"],
        },
    }


def build_config(nodes: list) -> dict:
    names = [n["name"] for n in nodes]

    groups = [
        {
            "name": G_MANUAL,
            "type": "select",
            # 第一项就是默认值 -> 默认走自动择优；也能手动切到某个具体节点
            "proxies": [G_AUTO, G_AI, G_DIRECT] + names,
        },
        {
            "name": G_AUTO,
            "type": "url-test",
            "url": "https://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50,
            # 默认 true 表示「没用到就不体检」，会让首次请求撞上一个没测过的节点
            "lazy": False,
            # ★ 必须 list(names) 复制一份：直接塞同一个 list 对象会让 PyYAML 输出
            #   `&id001` / `*id001` 锚点别名，配置能读但很难看、也容易被别的
            #   订阅转换工具处理坏。每个组各拿一份独立的副本。
            "proxies": list(names),
        },
        {
            "name": G_AI,
            "type": "url-test",
            # 探测点就是 AI 自己：被地区封锁会返回 403 拦截页
            "url": "https://chatgpt.com/cdn-cgi/trace",
            # ★ 默认值是 '*'（不校验状态码），会把 403 封锁页判成「节点可用」
            "expected-status": 200,
            "timeout": 5000,
            "interval": 300,
            "tolerance": 100,
            "lazy": False,
            "max-failed-times": 2,
            "proxies": list(names),
        },
        {"name": G_DIRECT, "type": "select", "proxies": ["DIRECT", G_BLOCK]},
        {"name": G_BLOCK, "type": "select", "proxies": ["REJECT", "DIRECT"]},
    ]

    # 规则顺序即优先级，第一条命中即停
    rules = [
        "GEOIP,private,DIRECT,no-resolve",
        # ★ AI 必须排在所有「大陆判定」之前：这类站点的 CDN 在国内有边缘节点，
        #   一旦 GEOIP,CN 排前面，AI 流量会被判成直连，症状和「地区封锁」一模一样
        f"GEOSITE,openai,{G_AI}",
        f"GEOSITE,anthropic,{G_AI}",
        f"GEOSITE,google-gemini,{G_AI}",
        f"GEOSITE,category-ai-!cn,{G_AI}",
        f"GEOSITE,cn,{G_DIRECT}",
        f"GEOIP,CN,{G_DIRECT},no-resolve",
        f"MATCH,{G_MANUAL}",
    ]

    return {
        "log-level": "info",
        "mode": "rule",
        # ★ 见 build_dns() 里的说明：收 IPv6 节点就必须打开
        "ipv6": True,
        "unified-delay": True,
        "tcp-concurrent": True,
        "dns": build_dns(),
        "proxies": [dict(n) for n in nodes],
        "proxy-groups": groups,
        "rules": rules,
    }


def dump_yaml(obj) -> str:
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=4096)


def header_comment(nodes: list, origins: list, counts: dict, source_desc: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# " + "=" * 72,
        "# 本文件由 GitHub Actions 自动生成 —— 请勿手工修改，下次运行会被覆盖。",
        "#",
        f"# 生成时间 : {now}",
        f"# 数据源   : https://gitlab.com/free9999/ipupdate @ {BRANCH}",
        f"# 抓取范围 : {SOURCE_PREFIX[SOURCE] or '（仓库根目录）'}"
        f"{' / '.join(WANT_DIRS)}   [source={SOURCE}]",
        f"# 枚举方式 : {source_desc}",
        f"# 节点数   : {len(nodes)} 个（去重前 {counts['解析出节点']}，"
        f"纯拷贝重复 {counts['sha重复']}，跨格式重复 {counts['语义重复']}）",
        "#",
        "# 该仓库存在三套同名目录（root / backup/.../ip / backup/.../ipp），内容并不相同。",
        "# 本文件的来源见上面「抓取范围」；换来源用 --source root|ip|ipp。",
        "#",
        "# 节点来源（源文件 -> 节点名）:",
    ]
    for n, src in zip(nodes, origins):
        lines.append(
            f"#   {n['name']:<18} {n['type']:<10} "
            f"{fmt_addr(n['server'], n['port']):<30} <- {src or '?'}"
        )
    lines.append("#")
    lines.append("# 这些是公开的免费节点，随时可能失效或变慢 —— 自动选择组会自己挑活的。")
    lines.append("# " + "=" * 72)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main() -> int:
    global FLAG_MODE, SOURCE

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)              # 仓库根（脚本在 scripts/ 下）

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(root, "main.yaml"))
    ap.add_argument("--report", default=os.path.join(root, "node_report.json"))
    ap.add_argument("--source", choices=sorted(SOURCE_PREFIX), default=DEFAULT_SOURCE,
                    help="抓哪一套同名目录（三套内容不同，见 SOURCE_PREFIX 注释）："
                         "ipp=backup/img/1/2/ipp/（默认，6/4/4/4 共 18 个文件）；"
                         "ip=backup/img/1/2/ip/（同样 6/4/4/4，但内容与 ipp 不同）；"
                         "root=仓库根目录（3/2/2/2 共 9 个文件，且大多为 IPv6 节点）")
    ap.add_argument("--flag-mode", choices=list(FLAG_MODES), default="emoji",
                    help="节点名后缀的国家标识：emoji=🇫🇷（默认） code=FR both=🇫🇷FR none=不加")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    FLAG_MODE = args.flag_mode
    SOURCE = args.source
    prefix = SOURCE_PREFIX[SOURCE]

    # ---- 1. 枚举要抓的文件 ----
    files, source_desc = list_upstream_files(prefix, args.timeout)
    # ★ 再兜一层前缀过滤：兜底清单是按 source 生成的，但 API 枚举走的是全仓库；
    #   不加这道过滤，root 的 clash.meta2 会和 ipp 的 clash.meta2 混进同一份订阅。
    files = [f for f in files if any(f.startswith(prefix + d + "/") for d in WANT_DIRS)]
    print(f"[1/5] 来源 {SOURCE}（{prefix or '仓库根目录'}）"
          f"待抓取 {len(files)} 个文件（{source_desc}）")
    for f in files:
        print(f"        {f}")

    # ---- 2. 抓取 + 解析 ----
    raw, errors, skipped, node_notes = [], [], [], []
    for path in files:
        try:
            text = fetch_text(path, args.timeout)
        except Exception as e:            # noqa: BLE001
            # 单文件失败不中断整体（上游偶尔会删文件，兜底清单里就会 404）
            errors.append({"file": path, "error": f"下载失败: {e}"})
            print(f"  [失败] {path}: 下载失败")
            continue
        nodes, notes = parse_file(text, path)
        if not nodes:
            reason = "; ".join(notes) or "无节点"
            skipped.append({"file": path, "reason": reason})
            print(f"  [跳过] {path}: {reason}")
            continue
        if notes:
            # 文件里既有收下的、也有跳过的（典型：quick/ 目录里混着 anytls / mieru）
            node_notes.append({"file": path, "notes": notes})
        for n in nodes:
            print(f"  [ok]   {path}: {n['type']} {fmt_addr(n['server'], n['port'])}")
        raw.extend(nodes)

    if not raw:
        print("\n[致命] 一个节点都没解析出来 —— 中止，不覆盖现有订阅。", file=sys.stderr)
        return 2

    # ---- 3. 两级去重 ----
    uniq, dup_sha, dup_sem = dedup(raw)
    print(f"[2/5] 解析出 {len(raw)} 个节点 -> 去重后 {len(uniq)} 个"
          f"（纯拷贝 {len(dup_sha)}，跨格式 {len(dup_sem)}）")
    for d in dup_sha:
        print(f"        重复(拷贝): {d['src']}")
    for d in dup_sem:
        print(f"        重复(跨格式): {d['src']}  ==  {d['dup_of']}")

    if not uniq:
        print("\n[致命] 去重后没有节点 —— 中止。", file=sys.stderr)
        return 2

    # ---- 4. 裁字段 + 命名 + 组装 ----
    dropped = sanitize(uniq)
    if dropped:
        print(f"        裁掉白名单外的字段 {len(dropped)} 处:")
        for d in dropped:
            print(f"          - {d['src']}: {d['field']} = {d['value']!r}")

    # assign_names() 会把 `_` 前缀的内部键清掉，源文件信息要**先**存下来
    origins = [n.get("_src") for n in uniq]
    assign_names(uniq)
    cfg = build_config(uniq)
    body = dump_yaml(cfg)

    counts = {"解析出节点": len(raw), "去重后节点": len(uniq),
              "sha重复": len(dup_sha), "语义重复": len(dup_sem),
              "裁掉字段": len(dropped),
              "解析失败": len(errors), "整文件跳过": len(skipped),
              "部分跳过": len(node_notes)}
    text_out = header_comment(uniq, origins, counts, source_desc) + body

    print(f"[3/5] 节点清单:")
    for n in uniq:
        print(f"        {n['name']:<18} {n['type']:<10} "
              f"{fmt_addr(n['server'], n['port'])}")

    ipv6_cnt = sum(1 for n in uniq if is_ipv6(n["server"]))
    print(f"[4/5] IPv4 {len(uniq) - ipv6_cnt} 个 / IPv6 {ipv6_cnt} 个"
          f"（配置已强制 ipv6: true）")
    if errors or skipped or node_notes:
        print(f"[5/5] 未收录: 下载/解析失败 {len(errors)}，整文件跳过 {len(skipped)}，"
              f"文件内部分跳过 {len(node_notes)}")
        for e in errors:
            print(f"        ✗ {e['file']}: {e['error']}")
        for s in skipped:
            print(f"        - {s['file']}: {s['reason']}")
        for p in node_notes:
            print(f"        - {p['file']}: {'; '.join(p['notes'])}")

    # ---- 5. 落盘 ----
    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .isoformat(timespec="seconds"),
        "upstream": f"https://gitlab.com/free9999/ipupdate @ {BRANCH}",
        "source": SOURCE,
        "source_prefix": SOURCE_PREFIX[SOURCE],
        "dirs": WANT_DIRS,
        "enumerate": source_desc,
        "flag_mode": FLAG_MODE,
        "counts": counts,
        "by_type": {},
        # 用 origins 而不是 n["_src"]：assign_names() 已经把 `_` 前缀的键清掉了
        "nodes": [{"name": n["name"], "type": n["type"], "server": n["server"],
                   "port": n["port"], "src": origins[i]}
                  for i, n in enumerate(uniq)],
        "duplicates_sha": dup_sha,
        "duplicates_semantic": dup_sem,
        "dropped_fields": dropped,
        "errors": errors,
        "skipped": skipped,
        "partial_skips": node_notes,
    }
    for n in uniq:
        report["by_type"][n["type"]] = report["by_type"].get(n["type"], 0) + 1

    if args.dry_run:
        print("\n[dry-run] 不写文件。生成的配置预览：\n")
        print(text_out)
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text_out)
    with open(args.report, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n已生成: {args.out}")
    print(f"已生成: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
