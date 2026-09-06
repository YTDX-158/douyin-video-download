---
name: video-download
description: 解析抖音/快手/B站/小红书链接，下载无水印视频、图集和音频。当用户提供抖音分享链接（v.douyin.com、www.iesdouyin.com、www.douyin.com）、快手分享链接（v.kuaishou.com）、B站链接（bilibili.com/BV号）或小红书链接（xhslink.cn/xhslink.com 短链）时，自动识别平台、解析链接获取视频/图集/音频信息并下载。支持视频和图集两种内容类型，自动保存互动数据。B站默认下载音频流供转录。
---

# 短视频下载（抖音/快手/B站/小红书）

解析抖音 / 快手 / B站 / 小红书 链接 → 下载无水印视频 / 图集 / 音频。轻量版，不含语音转文字。

## 🛡️ 反爬处理

**抖音（2026-08-12 升级反爬）**：对纯脚本请求返回「验证码中间页」，直接解析会失败。脚本内置两级方案，**用户无感知**：
1. **直接解析**（`_ROUTER_DATA` / `_SSR` / `RENDER_DATA`）——抖音未拦截时最快
2. **浏览器兜底**（Playwright 真实浏览器抓网络响应）——被拦截时自动切换，无需人工操作

**快手（2026-08-18 新增）**：短链跟随重定向 → 抓页面 Relay 数据 → 提取高清 mp4 + 标题/作者/点赞/播放。无需浏览器兜底。

**B站（2026-08-28 新增，2026-09-06 改官方 wbi 接口）**：`download_bilibili.py` 走官方 API（nav 取 key → view 拿 cid → playurl 取 dash）。默认下载音频流(m4s)供转录（`--video` 下载视频流）。**清晰度**：未登录视频流最高 480p；把登录 Cookie 存 `scripts/bilibili_cookies.txt` → 1080p。

**小红书（2026-09-06 新增）**：xhslink 短链跟随重定向 → 最终 URL 自带 `xsec_token`（单页数据通行证）→ 页面 `__INITIAL_STATE__` 服务端渲染完整数据 → 视频取最高清 mp4；**图文取 `ci.xiaohongshu.com/{fileId}` 原图直链**（绕开水印/压缩层，扩展名按文件头定）。纯 requests 无浏览器兜底。互动数是中文缩写（'5万'）原样展示。

浏览器兜底需要本机装有 playwright：`pip install playwright && playwright install chromium`。

## ⚡ 快速开始

**macOS / Linux：**
```bash
scripts/venv/bin/python scripts/download_douyin_video.py "<分享链接>"
```

**Windows（必须先清代理 + 强制 UTF-8）：**
```bash
unset ALL_PROXY all_proxy HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 scripts/venv/Scripts/python.exe \
  scripts/download_douyin_video.py "<分享链接>"
```

## 工作流程

1. **识别平台** — 抖音（v.douyin.com / iesdouyin.com / douyin.com）、快手（v.kuaishou.com）、B站（bilibili.com / BV 号）或 小红书（xhslink.cn / xhslink.com 短链）
2. **提取作品信息** — 标题、作者、封面、互动数据等
3. **下载内容** — 自动识别视频或图集类型
   - 抖音视频：自动把 `playwm` 换 `play` 拿无水印 MP4
   - 快手：直接取 `photo-video-mz` 高清源
   - 小红书：短链带 xsec_token 直取最高清 mp4（图集走 imageList）
   - 图集：下载全部图片（含 Live Photo 视频）
   - B站：默认下载音频流 m4s（`--video` 下载视频流）
4. **保存互动数据** — 点赞/收藏/评论/转发数写入 `互动数据.md`（快手仅点赞/播放，其余标未提供）；B站写入 `信息.md`

## 完整参数

```bash
python scripts/download_douyin_video.py <分享链接> \
  --output-dir <目录>     # 指定输出目录（不传则自动按标题建文件夹）
```

## 输出

- 视频：`<视频ID>.mp4`
- 图集：`<视频ID>_01.jpg`、`<视频ID>_02.jpg` ...（含 Live Photo 时附带 `_live.mp4`）
- 互动数据：`互动数据.md`
- 不传 `--output-dir` 时，文件自动按 `作者-标题前20字` 建子文件夹存到平台对应素材库（抖音 → `003_资源库\抖音素材\`，快手 → `003_资源库\快手素材\`，小红书 → `003_资源库\小红书素材\`）

## 脚本说明

- **`download_douyin_video.py`** — 主脚本：识别平台 / 解析链接 / 取作品信息 / 下载视频或图集 / 保存互动数据（抖音/快手/小红书）
- **`download_bilibili.py`** — B站脚本：解析播放页 / 提取流 / 下载音频（默认）或视频（`--video`）/ 保存信息.md

## 故障排查

1. **Windows：GBK 报错 / 被 SOCKS 代理挡**
   - 现象：emoji 触发 `UnicodeEncodeError: 'gbk'`；或连接被拒
   - 解决：按上面「Windows 快速开始」先 `unset` 代理 + `PYTHONUTF8=1 PYTHONIOENCODING=utf-8` 再跑
2. **抖音解析失败提示「浏览器方案也未获取到视频数据」**
   - 原因：抖音直接解析和浏览器兜底都失败了，大概率视频已删除 / 设私密
   - 处理：人工打开链接确认视频是否还在；或可能临时风控，稍后重试
3. **快手提示「未找到视频地址」** — 视频已删除 / 私密，或临时风控稍后重试
4. **报 playwright 相关错误** — 浏览器兜底缺依赖，`pip install playwright && playwright install chromium`
5. **图集链接** — 脚本会自动识别并下载图片，不会当视频处理
6. **小红书提示「笔记数据为空 / 未解析到视频」** — 视频已删除 / 私密，或贴的是不带 token 的页面直链 → 用 App「复制链接」的 xhslink 短链
7. **西瓜视频** — 暂不支持
