# douyin-video-download — 抖音链接 → 无水印视频/图集

轻量版抖音下载工具：解析分享链接 → 下载无水印视频或图集。不含语音转文字，体积小、启动快。

## 特点

- ✅ 轻量：只需 requests + urllib3，无 torch/funasr 等重型依赖
- ✅ 自动识别：视频 / 图集自动判断，分别下载
- ✅ 无水印：视频自动替换为无水印地址
- ✅ 互动数据：自动保存点赞/收藏/评论/转发数

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
| `<分享链接>` | 必填。支持 `v.douyin.com`、`www.iesdouyin.com/share/video/`、`www.douyin.com/video/` 三种格式 |
| `--output-dir` | 自定义输出目录。不传时自动按 `作者-标题前20字` 建子文件夹存到项目根目录 |

## 输出

```
<作者-标题前20字>/
├── <视频ID>.mp4          # 视频作品：无水印视频
├── <视频ID>_01.jpg       # 图集作品：第1张图片
├── <视频ID>_02.jpg       # 图集作品：第2张图片
├── <视频ID>_01_live.mp4  # （可选）Live Photo 视频
└── 互动数据.md            # 点赞/收藏/评论/转发数据
```

## 目录结构

```
douyin-video-download/
├── README.md                      # 本文件
├── SKILL.md                       # skill 文档（面向 agent）
└── scripts/
    ├── download_douyin_video.py   # 主脚本：解析 + 下载
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
| 解析失败 | 链接被删 / 设为私密，或链接格式不对 |
| 图集下载 | 脚本自动识别，不需要额外参数 |

## 免责声明

仅供学习研究。
