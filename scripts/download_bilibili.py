# -*- coding: utf-8 -*-
"""B站视频下载：链接/BV → 下载音频流(m4s)或视频流 → 存到 B站素材库

默认下载音频流（转录语音用，流量极小）；`--video` 可下载视频流。

用法：
    python download_bilibili.py "<B站链接或BV号>"
    python download_bilibili.py "<链接>" --video         # 下载视频流
    python download_bilibili.py "<链接>" --output-dir "D:/xx"   # 自定义输出目录

输出（默认落 B站素材库）：
    B站素材/[BV号]_[标题前20字]/
    ├── [BV号].m4s        # 音频流（--video 时为视频流 .m4s）
    └── 信息.md           # 标题/UP主/链接/时长/大小
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

BILI_MATERIAL_DIR = Path(r"D:\Claude_Files\003_资源库\B站素材")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}


def extract_link(text):
    """从文字+链接混合里抠出第一个 https 链接"""
    m = re.search(r'https?://[A-Za-z0-9./?=_%&:#@~-]+', text)
    return m.group(0) if m else text


def extract_bvid(text):
    m = re.search(r'(BV[0-9A-Za-z]{10})', text)
    if not m:
        raise SystemExit("❌ 链接里没找到 BV 号")
    return m.group(1)


def fetch_playinfo(bvid):
    """请求播放页 → 返回 (标题, up主, dash数据)"""
    import requests
    s = requests.Session()
    s.get("https://www.bilibili.com/", headers=HEADERS, timeout=15)  # 种 buvid cookie
    r = s.get(f"https://www.bilibili.com/video/{bvid}", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        raise SystemExit(f"❌ 播放页访问失败 HTTP {r.status_code}")

    m = re.search(r'window\.__playinfo__=(\{.*?\})</script>', r.text)
    if not m:
        raise SystemExit("❌ 未找到视频流（页面结构变了或需登录）")
    dash = json.loads(m.group(1)).get("data", {}).get("dash") or {}

    # 标题 + UP主：优先从 __INITIAL_STATE__ 拿，拿不到退回 <title>
    title, up = None, None
    ms = re.search(r'window\.__INITIAL_STATE__=(\{.*?\});\(function', r.text)
    if ms:
        try:
            st = json.loads(ms.group(1))
            vd = st.get("videoData") or {}
            title = vd.get("title")
            owner = vd.get("owner") or {}
            up = owner.get("name")
        except Exception:
            pass
    if not title:
        tm = re.search(r'<title>(.*?)</title>', r.text)
        if tm:
            title = tm.group(1).replace("_哔哩哔哩_bilibili", "").strip()

    return title or bvid, up or "未知", dash


def sanitize(name):
    """文件名安全化：去掉非法字符"""
    return re.sub(r'[\\/:*?"<>|\r\n]', '', name).strip()[:20]


def download_stream(url, out_path, referer):
    """带 Referer 下载流，返回字节数"""
    import requests
    h = dict(HEADERS)
    h["Referer"] = referer
    with requests.get(url, headers=h, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        got = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                got += len(chunk)
                if total:
                    print(f"\r  下载中 {got/1e6:.1f}/{total/1e6:.1f} MB", end="", flush=True)
    print()
    return got


def main():
    parser = argparse.ArgumentParser(description="B站视频下载（默认音频流，--video 下载视频流）")
    parser.add_argument("url", help="B站链接或BV号")
    parser.add_argument("--video", action="store_true", help="下载视频流（默认只下载音频流，转录用）")
    parser.add_argument("--output-dir", default=None, help="自定义输出目录")
    args = parser.parse_args()

    url = extract_link(args.url)
    bvid = extract_bvid(url)
    print(f"🔗 链接: {url}")
    print(f"🔎 BV号: {bvid}")

    title, up, dash = fetch_playinfo(bvid)
    audios = dash.get("audio") or []
    videos = dash.get("video") or []
    if not args.video and not audios:
        raise SystemExit("❌ 没有音频流")
    if args.video and not videos:
        raise SystemExit("❌ 没有视频流")

    # 输出目录：默认 B站素材\<BV>_<标题>
    base = Path(args.output_dir) if args.output_dir else BILI_MATERIAL_DIR
    sub = sanitize(f"{bvid}_{title}")
    out_dir = base / sub
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.video:
        v = videos[0]
        ext = ".m4s"
        size = download_stream(v["baseUrl"], out_dir / f"{bvid}{ext}", f"https://www.bilibili.com/video/{bvid}")
        kind = "视频"
    else:
        a = audios[0]
        ext = ".m4s"
        size = download_stream(a["baseUrl"], out_dir / f"{bvid}{ext}", f"https://www.bilibili.com/video/{bvid}")
        kind = "音频"

    info = f"# B站视频\n\n- 标题: {title}\n- UP主: {up}\n- BV号: {bvid}\n- 链接: https://www.bilibili.com/video/{bvid}\n- 类型: {kind}\n- 大小: {size/1e6:.2f} MB\n"
    (out_dir / "信息.md").write_text(info, encoding="utf-8")

    print(f"✅ 完成")
    print(f"   标题: {title}")
    print(f"   UP主: {up}")
    print(f"   保存: {out_dir / (bvid + ext)}")


if __name__ == "__main__":
    main()
