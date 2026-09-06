# -*- coding: utf-8 -*-
"""B站视频下载：链接/BV → 下载音频流(m4s)或视频流 → 存到 B站素材库

默认下载音频流（转录语音用，流量极小）；`--video` 可下载视频流。

清晰度（2026-09-06 重写为官方 wbi 接口 + cookie 支持）：
    未登录：视频流最高 480p（B站硬限制）；放入登录 cookie 后 → 1080p（更高需大会员）。
    原因：2026 起 B站登录态页面不再服务端渲染 __playinfo__，播放数据须走官方 API
          （/x/player/wbi/playurl，wbi 签名）。本脚本已改为 API 路线。
    做法：Edge 登录 bilibili.com → F12→Network→刷新视频页→点第一个请求→复制请求头 Cookie 整行
          → 存到脚本同目录 `bilibili_cookies.txt`（一行即可）。
          ⚠️ SESSDATA 约半月刷新一次，失效时重导一次。

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
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BILI_MATERIAL_DIR = Path(r"D:\Claude_Files\003_资源库\B站素材")

# 2026-09-06：UA 需较新（旧 Chrome/120 会被 B站 412 拒——风控对旧 UA 拦截）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# B站登录 cookie（提清晰度）。见 load_bili_cookie。
_BILI_COOKIE = None


def load_bili_cookie():
    """读 B站登录 cookie（脚本同目录 bilibili_cookies.txt，一行 Cookie 串）。
    返回 None 表示无 cookie（视频流最高 480p）。2026-09-06。
    """
    global _BILI_COOKIE
    if _BILI_COOKIE is not None:
        return _BILI_COOKIE
    try:
        p = Path(__file__).parent / "bilibili_cookies.txt"
        if not p.exists():
            _BILI_COOKIE = ""
            return None
        c = p.read_text("utf-8", errors="ignore").strip()
        if c.lower().startswith("cookie:"):
            c = c[len("cookie:"):].strip()
        if not c:
            _BILI_COOKIE = ""
            return None
        _BILI_COOKIE = c
        return c
    except Exception:
        _BILI_COOKIE = ""
        return None


# ---- wbi 签名（B站官方接口鉴权，社区标准算法，2026-09-06 引入） ----
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _wbi_sign(params, img_key, sub_key):
    """给接口参数加 wts(时间戳) + w_rid(md5 签名)。"""
    import requests  # noqa: F401  仅保持一致风格
    mixin = "".join((img_key + sub_key)[i] for i in MIXIN_KEY_ENC_TAB)[:32]
    params["wts"] = int(time.time())
    # 参数按 key 排序后 urlencode，过滤值里的 !'()* 字符（社区签名规则）
    def _clean(v):
        return str(v).translate({ord(c): None for c in "!'()*"})
    query = "&".join(f"{k}={_clean(params[k])}" for k in sorted(params))
    params["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return params


def fetch_playinfo(bvid):
    """获取视频信息 → (标题, up主, dash数据)。
    走官方 API：nav 拿 wbi key → view 拿 cid/标题 → playurl(wbi) 拿 dash 播放流。
    带登录 cookie 时 playurl 返回更高清晰度档（1080P 等）。
    """
    import requests
    s = requests.Session()
    hdr = dict(HEADERS)
    if load_bili_cookie():
        hdr["Cookie"] = _BILI_COOKIE
    s.get("https://www.bilibili.com/", headers=hdr, timeout=15)  # 种 buvid cookie

    # 1. nav：拿 wbi 的 img/sub key（游客也有）
    r = s.get("https://api.bilibili.com/x/web-interface/nav", headers=hdr, timeout=15)
    wbi = (r.json().get("data") or {}).get("wbi_img") or {}
    img_key = os.path.basename((wbi.get("img_url") or "").split("?")[0]).split(".")[0]
    sub_key = os.path.basename((wbi.get("sub_url") or "").split("?")[0]).split(".")[0]
    if not img_key or not sub_key:
        raise SystemExit("❌ 拿不到 wbi key（nav 接口异常），稍后重试")

    # 2. view：标题 / UP主 / cid
    r = s.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}, headers=hdr, timeout=15)
    js = r.json()
    if js.get("code") != 0:
        raise SystemExit(f"❌ 视频信息获取失败: {js.get('code')} {js.get('message')}")
    vd = js.get("data") or {}
    title = vd.get("title") or bvid
    up = (vd.get("owner") or {}).get("name") or "未知"
    cid = vd.get("cid")
    if not cid:
        raise SystemExit("❌ 拿不到视频 cid")

    # 3. playurl(wbi)：qn=80(1080P 起) + fnval=16(dash 流)
    params = {"bvid": bvid, "cid": cid, "qn": 80, "fnval": 16, "fourk": 1}
    _wbi_sign(params, img_key, sub_key)
    r = s.get("https://api.bilibili.com/x/player/wbi/playurl", params=params, headers=hdr, timeout=20)
    js = r.json()
    if js.get("code") != 0:
        raise SystemExit(f"❌ 播放流获取失败: {js.get('code')} {js.get('message')}")
    dash = (js.get("data") or {}).get("dash") or {}
    if not dash:
        raise SystemExit("❌ 无 dash 流（老视频可能只有直链，或需大会员/登录）")
    # dash.video/audio 按清晰度 id 降序排（id 越大越清），main 取 [0] 即最高可用
    dash["video"] = sorted(dash.get("video") or [], key=lambda v: v.get("id") or 0, reverse=True)
    dash["audio"] = sorted(dash.get("audio") or [], key=lambda a: a.get("id") or 0, reverse=True)
    return title, up, dash


def extract_link(text):
    """从文字+链接混合里抠出第一个 https 链接"""
    m = re.search(r'https?://[A-Za-z0-9./?=_%&:#@~-]+', text)
    return m.group(0) if m else text


def extract_bvid(text):
    m = re.search(r'(BV[0-9A-Za-z]{10})', text)
    if not m:
        raise SystemExit("❌ 链接里没找到 BV 号")
    return m.group(1)


def sanitize(name):
    """文件名安全化：去掉非法字符"""
    return re.sub(r'[\\/:*?"<>|\r\n]', '', name).strip()[:20]


def download_stream(url, out_path, referer):
    """带 Referer（+ 登录 cookie）下载流，返回字节数"""
    import requests
    h = dict(HEADERS)
    h["Referer"] = referer
    if load_bili_cookie():
        h["Cookie"] = _BILI_COOKIE
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
        if not load_bili_cookie():
            print("⚠️  未检测到登录 cookie（bilibili_cookies.txt）→ 视频流最高 480p。")
            print("   想下 1080p：见脚本头说明，把 Edge 登录后的 Cookie 存成 bilibili_cookies.txt 再跑。")
        v = videos[0]
        ext = ".m4s"
        size = download_stream(v["baseUrl"], out_dir / f"{bvid}{ext}", f"https://www.bilibili.com/video/{bvid}")
        kind = f"视频(id={v.get('id')})"
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
