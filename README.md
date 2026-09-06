# 短视频下载 — 抖音/快手/B站/小红书 → 无水印视频/图集

抖音 + 快手 + B站 + 小红书 四平台下载工具（原 douyin-video-download，2026-08-18 升级快手，2026-08-28 新增 B站，2026-09-06 新增小红书）。
解析分享链接 → 下载无水印视频或图集。不含语音转文字，体积小、启动快。

## 特点

- ✅ 轻量：核心只需 requests + urllib3，无 torch/funasr 等重型依赖
- ✅ 四平台：自动识别抖音（v.douyin.com / iesdouyin.com / douyin.com）、快手（v.kuaishou.com）、B站（bilibili.com / BV 号）、小红书（xhslink.cn / xhslink.com / xiaohongshu.com）
- ✅ 反爬自动兜底：抖音拦截脚本时自动用 Playwright 真实浏览器抓数据（2026-08-12 起抖音升级反爬，此兜底已必要）
- ✅ 自动识别：视频 / 图集自动判断，分别下载
- ✅ 无水印：抖音视频自动替换为无水印地址；快手直接取高清源
- ✅ 互动数据：自动保存点赞/收藏/评论/转发（快手网页仅暴露点赞/播放）

## 工作方式

**抖音**（两级方案）：抖音反爬升级后，纯脚本请求会被弹「验证码中间页」。脚本内置两级方案，自动切换、无感知：
1. **直接解析**（`_ROUTER_DATA` / `_SSR` / `RENDER_DATA`）——未拦截时最快
2. **浏览器兜底**（Playwright 打开抖音首页种 cookie → 进视频页 → 拦截 `/aweme/v1/web/aweme/detail/` 响应拿数据）

**快手**：短链跟随重定向 → 抓页面 Relay 数据 → 提取 `photo-video-mz` 高清 mp4 + 标题/作者/点赞/播放。快手网页版不暴露评论/收藏/转发。

**B站（2026-08-28 新增，2026-09-06 改走官方 wbi 接口）**：B站已不在页面塞播放数据，脚本改用官方 API（nav 取 wbi key → view 拿 cid → playurl 取 dash 流），默认只下载**音频流（m4s）**（语音转录用，流量极小）；`--video` 可下载视频流。无需浏览器兜底。

**B站清晰度（2026-09-06 加 cookie 支持）**：未登录视频流最高 **480p**（B站硬限制）；把 Edge 登录 B站后的 Cookie 存成 `scripts/bilibili_cookies.txt`（一行）→ 视频流升 **1080p**（更高需大会员）。SESSDATA 约半月刷新，失效重导一次。取法见脚本头注释或下方故障排查。

**小红书（2026-09-06 新增）**：xhslink 短链跟随重定向 → 完整 URL 自带 `xsec_token`（单页数据通行证）→ 页面 `window.__INITIAL_STATE__` 服务端渲染完整数据（视频源/标题/作者/互动全在）→ 视频取最高清档；**图文取 `ci.xiaohongshu.com/{fileId}` 原图直链**（实测 5.5MB 原图 vs qc 层 293KB，绕过平台水印/压缩中间层，扩展名按文件头自动定）。纯 requests 无浏览器兜底。互动数是中文缩写（'5万'）原样展示。

浏览器兜底需要本机装有 playwright：`pip install playwright && playwright install chromium`（已装则无需操作）。

## 用法

### macOS / Linux

```bash
scripts/venv/bin/python scripts/download_douyin_video.py "<分享链接>"
```

### Windows（必须先清代理 + 强制 UTF-8）

```bash
unset ALL_PROXY all_proxy HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 scripts/venv/Scripts/python.exe \
  scripts/download_douyin_video.py "<分享链接>"
```

### 参数

| 参数 | 说明 |
|---|---|
| `<分享链接>` | 必填。抖音：`v.douyin.com`、`www.iesdouyin.com/share/video/`、`www.douyin.com/video/`；快手：`v.kuaishou.com`；B站：`www.bilibili.com/video/BVxxx` 或裸 BV 号；小红书：`xhslink.cn` / `xhslink.com` 短链（App「复制链接」，最稳）|
| `--output-dir` | 自定义输出目录。不传时自动按 `作者-标题前20字` 建子文件夹存到平台对应素材库 |
| `--video` | （B站专用）下载视频流，默认只下载音频流 |

## 输出

```
抖音/快手/小红书：
<作者-标题前20字>/
├── <视频ID>.mp4          # 视频作品：无水印视频
├── <视频ID>_01.jpg       # 图集作品：第1张图片
├── <视频ID>_02.jpg       # 图集作品：第2张图片
├── <视频ID>_01_live.mp4  # （可选）Live Photo 视频
└── 互动数据.md            # 点赞/收藏/评论/转发数据

B站：
<BV号-标题前20字>/
├── <BV号>.m4s            # 音频流（--video 时为视频流）
└── 信息.md               # 标题/UP主/链接/大小
```

默认输出目录（用户约定）：抖音 → `D:\Claude_Files\003_资源库\抖音素材\`，快手 → `D:\Claude_Files\003_资源库\快手素材\`，B站 → `D:\Claude_Files\003_资源库\B站素材\`，小红书 → `D:\Claude_Files\003_资源库\小红书素材\`。

## 目录结构

```
短视频下载/
├── README.md                      # 本文件
├── SKILL.md                       # skill 文档（面向 agent）
└── scripts/
    ├── download_douyin_video.py   # 主脚本：解析 + 下载（抖音/快手）
    ├── download_bilibili.py       # B站脚本：下载音频流（默认）/ 视频流
    └── venv/                      # 虚拟环境
```

## 与 douyin-video-transcribe 的区别

| 特性 | douyin-video-download | douyin-video-transcribe |
|---|---|---|
| 下载视频 | ✅ | ✅ |
| 下载图集 | ✅ | ✅ |
| 语音转文字 | ❌ | ✅ |
| 依赖体积 | ~50MB | ~5GB+ |
| 启动速度 | 快 | 慢（加载 torch） |

只需要下载视频/图集时用本工具；需要转文字时用 transcribe 版本。

## 故障排查

| 现象 | 原因 / 解法 |
|---|---|
| 请求失败 / 连接被拒（Windows） | 没清代理，先 `unset ALL_PROXY ...` |
| `UnicodeEncodeError`（GBK） | 没加 `PYTHONUTF8=1 PYTHONIOENCODING=utf-8` |
| 抖音提示「浏览器方案也未获取到视频数据」 | 视频已删除 / 私密，或临时风控稍后重试 |
| 快手提示「未找到视频地址」 | 视频已删除 / 私密，或临时风控稍后重试 |
| B站提示「未找到视频流」 | 视频已删除 / 需登录 / 页面结构变更，人工打开链接确认 |
| 小红书提示「笔记数据为空 / 未解析到视频」 | 视频已删除 / 私密；或贴的是不带 token 的页面直链 → 用 App「复制链接」的 xhslink 短链 |
| B站 --video 只有 480p | 未放登录 cookie → 把 Edge 登录 B站后的 Cookie 存成 `scripts/bilibili_cookies.txt`（SESSDATA 约半月过期，失效重导） |
| 报 playwright 相关错误 | 缺浏览器兜底依赖，`pip install playwright && playwright install chromium` |
| 图集下载 | 脚本自动识别，不需要额外参数 |

## 免责声明

仅供学习研究。
