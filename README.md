# clash-sub

一条**自动更新**的 Clash / mihomo 订阅。每隔几天由 GitHub Actions 重新抓取、去重、整合，
你只需要在客户端里填一条订阅地址。

## 订阅地址

把下面的 `<你的GitHub用户名>` 换掉即可（`clash-sub` 是仓库名，想改名就同步改）：

| 用途 | 地址 |
| --- | --- |
| **首选**（国内可达性最好） | `https://fastly.jsdelivr.net/gh/<你的GitHub用户名>/clash-sub@main/main.yaml` |
| jsDelivr 备用节点 | `https://gcore.jsdelivr.net/gh/<你的GitHub用户名>/clash-sub@main/main.yaml` |
| jsDelivr 备用节点 | `https://testingcf.jsdelivr.net/gh/<你的GitHub用户名>/clash-sub@main/main.yaml` |
| GitHub 直连（国内常被墙） | `https://raw.githubusercontent.com/<你的GitHub用户名>/clash-sub/main/main.yaml` |

在 Clash Verge Rev 里：**订阅 → 新建 → 类型「远程」→ 把地址粘进 URL → 确定**
（也可以直接填 jsDelivr 的 `.yaml` 地址，Verge 会自己下载）。

> jsDelivr 对分支内容有 **12 小时**缓存。刚跑完构建想立刻拿到新版，可以访问一次
> `https://purge.jsdelivr.net/gh/<你的GitHub用户名>/clash-sub@main/main.yaml`
> 手动刷新缓存。

## 它做了什么

1. 从 GitLab 仓库 `free9999/ipupdate`（默认分支 `master`）抓取**根目录**下这 4 个目录的配置：
   `clash.meta2` / `quick` / `hysteria` / `hysteria2`
2. **两级去重**：
   - 一级：内容完全相同（sha256 一致）的纯拷贝；
   - 二级：同一节点的**不同格式写法** —— 同一台服务器可能在一处是 Clash YAML、
     在另一处是 hysteria 原生 JSON，字段名和字段集都不同，只靠内容比对抓不住，
     所以再用 `(协议, 服务器, 端口, 凭据)` 做一次语义去重。
3. 把三种输入格式（Clash/mihomo YAML、hysteria1 原生 JSON、hysteria2 原生 JSON）
   统一映射成 mihomo 原生节点，并按字段白名单裁剪多余字段。
4. 输出一份完整的 `main.yaml`：节点 + 策略组 + 规则 + fake-ip DNS。

节点名用「**源目录缩写 + 源目录编号**」命名（`cm2-2` / `qk-0` / `hy2-0` …）——
一个萝卜一个坑，某个节点掉了不会让其它节点的名字整体漂移，你手动钉死的选择就不会串位。
末尾的旗帜是节点 IP 的国家（离线 GeoIP 解析，拿不到就不加）。

## 目录结构

```
├── main.yaml              # 生成的订阅（不要手改，会被覆盖）
├── node_report.json       # 本次生成的明细报告（来源 / 去重 / 报错）
├── scripts/build_sub.py   # 生成器
└── .github/workflows/build.yml
```

## 常用改动

| 想改什么 | 改哪里 |
| --- | --- |
| 更新频率 | `build.yml` 里的 `cron`（每 2 天 → 每天：`0 3 * * *`；每周一：`0 3 * * 1`） |
| 抓哪些目录 | `scripts/build_sub.py` 的 `WANT_DIRS` |
| 节点名不带国家旗帜 | 生成命令加 `--flag-mode none`（或 `code` 用两字母码） |
| 立即重建 | Actions 页面 → build-subscription → Run workflow |

改完 `scripts/` 或 `.github/` 下的文件并提交，就会自动触发一次重建。

## 注意事项

- 这里的节点是**公开免费节点**，随时可能失效或变慢，不保证可用性。策略组里的
  「自动选择」（url-test）会自己挑当时最快且活着的那个。
- 配置里 `ipv6: true` 是**必须**打开的：这些节点绝大多数是纯 IPv6 地址，
  mihomo 的全局 `ipv6: false` 会让内核拒绝直连 IPv6 节点，表现为节点永远连不上。
- 「AI」策略组用 `https://chatgpt.com/cdn-cgi/trace` 且要求状态码 200 来筛节点 ——
  这些免费节点很可能一个都过不了 ChatGPT 的地区限制，过不了时该组会自动落到第一个节点。
  要稳定的 AI 出口请另用专门的配置，不要指望这份。
- 本仓库刻意**不 fork、不 clone** 上游仓库：上游有 30 个协议目录、好几百 MB，
  我们每次只按需抓这 4 个目录里的十来个配置文件。
