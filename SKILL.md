---
name: douyin-video-download
description: 解析抖音分享链接，下载无水印视频和图集。当用户提供抖音分享链接（v.douyin.com、www.iesdouyin.com、www.douyin.com）时，自动解析链接获取视频/图集信息并下载。支持视频和图集两种内容类型，自动保存互动数据。
---

# 抖音视频下载

解析抖音分享链接 → 下载无水印视频 / 图集。轻量版，不含语音转文字。

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

1. **解析分享链接** — 兼容 App 分享链接和 PC 端链接
2. **提取作品信息** — 标题、作者、封面、互动数据等
3. **下载内容** — 自动识别视频或图集类型
   - 视频：自动把 `playwm` 换 `play` 拿无水印 MP4
   - 图集：下载全部图片（含 Live Photo 视频）
4. **保存互动数据** — 点赞/收藏/评论/转发数写入 `互动数据.md`

## 完整参数

```bash
python scripts/download_douyin_video.py <分享链接> \
  --output-dir <目录>     # 指定输出目录（不传则自动按标题建文件夹）
```

## 输出

- 视频：`<视频ID>.mp4`
- 图集：`<视频ID>_01.jpg`、`<视频ID>_02.jpg` ...（含 Live Photo 时附带 `_live.mp4`）
- 互动数据：`互动数据.md`
- 不传 `--output-dir` 时，文件自动按 `作者-标题前20字` 建子文件夹存放到项目根目录

## 脚本说明

- **`download_douyin_video.py`** — 主脚本：解析链接 / 取作品信息 / 下载视频或图集 / 保存互动数据

## 故障排查

1. **Windows：GBK 报错 / 被 SOCKS 代理挡**
   - 现象：emoji 触发 `UnicodeEncodeError: 'gbk'`；或连接被拒
   - 解决：按上面「Windows 快速开始」先 `unset` 代理 + `PYTHONUTF8=1 PYTHONIOENCODING=utf-8` 再跑
2. **无法解析链接 / 下载失败** — 检查链接是否有效（未删除、非私密）、网络连通、磁盘空间
3. **图集链接** — 脚本会自动识别并下载图片，不会当视频处理
4. **西瓜视频** — 暂不支持
