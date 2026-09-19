#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
解析抖音/快手/小红书分享链接，下载无水印视频/图集

平台支持（2026-09-06 更新）：
  ✅ 抖音（v.douyin.com / www.iesdouyin.com / www.douyin.com）
  ✅ 快手（v.kuaishou.com）
  ✅ 小红书（xhslink.cn / xhslink.com 短链，www.xiaohongshu.com 页面直链）

抖音反爬处理（2026-08-12 更新）：
  抖音反爬升级 —— 对纯脚本请求（手机/电脑 UA 都试过）返回「验证码中间页」，
  HTML 里不再有视频数据，直接解析会失败。
  因此抖音内置了两级方案：
    1. 直接解析（_ROUTER_DATA / _SSR / RENDER_DATA）—— 抖音未拦截时最快
    2. 浏览器兜底（Playwright 真实浏览器抓网络响应）—— 被拦截时自动切换
  正常使用无感知：丢链接即可，失败自动切浏览器方案。

快手解析（2026-08-18 新增）：
  快手短链跟随重定向 → 抓页面 Relay 数据 → 提取视频地址（photo-video-mz 高清）
  + 标题 + 作者 + 点赞/播放。快手网页版只暴露点赞和播放，无评论/收藏/转发。

小红书解析（2026-09-06 新增，纯 requests 无浏览器兜底）：
  分享短链(xhslink)跟随重定向 → 最终 URL 自带 xsec_token（单页数据通行证）→
  页面 window.__INITIAL_STATE__ 服务端渲染出完整数据：视频走 video.media.stream.h264
  多档取最高清，图文走 imageList；标题/作者/点赞/收藏/评论/转发同取。
  ⚠️ 互动数是中文缩写字符串（'5万'/'999+'）原样展示；下载 CDN 需 xiaohongshu.com Referer。

  2026-09-19 更新（两处，都是实测踩出来的）：
  - 电脑 UA 会被重定向到 /login 登录页（页面无笔记数据）→ 扑空时自动改走手机 UA
  - 页面结构改版：note.noteDetailMap → noteData.data.noteData；作者 nickname → nickName
"""

import argparse
import json
import os
import re
import random
import string
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# 防 GBK 崩（Windows 后台/管道重定向运行）：强制 UTF-8 输出，emoji+中文都能打
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# User Agent
USER_AGENT = 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1'

# ===== 2026-09-03 抖音反爬优化：cookie 缓存 + 浏览器单例复用 + 短链浏览器展开 =====
import time as _time, atexit as _atexit
_BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
_SCRIPT_DIR = Path(__file__).parent
_COOKIE_FILE = _SCRIPT_DIR / '.douyin_cookies.json'
_COOKIE_TTL = 7 * 24 * 3600
_playwright = None
_browser = None


def load_saved_cookies():
    """读缓存 cookie（带时间戳；超期返回空让浏览器重种）"""
    try:
        data = json.loads(_COOKIE_FILE.read_text('utf-8'))
        if _time.time() - data.get('ts', 0) > _COOKIE_TTL:
            return []
        return data.get('cookies', [])
    except Exception:
        return []


def save_cookies(cookies):
    """把浏览器 context 的 cookie 落盘（分域存，供下次 requests 直接复用）"""
    try:
        _COOKIE_FILE.write_text(json.dumps({'ts': _time.time(), 'cookies': cookies}), 'utf-8')
    except Exception:
        pass


def apply_saved_cookies(session):
    """把缓存 cookie 注入 requests session（按域名匹配注入）"""
    for c in load_saved_cookies():
        try:
            session.cookies.set(c.get('name', ''), c.get('value', ''), domain=c.get('domain', ''))
        except Exception:
            pass


def clear_saved_cookies():
    try:
        _COOKIE_FILE.unlink()
    except Exception:
        pass


def get_browser():
    """浏览器单例：同一次运行只起一个 Chromium，多次兜底复用（慢一次、后面快）"""
    global _playwright, _browser
    if _browser is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise Exception('浏览器兜底需要 playwright：pip install playwright && playwright install chromium')
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=True)
    return _browser


def close_browser():
    """统一关浏览器（main finally + atexit 双保险，防孤儿 Chromium 残留）"""
    global _playwright, _browser
    try:
        if _browser:
            _browser.close()
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _browser = None
    _playwright = None


_atexit.register(close_browser)


def _make_context():
    """浏览器上下文（桌面 UA + 带入缓存 cookie——省得每次逛首页重种）"""
    browser = get_browser()
    context = browser.new_context(user_agent=_BROWSER_UA, viewport={'width': 1280, 'height': 800}, locale='zh-CN')
    try:
        context.add_cookies(load_saved_cookies())
    except Exception:
        pass
    return context


def create_session():
    """创建带重试机制的requests session"""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def rand_seq(n):
    """生成随机字符串"""
    letters = string.ascii_letters + string.digits
    return ''.join(random.choice(letters) for _ in range(n))


def generate_fixed_length_numeric_id(length):
    """生成固定位数的随机数字（前导零）"""
    max_num = 10 ** length
    random_num = random.randint(0, max_num - 1)
    return str(random_num).zfill(length)


def get_no_webp_url(url_list):
    """优先获取非 .webp 格式的图片 url"""
    for url in url_list:
        if '.webp' not in url:
            return url
    return url_list[0] if url_list else ''


def get_canonical_from_html(html_content):
    """从 HTML 字符串获取 canonical URL"""
    match = re.search(r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', html_content, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def parse_video_id_from_path(url_path):
    """从路径中提取视频ID"""
    if not url_path:
        return None
    
    # 如果是完整URL，先解析
    if url_path.startswith('http://') or url_path.startswith('https://'):
        parsed = urlparse(url_path)
    else:
        parsed = urlparse('http://example.com' + url_path)
    
    # 判断网页精选页面的视频
    # https://www.douyin.com/jingxuan?modal_id=xxxx
    if parsed.query:
        query_params = parse_qs(parsed.query)
        if 'modal_id' in query_params and query_params['modal_id']:
            return query_params['modal_id'][0]
    
    # 判断其他页面的视频
    # https://www.iesdouyin.com/share/video/xxxx
    path = parsed.path.strip('/')
    if not path:
        return None
    
    path_parts = path.split('/')
    if path_parts:
        return path_parts[-1]
    
    return None


def convert_ssr_data_to_standard_format(video_data):
    """将 SSR 数据格式转换为标准格式"""
    avatar_url_list = []
    if 'author' in video_data and 'avatar_thumb' in video_data['author']:
        if isinstance(video_data['author']['avatar_thumb'], dict) and 'url_list' in video_data['author']['avatar_thumb']:
            avatar_url_list = video_data['author']['avatar_thumb']['url_list']
        elif isinstance(video_data['author']['avatar_thumb'], str):
            avatar_url_list = [video_data['author']['avatar_thumb']]
    
    result = {
        'desc': video_data.get('desc', ''),
        'author': {
            'sec_uid': video_data.get('author', {}).get('sec_uid', ''),
            'nickname': video_data.get('author', {}).get('nickname', ''),
            'avatar_thumb': {
                'url_list': avatar_url_list
            },
        },
    }
    
    # 处理视频数据
    if 'video' in video_data:
        video = video_data['video']
        play_url_list = []
        if 'playAddr' in video and 'url_list' in video['playAddr']:
            play_url_list = video['playAddr']['url_list']
        elif 'play_addr' in video and 'url_list' in video['play_addr']:
            play_url_list = video['play_addr']['url_list']
        
        cover_url_list = []
        if 'cover' in video and 'url_list' in video['cover']:
            cover_url_list = video['cover']['url_list']
        
        result['video'] = {
            'play_addr': {
                'url_list': play_url_list
            },
            'cover': {
                'url_list': cover_url_list
            },
        }
    
    # 处理图片数据
    if 'images' in video_data and isinstance(video_data['images'], list):
        result['images'] = video_data['images']
    
    return result


def extract_video_data_from_html(html, video_id):
    """从HTML中提取视频数据（多种方法）"""
    # 方法1: 尝试从 window._ROUTER_DATA 提取（主要方法）
    match = re.search(r'window\._ROUTER_DATA\s*=\s*(.*?)</script>', html, re.DOTALL)
    if match:
        json_str = match.group(1).strip()
        try:
            json_data = json.loads(json_str)
            if json_data and 'loaderData' in json_data:
                # HTML中的路径是固定的 "video_(id)/page"
                page_key = 'video_(id)/page'
                if page_key not in json_data['loaderData']:
                    page_key = f'video_{video_id}/page'
                
                if page_key in json_data['loaderData']:
                    page_data = json_data['loaderData'][page_key]
                    if 'videoInfoRes' in page_data and 'item_list' in page_data['videoInfoRes']:
                        if page_data['videoInfoRes']['item_list']:
                            return page_data['videoInfoRes']['item_list'][0]
                        elif 'filter_list' in page_data['videoInfoRes']:
                            filter_list = page_data['videoInfoRes']['filter_list']
                            for filter_item in filter_list:
                                if filter_item.get('aweme_id') == video_id:
                                    raise Exception(
                                        f"获取视频信息失败: {filter_item.get('filter_reason', '未知原因')} - {filter_item.get('detail_msg', '')}"
                                    )
        except json.JSONDecodeError:
            pass
    
    # 方法2: 尝试从 window._SSR_HYDRATED_DATA 提取
    match = re.search(r'window\._SSR_HYDRATED_DATA\s*=\s*({.+?});', html, re.DOTALL)
    if match:
        json_str = match.group(1)
        if '&' in json_str:
            import html as html_module
            json_str = html_module.unescape(json_str)
        try:
            json_data = json.loads(json_str)
            if json_data and 'defaultScope' in json_data and 'videoData' in json_data['defaultScope']:
                return convert_ssr_data_to_standard_format(json_data['defaultScope']['videoData'])
        except (json.JSONDecodeError, KeyError):
            pass
    
    # 方法3: 尝试从 RENDER_DATA script 标签提取
    match = re.search(r'<script[^>]*id=["\']RENDER_DATA["\'][^>]*>(.+?)</script>', html, re.DOTALL)
    if match:
        json_str = match.group(1).strip()
        if '%' in json_str:
            from urllib.parse import unquote
            json_str = unquote(json_str)
        try:
            json_data = json.loads(json_str)
            if json_data and 'defaultScope' in json_data:
                if 'videoData' in json_data['defaultScope']:
                    return convert_ssr_data_to_standard_format(json_data['defaultScope']['videoData'])
                elif 'aweme' in json_data['defaultScope']:
                    return convert_ssr_data_to_standard_format(json_data['defaultScope']['aweme'])
        except (json.JSONDecodeError, KeyError):
            pass
    
    # 方法4: 尝试从 window.RENDER_DATA 提取
    match = re.search(r'window\.RENDER_DATA\s*=\s*({.+?});', html, re.DOTALL)
    if match:
        json_str = match.group(1)
        if '&' in json_str:
            import html as html_module
            json_str = html_module.unescape(json_str)
        if '%' in json_str:
            from urllib.parse import unquote
            json_str = unquote(json_str)
        try:
            json_data = json.loads(json_str)
            if json_data and 'defaultScope' in json_data and 'videoData' in json_data['defaultScope']:
                return convert_ssr_data_to_standard_format(json_data['defaultScope']['videoData'])
        except (json.JSONDecodeError, KeyError):
            pass
    
    # 方法5: 尝试直接匹配 videoData 或 aweme_detail
    patterns = [
        r'"videoData":\s*({.+?}),',
        r'"aweme_detail":\s*({.+?}),',
        r'"itemList":\s*\[({.+?})\]',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            json_str = match.group(1)
            if '&' in json_str:
                import html as html_module
                json_str = html_module.unescape(json_str)
            if '%' in json_str:
                from urllib.parse import unquote
                json_str = unquote(json_str)
            try:
                json_data = json.loads(json_str)
                if json_data:
                    return convert_ssr_data_to_standard_format(json_data)
            except (json.JSONDecodeError, KeyError):
                pass
    
    return None


def get_redirect_url(session, video_url):
    """获取重定向后的视频地址"""
    if not video_url:
        return video_url

    try:
        response = session.get(video_url, allow_redirects=False, headers={'User-Agent': USER_AGENT}, timeout=10)
        if 300 <= response.status_code < 400:
            location = response.headers.get('Location')
            if location:
                return location
    except Exception:
        pass

    return video_url


def convert_aweme_to_result(data, video_id, session):
    """把 aweme_detail 数据统一转成标准 result 格式（视频 / 图集共用）。

    数据来源有两处：①HTML里的 _ROUTER_DATA / _SSR / RENDER_DATA；②浏览器兜底抓到的网络响应。
    两者都是 aweme 结构，转出来的 result 字段完全一致，后面下载逻辑不用区分。
    """
    # 图集图片地址
    images = []
    if 'images' in data and isinstance(data['images'], list):
        for image_item in data['images']:
            url_list = image_item.get('url_list', [])
            image_url = get_no_webp_url(url_list)
            if image_url:
                images.append({
                    'url': image_url,
                    'live_photo_url': image_item.get('video', {}).get('play_addr', {}).get('url_list', [None])[0],
                })

    # 提取视频播放地址（图集没有视频）
    # 优先从 bit_rate 里选最高码率档，兜底回顶层 play_addr（2026-08-26 改进：
    # detail 接口顶层 play_addr 可能给低清档，bit_rate 数组里才是各清晰度，取最大码率）
    video_url = ''
    if not images and 'video' in data:
        url_list = []
        bit_rates = data['video'].get('bit_rate') or []
        if bit_rates:
            top = sorted(bit_rates, key=lambda b: (b.get('bit_rate') or 0), reverse=True)[0]
            url_list = (top.get('play_addr') or {}).get('url_list', [])
        if not url_list:
            url_list = data['video'].get('play_addr', {}).get('url_list', [])
        if url_list:
            # 将 playwm 替换为 play，获取无水印视频
            video_url = url_list[0].replace('playwm', 'play')

    # 获取封面
    cover_url = ''
    if 'video' in data and 'cover' in data['video']:
        url_list = data['video']['cover'].get('url_list', [])
        if url_list:
            cover_url = get_no_webp_url(url_list)

    # 提取互动数据（点赞/收藏/评论/转发）
    raw_stats = data.get('statistics', {}) or {}
    statistics = {
        'digg_count': raw_stats.get('digg_count', 0),      # 点赞
        'collect_count': raw_stats.get('collect_count', 0),  # 收藏
        'comment_count': raw_stats.get('comment_count', 0),  # 评论
        'share_count': raw_stats.get('share_count', 0),     # 转发
    }

    result = {
        'title': data.get('desc', ''),
        'video_url': video_url,
        'cover_url': cover_url,
        'images': images,
        'author': {
            'uid': data.get('author', {}).get('sec_uid', ''),
            'name': data.get('author', {}).get('nickname', ''),
            'avatar': data.get('author', {}).get('avatar_thumb', {}).get('url_list', [None])[0],
        },
        'statistics': statistics,
        'platform': 'douyin',   # 平台标识：下载 Referer / 默认输出目录按它分流
    }

    # 获取302重定向之后的真实视频地址
    if result['video_url']:
        result['video_url'] = get_redirect_url(session, result['video_url'])

    if not result['video_url'] and not result['images']:
        raise Exception('没有作品')

    return result


def fetch_via_browser_data(video_id, session):
    """浏览器兜底方案：抖音对纯脚本请求返回「验证码中间页」时，
    用 Playwright 真实浏览器打开视频页，拦截网络响应抓到 aweme_detail 数据。

    原理：请求方带完整浏览器指纹（Playwright Chromium + 正常 UA），抖音会正常返回视频页，
    页面加载时向 /aweme/v1/web/aweme/detail/ 发起请求，响应 JSON 里就是完整视频数据。
    2026-09-03：改用单例浏览器复用 + cookie 落盘缓存（下次 requests 直接复用，少逛首页）。
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401 确保已装
    except ImportError:
        raise Exception('浏览器兜底需要 playwright。请先安装：pip install playwright && playwright install chromium')

    aweme_detail = {}
    context = _make_context()  # 单例浏览器 + 桌面 UA + 带入缓存 cookie
    try:
        page = context.new_page()

        # 监听必须在第一次导航之前注册——否则页面加载时 detail API 已返回，会漏掉。
        def on_response(resp):
            """监听网络响应，从详情接口里抓 aweme_detail"""
            nonlocal aweme_detail
            try:
                if 'aweme/v1' in resp.url or 'aweme/detail' in resp.url:
                    body = resp.json()
                    detail = body.get('aweme_detail') or body.get('aweme') or {}
                    if detail:
                        aweme_detail = detail
            except Exception:
                pass

        page.on('response', on_response)

        # 第1步：有缓存 cookie 就跳过逛首页；没有则先访问首页种 cookie（绕验证码中间页）
        if not load_saved_cookies():
            try:
                print('🌐 浏览器先访问抖音首页种 cookie')
                page.goto('https://www.douyin.com/', wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(3000)
            except Exception:
                pass  # 首页打不开没关系，继续进视频页

        # 第2步：访问视频页，抓详情接口数据
        page_url = f'https://www.douyin.com/video/{video_id}'
        print(f'🌐 浏览器打开: {page_url}')
        page.goto(page_url, wait_until='domcontentloaded', timeout=30000)
        # 等网络响应落地（页面加载 / 可能的验证码需要几秒）
        page.wait_for_timeout(8000)
    except Exception as e:
        raise Exception(f'浏览器兜底失败: {e}')
    finally:
        try:
            save_cookies(context.cookies())  # 种好的 cookie 落盘，下次 requests 直接复用
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass

    if not aweme_detail:
        raise Exception('浏览器方案也未获取到视频数据，可能视频已删除 / 私密，或需要人工打开链接处理验证码')

    return convert_aweme_to_result(aweme_detail, video_id, session)


def parse_video_id(video_id, session):
    """根据视频ID解析视频信息"""
    # 步骤1：请求抖音页面
    req_url = f"https://www.iesdouyin.com/share/video/{video_id}"
    
    response = session.get(req_url, headers={'User-Agent': USER_AGENT}, timeout=30)
    if not response.ok:
        raise Exception(f'请求失败: {response.status_code}')
    
    html = response.text
    
    # 步骤2：判断是否是图集（Note）
    is_note = False
    canonical = get_canonical_from_html(html)
    if canonical and '/note/' in canonical:
        is_note = True
    
    data = None
    
    # 获取图集
    if is_note:
        web_id = '75' + generate_fixed_length_numeric_id(15)
        a_bogus = rand_seq(64)
        
        api_url = (
            f'https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/?reflow_source=reflow_page'
            f'&web_id={web_id}&device_id={web_id}&aweme_ids=%5B{video_id}%5D'
            f'&request_source=200&a_bogus={a_bogus}'
        )
        
        api_response = session.get(api_url, headers={'User-Agent': USER_AGENT}, timeout=30)
        if api_response.ok:
            json_data = api_response.json()
            if json_data.get('aweme_details') and len(json_data['aweme_details']) > 0:
                data = json_data['aweme_details'][0]
            else:
                is_note = False
        else:
            is_note = False
    
    # 获取视频
    if not is_note:
        data = extract_video_data_from_html(html, video_id)
        if not data:
            # 2026-08-12 起，抖音对纯脚本请求返回「验证码中间页」，HTML 里不再有视频数据。
            # 切浏览器兜底方案：用 Playwright 真实浏览器打开页面，从网络响应里抓 aweme_detail。
            print('⚠️ HTML 直接解析失败（大概率触发抖音验证码拦截），切换浏览器兜底方案...')
            return fetch_via_browser_data(video_id, session)

    if not data:
        raise Exception('无法获取视频数据')

    # 统一转成标准 result 格式（视频 / 图集都走这里）
    return convert_aweme_to_result(data, video_id, session)


def resolve_share_url_via_browser(share_url):
    """短链 requests 拿 302 被验证码拦时，用真实浏览器跟随跳转，从最终 URL 提 video_id。
    返回 video_id；等跳转超时抛异常（需人工打开链接处理验证码）。2026-09-03。"""
    context = _make_context()
    try:
        page = context.new_page()
        # 无缓存 cookie 先逛首页种（绕验证码中间页）；有则跳过
        if not load_saved_cookies():
            try:
                page.goto('https://www.douyin.com/', wait_until='domcontentloaded', timeout=30000)
            except Exception:
                pass
        page.goto(share_url, wait_until='domcontentloaded', timeout=30000)
        # 等跳转：最多 15s，直到 URL 出现 /video/ 或 /share/video/ 或 /note/
        for _ in range(30):
            page.wait_for_timeout(500)
            u = page.url
            if re.search(r'/video/|/share/video/|/note/', u):
                vid = parse_video_id_from_path(u)
                if vid:
                    return vid
        raise Exception('浏览器跟随短链跳转超时，可能需人工打开链接处理验证码')
    finally:
        try:
            save_cookies(context.cookies())
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass


def parse_app_share_url(share_url, session):
    """解析App分享链接（v.douyin.com 短链）。
    先纯 requests 拿 302；被验证码拦（非 3xx / 提不出 id）→ 浏览器跟随跳转展开（2026-09-03）。
    """
    # 禁用重定向，获取重定向前的参数
    response = session.get(share_url, allow_redirects=False, headers={'User-Agent': USER_AGENT}, timeout=30)

    if 300 <= response.status_code < 400:
        location = response.headers.get('Location')
        if location:
            parsed_location = urlparse(location)
            if parsed_location.path:
                video_id = parse_video_id_from_path(parsed_location.path)
                if video_id:
                    # 检查是否是西瓜视频
                    if parsed_location.hostname and 'ixigua.com' in parsed_location.hostname:
                        raise Exception('西瓜视频暂不支持')
                    return parse_video_id(video_id, session)

    # requests 拿不到 302（抖音对短链弹验证码中间页）→ 浏览器跟随真实跳转展开
    print('⚠️ 短链纯代码被拦（验证码），改用浏览器跟随跳转...')
    video_id = resolve_share_url_via_browser(share_url)
    if not video_id:
        raise Exception('无法从分享链接中提取视频ID（浏览器也未拿到）')
    return parse_video_id(video_id, session)


def parse_pc_share_url(share_url, session):
    """解析PC端分享链接"""
    video_id = parse_video_id_from_path(share_url)
    if not video_id:
        raise Exception('无法从URL中提取视频ID')
    return parse_video_id(video_id, session)


def _kuaishou_mobile_page(share_url, session):
    """手机端页面兜底（2026-09-19 新增）

    背景（实测）：部分快手作品在【电脑 UA】页面里根本没有 photo-video-mz /
    adaptationSet 结构 —— 电脑 UA 落到 www.kuaishou.com/short-video/{id}
    （约 75KB，Apollo 数据），而【手机 UA】落到
    v.m.chenzhongtech.com/fw/long-video/{id}（约 186KB），数据是全的。
    触发样本：v.kuaishou.com/KDMMW4b5（photoType=VIDEO，但 PC 页面无视频数据）。
    电脑 UA 那条路取不到地址时，用它兜底。

    只读、不抛异常：整体失败返回 None。
    """
    ua = ('Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) '
          'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1')
    try:
        resp = session.get(share_url, headers={'User-Agent': ua}, timeout=30, allow_redirects=True)
        if not resp.ok:
            return None
        html = resp.text
    except Exception:
        return None

    def _unescape(s):
        """只还原 \\uXXXX 转义。
        ⚠️ 不要对含中文的串用 .encode().decode('unicode_escape')：那会把 UTF-8
        字节当 latin-1 解码，中文全变乱码（2026-09-19 亲手踩过，标题/作者全毁）。"""
        return re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)

    out = {'video_url': '', 'video_src': ''}

    m = re.search(r'photoId=([A-Za-z0-9]+)', resp.url) or \
        re.search(r'/(?:long-video|short-video)/([A-Za-z0-9]+)', resp.url)
    if m:
        out['photo_id'] = m.group(1)

    m = re.search(r'"caption":"(.*?)","', html)
    if m:
        out['title'] = _unescape(m.group(1))

    # 视频地址：优先 upic 直链（avc/H.264，兼容性最好且码率更高），
    # 回退 photo-video-mz（hd15 —— 实测这条是 hevc/H.265，留给解码环境好的场景）
    for pat, src in ((r'"url":"(https://[^"]*?/upic/[^"]*?\.mp4[^"]*?)"', 'upic-avc'),
                     (r'"url":"(https://[^"]*?/photo-video-mz/[^"]*?\.mp4[^"]*?)"', 'photo-video-mz')):
        m = re.search(pat, html)
        if m:
            out['video_url'] = _unescape(m.group(1))
            out['video_src'] = src
            break

    m = re.search(r'"coverUrl":"(.*?)","', html)
    if m:
        out['cover_url'] = _unescape(m.group(1))

    for pat in (r'"userName":"(.*?)"', r'"name":"(.*?)","following"'):
        m = re.search(pat, html)
        if m:
            out['author_name'] = _unescape(m.group(1))
            break

    m = re.search(r'"headerUrl":"(.*?)"', html)
    if m:
        out['author_avatar'] = _unescape(m.group(1))

    # 互动数据：手机页面比 PC 页面全（PC 只有点赞，这里点赞/评论/收藏/转发/播放都有）
    # ⚠️ 字段含义已验证（2026-09-19）：viewCount 实测 = 303368，与本条分享文案标注的
    #    "该作品被播放过 29.1 万次"吻合（同一条作品，数字随播放增长）
    #    → 确认为【单条作品】数据；旁证：2.4万赞 / 30万播放 ≈ 8% 点赞率，比例正常。
    #    故同组的 likeCount / commentCount 等亦为作品级，不是作者累计值。
    for field, key in (('likeCount', 'like_count'),
                       ('commentCount', 'comment_count'),
                       ('collectionCount', 'collect_count'),
                       ('shareCount', 'share_count'),
                       ('viewCount', 'view_count')):
        m = re.search(rf'"{field}":\s*(\d+)', html)
        if m:
            out[key] = int(m.group(1))

    return out


def parse_kuaishou(share_url, session):
    """解析快手分享链接（v.kuaishou.com 短链）→ 返回标准 result 格式。

    流程：
    1. 短链跟随重定向 → 拿到 www.kuaishou.com/short-video/{photoId} 页面
    2. 抓 HTML，从 Relay 数据（$VisionVideoDetailPhoto）提取：
       - caption：标题
       - adaptationSet[].representation[].url：视频地址（photo-video-mz = 图集视频 hd15）
       - coverUrl：封面
       - 作者节点（name / headerUrl）：作者
       - realLikeCount / viewCount：点赞 / 播放
    3. 转成与抖音一致的 result 格式（platform='kuaishou'）

    快手网页版只暴露点赞和播放数，评论/收藏/转发页面 JSON 里没有 → 记为 0。
    """
    # 1. 跟随重定向，拿最终视频页
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    resp = session.get(share_url, headers={'User-Agent': ua}, timeout=30, allow_redirects=True)
    if not resp.ok:
        raise Exception(f'快手请求失败: {resp.status_code}')
    html = resp.text
    final_url = resp.url

    # 2. 从最终 URL 提取 photoId（如 3xga748i8zcdu7k）
    photo_id = None
    m = re.search(r'/short-video/([A-Za-z0-9]+)', final_url)
    if m:
        photo_id = m.group(1)
    if not photo_id:
        m = re.search(r'"photoId":"([A-Za-z0-9]+)"', html)
        if m:
            photo_id = m.group(1)
    if not photo_id:
        raise Exception('无法从快手链接提取 photoId')

    # 3. 标题 caption
    title = ''
    m = re.search(r'"caption":"(.*?)",', html)
    if m:
        title = m.group(1)

    # 4. 视频地址：优先 photo-video-mz 的 hd15（图集视频/高清），否则取第一个 representation url
    video_url = ''
    idx = html.find('photo-video-mz')
    if idx != -1:
        seg = html[idx - 80:idx + 4000]
        m = re.search(r'"url":"([^"]*)"', seg)
        if m:
            video_url = m.group(1)
    if not video_url:
        idx = html.find('"adaptationSet"')
        if idx != -1:
            seg = html[idx:idx + 4000]
            m = re.search(r'"url":"([^"]*)"', seg)
            if m:
                video_url = m.group(1)
    if video_url:
        video_url = video_url.encode().decode('unicode_escape')

    # 4.5 兜底（2026-09-19 新增）：电脑 UA 页面扑空 → 改走手机端页面
    #     部分作品（图文/幻灯片类，实测 v.kuaishou.com/KDMMW4b5）PC 页面不含
    #     photo-video-mz / adaptationSet，只有手机端 v.m.chenzhongtech.com 页面才有。
    mobile_fb = None
    if not video_url:
        mobile_fb = _kuaishou_mobile_page(share_url, session)
        if mobile_fb:
            video_url = mobile_fb.get('video_url', '')
            if video_url:
                print(f"  ↩ 电脑端页面无视频数据，已改走手机端页面（源: {mobile_fb.get('video_src')}）")
            if not title and mobile_fb.get('title'):
                title = mobile_fb['title']
            if not photo_id and mobile_fb.get('photo_id'):
                photo_id = mobile_fb['photo_id']

    # 5. 封面
    cover_url = ''
    m = re.search(r'"coverUrl":"(.*?)","', html)
    if m:
        cover_url = m.group(1).encode().decode('unicode_escape')

    # 6. 作者：从最终 URL 的 userId 参数定位页面里的作者节点
    author_uid = ''
    author_name = ''
    author_avatar = ''
    parsed_final = urlparse(final_url)
    query_params = parse_qs(parsed_final.query)
    if query_params.get('userId'):
        author_uid = query_params['userId'][0]
        m = re.search(re.escape(author_uid) + r'","name":"(.*?)","following"', html)
        if m:
            author_name = m.group(1)
    m = re.search(r'"headerUrl":"(.*?)"', html)
    if m:
        author_avatar = m.group(1).encode().decode('unicode_escape')

    # 7. 互动数据：点赞（realLikeCount）+ 播放（viewCount）
    like_count = 0
    m = re.search(r'"realLikeCount":(\d+)', html)
    if m:
        like_count = int(m.group(1))
    view_count = ''
    m = re.search(r'"viewCount":"([^"]*)"', html)
    if m:
        view_count = m.group(1)

    # 7.5 手机端兜底补齐（2026-09-19）：PC 页面取不到的字段，用手机页面的值填。
    #     PC 页面只有点赞；手机页面点赞/评论/收藏/转发/播放是整套 → 补齐后数据更完整。
    collect_count = 0
    comment_count = 0
    share_count = 0
    if mobile_fb:
        if not author_name:
            author_name = mobile_fb.get('author_name', '')
        if not author_avatar:
            author_avatar = mobile_fb.get('author_avatar', '')
        if not like_count and mobile_fb.get('like_count'):
            like_count = mobile_fb['like_count']
        if not collect_count and mobile_fb.get('collect_count'):
            collect_count = mobile_fb['collect_count']
        if not comment_count and mobile_fb.get('comment_count'):
            comment_count = mobile_fb['comment_count']
        if not share_count and mobile_fb.get('share_count'):
            share_count = mobile_fb['share_count']
        if not view_count and mobile_fb.get('view_count'):
            # 统一成字符串，与 PC 路径（"36万" 这类）保持同型，免得下游格式化踩坑
            view_count = str(mobile_fb['view_count'])

    statistics = {
        'digg_count': like_count,
        'collect_count': collect_count,
        'comment_count': comment_count,
        'share_count': share_count,
        'view_count': view_count,   # 快手特有：播放量
    }

    result = {
        'title': title,
        'video_url': video_url,
        'cover_url': cover_url,
        'images': [],
        'author': {
            'uid': author_uid,
            'name': author_name,
            'avatar': author_avatar,
        },
        'statistics': statistics,
        'platform': 'kuaishou',
        'video_id': photo_id,
    }

    if not result['video_url']:
        raise Exception('快手页面未找到视频地址，可能视频已删除 / 私密')

    return result


def extract_xhs_note_from_html(html):
    """从小红书页面 HTML 的 window.__INITIAL_STATE__ 提取笔记数据 note。

    2026-09-06 实测结论（纯 requests，无浏览器兜底）：
    - 分享短链(xhslink)展开后的完整 URL 自带 xsec_token —— 这是解锁单页完整数据的通行证，
      服务端把笔记数据（视频源/标题/作者/互动）整个渲染进 __INITIAL_STATE__。
    - __INITIAL_STATE__ 是 JS 全局态直拼页面，内含裸 undefined（非合法 JSON）→ 先替换成 null。
    - 内容可能带 HTML 实体（&amp; 等）→ 先 html.unescape 再解析（对无实体串幂等无害）。
    - 2026-09-19 实测：页面结构已改版，笔记对象路径从 note.noteDetailMap[noteId].note
      变为 noteData.data.noteData；作者字段从 nickname 变为 nickName。本函数两套都兼容。
    """
    import html as html_module
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>', html, re.DOTALL)
    if not m:
        raise Exception('页面未找到笔记数据（__INITIAL_STATE__）。\n  可能原因：直链缺 xsec_token / 被风控 / 需登录。\n  请用 App 里的「复制链接」分享（xhslink 开头的短链）再试。')
    raw = html_module.unescape(m.group(1))
    # 裸 undefined → null（实测 20+ 处，多见于前端全局态字段）
    raw = re.sub(r':\s*undefined', ':null', raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise Exception('笔记数据解析失败（小红书页面结构可能已变更，需更新脚本）')

    # 定位 note —— 两套结构都试（新版优先）
    note = None
    try:
        cand = data['noteData']['data']['noteData']
        if isinstance(cand, dict) and cand.get('noteId'):
            note = cand
    except (KeyError, TypeError):
        pass
    if not note:
        note_map = (data.get('note') or {}).get('noteDetailMap') or {}
        for _k, v in note_map.items():
            if isinstance(v, dict) and isinstance(v.get('note'), dict):
                note = v['note']
                break
    if not note:
        raise Exception('笔记数据为空（视频已删除 / 私密 / 需登录？）')
    return note


def xhs_image_url(item):
    """小红书图文取原图直链（绕过 sns-webpic-qc 水印/压缩层，2026-09-06 实测）。

    实测：urlDefault(qc 层) 同图仅 ~293KB 且带水印；fileId 拼 ci.xiaohongshu.com 直链 = 5.5MB 原图。
    优先级：fileId 全路径拼 ci → traceId（fileId 空时）→ urlDefault 兜底。
    ci 直链 URL 无扩展名，下载时按 content-type 定 .png/.jpg/.webp（见 main 图集段）。
    """
    fid = (item.get('fileId') or '').strip()
    if fid:
        return f'https://ci.xiaohongshu.com/{fid}'
    tid = (item.get('traceId') or '').strip()
    if tid:
        return f'https://ci.xiaohongshu.com/{tid}'
    ud = item.get('urlDefault') or ''
    if ud.startswith('http://'):
        ud = 'https://' + ud[len('http://'):]
    return ud


def xhs_live_url(item):
    """小红书实况图(live)的视频源：livePhoto.media.stream.h264 优先，旧版兜底 item.stream.h264。
    取最高清档 masterUrl（统一 https）。无 live 返回空串。
    """
    for holder in [item.get('livePhoto'), item]:
        if not isinstance(holder, dict):
            continue
        cand = holder
        media = holder.get('media')
        if isinstance(media, dict) and isinstance(media.get('stream'), dict):
            cand = media
        streams = (cand.get('stream') or {}).get('h264') or []
        if isinstance(cand.get('h264'), list):
            streams = cand['h264'] or streams
        best = None
        for s in streams:
            if not isinstance(s, dict):
                continue
            if best is None or (s.get('width') or 0) > (best.get('width') or 0):
                best = s
        if best:
            mu = best.get('masterUrl') or ''
            if mu.startswith('http://'):
                mu = 'https://' + mu[len('http://'):]
            return mu
    return ''


def parse_xhs(share_url, session):
    """解析小红书分享链接（xhslink.cn/xhslink.com 短链，或 xiaohongshu.com 页面直链）→ 标准 result 格式。

    流程（2026-09-06 实测）：
    1. xhslink 短链 → requests 跟随重定向 → 最终 URL 自带 xsec_token（requests 自动跟随，
       resp.text 即最终页面，一次 GET 搞定）
    2. 从 __INITIAL_STATE__ 拿 note：视频走 video.media.stream.h264 多档取最高清，
       图文走 imageList；标题/作者/互动数据同取
    3. platform='xhs'，下载时带 xiaohongshu.com Referer（实测 CDN 校验 Referer）
    """
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    resp = session.get(share_url, headers={'User-Agent': ua}, timeout=30, allow_redirects=True)
    if not resp.ok:
        raise Exception(f'小红书请求失败: {resp.status_code}')
    try:
        note = extract_xhs_note_from_html(resp.text)
    except Exception as e_pc:
        # 2026-09-19 新增：电脑 UA 会被重定向到 /login 登录页（该页无笔记数据）
        # → 改走手机 UA 再试一次（实测手机 UA 直达笔记页，__INITIAL_STATE__ 数据完整）
        ua_phone = ('Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) '
                    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1')
        note = None
        try:
            resp2 = session.get(share_url, headers={'User-Agent': ua_phone}, timeout=30, allow_redirects=True)
            if resp2.ok:
                note = extract_xhs_note_from_html(resp2.text)
        except Exception:
            note = None
        if not note:
            raise e_pc
        print("  ↩ 电脑端被跳登录页，已改走手机端页面")
    note_id = note.get('noteId') or 'note'
    note_type = note.get('type', '')          # video=视频笔记；normal/image=图文
    is_video = note_type == 'video'

    # 作者
    user = note.get('user') or {}
    avatar = user.get('avatar', '')
    if isinstance(avatar, dict):
        avatar = avatar.get('url') or avatar.get('urlDefault') or ''

    # 视频源：video.media.stream.h264[] 多档清晰度 → 取 width 最大（最清）档
    video_url = ''
    if is_video:
        media = (note.get('video') or {}).get('media') or {}
        streams = (media.get('stream') or {}).get('h264') or []
        best = None
        for s in streams:
            if not isinstance(s, dict):
                continue
            if best is None or (s.get('width') or 0) > (best.get('width') or 0):
                best = s
        if best:
            mu = best.get('masterUrl') or ''
            if mu.startswith('http://'):
                mu = 'https://' + mu[len('http://'):]   # CDN http/https 都通，统一 https（实测 206）
            video_url = mu

    # 图文：imageList 原图（ci 直链绕过 qc 水印层）+ 实况图视频（livePhoto/stream）
    images = []
    if not is_video:
        for item in note.get('imageList') or []:
            if not isinstance(item, dict):
                continue
            url = xhs_image_url(item)
            if url:
                images.append({'url': url, 'live_photo_url': xhs_live_url(item)})

    # 互动数据：小红书是中文缩写字符串（'5万' / '999+'）→ 原样保留，不做 int 转换
    it = note.get('interactInfo') or {}
    statistics = {
        'digg_count': it.get('likedCount', 0),
        'collect_count': it.get('collectedCount', 0),
        'comment_count': it.get('commentCount', 0),
        'share_count': it.get('shareCount', 0),
    }

    result = {
        'title': note.get('title') or note.get('desc') or '',
        'video_url': video_url,
        'cover_url': '',                       # 封面未在 note 顶层，暂不强求（可后续从 video.image 挖）
        'images': images,
        'author': {
            'uid': str(user.get('userId', '')),
            'name': user.get('nickName') or user.get('nickname', ''),   # 新版 nickName / 旧版 nickname
            'avatar': avatar,
        },
        'statistics': statistics,
        'platform': 'xhs',
        'video_id': note_id,
    }

    if not result['video_url'] and not result['images']:
        raise Exception('未解析到视频或图片。\n  可能笔记已删除 / 私密，或页面数据不完整——试试 App 里重新复制链接。')
    return result


def parse_share_url(share_url, session):
    """解析分享链接（自动识别抖音 / 快手）"""
    parsed_url = urlparse(share_url)
    if not parsed_url.hostname:
        raise Exception('无效的URL')

    host = parsed_url.hostname

    if host in ['www.iesdouyin.com', 'www.douyin.com']:
        return parse_pc_share_url(share_url, session)
    elif host == 'v.douyin.com':
        return parse_app_share_url(share_url, session)
    elif host == 'v.kuaishou.com' or 'kuaishou.com' in host:
        return parse_kuaishou(share_url, session)
    elif host in ['xhslink.cn', 'xhslink.com'] or 'xiaohongshu.com' in host:
        return parse_xhs(share_url, session)
    else:
        raise Exception(f"不支持的域名: {host}")


# CDN Referer 校验映射：抖音 douyinvod 校 douyin.com，快手 kwaicdn 校 kuaishou.com，
# 小红书 sns-video xhscdn 校 xiaohongshu.com（实测不带对应 Referer 会 403 / 拒下）
PLATFORM_REFERERS = {
    'douyin': 'https://www.douyin.com/',
    'kuaishou': 'https://www.kuaishou.com/',
    'xhs': 'https://www.xiaohongshu.com/',
}


def download_hls_stream(m3u8_url, output_path, session, platform='kuaishou', file_type='视频'):
    """下载 HLS(m3u8) 分段流并拼接成 mp4（快手偶发 HLS 源，2026-09-03 补）。
    m3u8 里是 .ts 段（无 KEY 加密可直接纯拼接）；带加密则报错提示。
    """
    referer = PLATFORM_REFERERS.get(platform, PLATFORM_REFERERS['douyin'])
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Referer': referer,
    }
    # 1. 拉 m3u8 清单
    resp = session.get(m3u8_url, headers=headers, timeout=30)
    resp.raise_for_status()
    content = resp.text
    if '#EXT-X-KEY' in content:
        raise Exception('该 HLS 流已加密，无法直接合并（需 ffmpeg/解密）')
    segs = [l for l in content.split('\n') if l.strip() and not l.startswith('#')]
    if not segs:
        raise Exception('m3u8 里没有分段')
    base = m3u8_url.rsplit('/', 1)[0]
    print(f'📦 HLS 源（{file_type}）：{len(segs)} 段，下载合并中...')
    total = b''
    for i, seg in enumerate(segs):
        full = seg if seg.startswith('http') else base + '/' + seg
        d = session.get(full, headers=headers, timeout=60).content
        if len(d) < 1000:
            raise Exception(f'分段 {i+1} 异常（链接可能已失效）')
        total += d
        if (i + 1) % 5 == 0 or i == len(segs) - 1:
            print(f'\r  段 {i+1}/{len(segs)} 累计 {len(total) // 1024 // 1024}MB', end='', flush=True)
    print()
    with open(output_path, 'wb') as f:
        f.write(total)
    return output_path


def download_file(url, output_path, session, file_type='文件', platform='douyin'):
    """下载文件（通用）。
    抖音 CDN（douyinvod.com）校验 douyin.com Referer，快手 CDN（kwaicdn.com）校验 kuaishou.com Referer，
    不带对应 Referer 会 403；桌面 UA + 站内 Referer 必带。
    """
    # 2026-09-03：HLS(m3u8) 源 → 走分段合并（快手偶发；直链 mp4 不受影响）
    if '.m3u8' in url.lower():
        return download_hls_stream(url, output_path, session, platform, file_type)

    referer = PLATFORM_REFERERS.get(platform, PLATFORM_REFERERS['douyin'])
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Referer': referer,
    }
    response = session.get(url, headers=headers, stream=True, timeout=60)
    response.raise_for_status()
    
    total_size = int(response.headers.get('content-length', 0))
    
    with open(output_path, 'wb') as f:
        downloaded = 0
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    percent = (downloaded / total_size) * 100
                    print(f"\r下载进度: {percent:.1f}%", end='', flush=True)
    
    print()  # 换行
    return output_path


def probe_image_ext(session, url):
    """URL 无扩展名时探测真实图片格式（stream 读前 16 字节看 magic，比 content-type 可靠——实测
    ci CDN 的 content-type 对后续请求会失真）。小红书 ci.xiaohongshu.com 原图直链用它。2026-09-06。
    """
    try:
        r = session.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Referer': PLATFORM_REFERERS['xhs'],
        }, stream=True, timeout=15)
        head = next(r.iter_content(16), b'') if r.ok else b''
        r.close()
        if head[:4] == b'\x89PNG':
            return '.png'
        if head[:2] == b'\xff\xd8':
            return '.jpg'
        if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
            return '.webp'
        return '.jpg'
    except Exception:
        return '.jpg'


def sanitize_title_for_dir(title, fallback='video', max_len=50):
    """把抖音标题清洗成可用作文件夹名的字符串。

    去掉换行 / 话题标签(#xxx) / 文件系统非法字符，压缩空白并截断；
    清洗后为空则回退到 fallback（通常是视频ID）。
    """
    if not title:
        return fallback
    name = title.replace('\n', ' ').replace('\r', ' ')
    # 去掉话题标签（# 到下一个空白）
    name = re.sub(r'#\S+', '', name)
    # 去掉文件系统非法字符 / \ : * ? " < > | 以及控制字符
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '', name)
    # 压缩连续空白
    name = re.sub(r'\s+', ' ', name).strip()
    # 去掉结尾的点和空格（Windows 不允许）
    name = name.rstrip(' .')
    if len(name) > max_len:
        name = name[:max_len].rstrip(' .')
    return name or fallback


def write_statistics_file(stats_file, share_url, video_id, result):
    """把互动数据写入 Markdown 文件（与视频同目录）。
    抖音：点赞/收藏/评论/转发；快手：点赞/播放（快手网页不暴露评论收藏转发 → 标未提供）；
    小红书：点赞/收藏/评论/转发（值为中文缩写如 '5万'，原样展示）。
    """
    from datetime import date
    st = result.get('statistics', {}) or {}
    author = result.get('author', {}) or {}
    platform = result.get('platform', 'douyin')
    label_map = {'kuaishou': '快手', 'xhs': '小红书'}
    platform_label = label_map.get(platform, '抖音')
    lines = [
        f"# {author.get('name', '未知作者')} · {platform_label}视频互动数据",
        '',
        f"- 来源链接：{share_url}",
        f"- 视频 ID：{video_id}",
        f"- 作者：{author.get('name', '')}",
        f"- 标题：{result.get('title', '')}",
        f"- 数据抓取时间：{date.today().isoformat()}",
        '',
        '## 互动数据',
        '',
        '| 指标 | 数量 |',
        '|------|------|',
        f"| 👍 点赞 | {st.get('digg_count', 0)} |",
    ]
    if platform == 'kuaishou':
        lines.append(f"| ▶️ 播放 | {st.get('view_count', '未提供')} |")
    else:
        lines.append(f"| ⭐ 收藏 | {st.get('collect_count', 0)} |")
    lines += [
        f"| 💬 评论 | {st.get('comment_count', 0) if platform in ('douyin', 'xhs') else '未提供'} |",
        f"| 🔁 转发 | {st.get('share_count', 0) if platform in ('douyin', 'xhs') else '未提供'} |",
        '',
    ]
    stats_file.write_text('\n'.join(lines), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='解析抖音/快手/小红书分享链接，下载无水印视频/图集')
    parser.add_argument('url', type=str, help='抖音/快手/小红书分享链接（小红书用 App 复制的 xhslink 短链最稳）')
    parser.add_argument('--output-dir', type=str, default=None, help='输出目录。在 skill 目录执行时默认落到项目根的 <标题>/（标题不可用时回退到视频ID）；否则用当前目录下的 downloads/')
    parser.add_argument('--images', action='store_true', help='下载图集图片（默认视频/图集自动识别）')
    
    args = parser.parse_args()

    # 抖音分享口令通常是文字+链接混合（如 "3.82 复制此链接... https://v.douyin.com/xxx/"），
    # 自动从整段文本中提取出第一个 https 链接，只留链接部分
    url_match = re.search(r'https?://[A-Za-z0-9./?=_%&:#@~-]+', args.url)
    if url_match:
        args.url = url_match.group(0)
        print(f'🔗 已从文本中提取链接: {args.url}')
    else:
        print(f'🔗 使用输入的链接: {args.url}')

    # 输出目录：解析后按平台分流（抖音→抖音素材，快手→快手素材），这里先留空
    auto_base = None

    session = create_session()
    apply_saved_cookies(session)  # 2026-09-03：注入缓存 cookie，方式1（纯代码）更可能直接过

    try:
        print(f"正在解析分享链接: {args.url}")
        result = parse_share_url(args.url, session)
        
        print('解析成功！')
        print('')
        print('作品信息:')
        print(f'标题: {result.get("title", "未获取到")}')
        
        if result.get('video_url'):
            print(f'类型: 视频')
            print(f'视频链接: {result.get("video_url", "")}')
        elif result.get('images'):
            print(f'类型: 图集 ({len(result["images"])} 张)')
        
        print(f'封面: {result.get("cover_url", "未获取到")}')
        
        if result.get('images'):
            print('')
            print(f'图集图片 ({len(result["images"])} 张):')
            for index, image in enumerate(result['images']):
                print(f'  图片 {index + 1}: {image["url"]}')
                if image.get('live_photo_url'):
                    print(f'    Live Photo: {image["live_photo_url"]}')
        
        if result.get('author'):
            print('')
            print('作者信息:')
            print(f'昵称: {result["author"].get("name", "")}')
            print(f'UID: {result["author"].get("uid", "")}')
            print(f'头像: {result["author"].get("avatar", "")}')

        if result.get('statistics'):
            st = result['statistics']
            print('')
            print('互动数据:')
            print(f'点赞: {st.get("digg_count", 0)}')
            print(f'收藏: {st.get("collect_count", 0)}')
            print(f'评论: {st.get("comment_count", 0)}')
            print(f'转发: {st.get("share_count", 0)}')

        # 默认输出目录按平台分流：抖音 → 抖音素材，快手 → 快手素材（用户约定）
        if auto_base is None and args.output_dir is None:
            if result.get('platform') == 'kuaishou':
                auto_base = Path(r'D:\Claude_Files\003_资源库\快手素材')
            elif result.get('platform') == 'xhs':
                auto_base = Path(r'D:\Claude_Files\003_资源库\小红书素材')
            else:
                auto_base = Path(r'D:\Claude_Files\003_资源库\抖音素材')

        # 无论是否指定 --output-dir，都按「作者-标题前20字」确定子文件夹名（都不可用时回退到视频ID）
        # auto_base：未指定 --output-dir 时的默认父目录；显式指定 --output-dir 时用该目录作为父目录
        if auto_base is not None or args.output_dir is not None:
            fallback_id = result.get('video_id') or parse_video_id_from_path(args.url) or 'video'
            author_part = sanitize_title_for_dir(result.get('author', {}).get('name'), fallback='', max_len=15)
            title_part = sanitize_title_for_dir(result.get('title'), fallback='', max_len=20)
            parts = [p for p in (author_part, title_part) if p]
            folder_name = '-'.join(parts) if parts else fallback_id
            base = auto_base if auto_base is not None else Path(args.output_dir)
            args.output_dir = str(base / folder_name)
            print('')
            print(f"💡 文件将保存到: {args.output_dir}")

        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        video_id = result.get('video_id') or parse_video_id_from_path(args.url)
        if not video_id:
            video_id = 'video'

        # 下载视频
        video_url = result.get('video_url')
        if video_url:
            filename = f"{video_id}.mp4"
            output_path = output_dir / filename

            print('')
            print(f'正在下载视频到: {output_path}')
            download_file(video_url, output_path, session, '视频', result.get('platform', 'douyin'))
            print(f'视频下载完成: {output_path}')

            # 保存互动数据到同目录的 互动数据.md
            if result.get('statistics'):
                stats_file = output_dir / '互动数据.md'
                write_statistics_file(stats_file, args.url, video_id, result)
                print(f'互动数据已保存到: {stats_file}')
        
        # 下载图集
        images = result.get('images', [])
        if images:
            print('')
            print(f'正在下载图集 ({len(images)} 张)...')
            for index, image in enumerate(images):
                img_url = image['url']
                # 从 URL 中提取扩展名；无扩展名（小红书 ci 原图直链）→ HEAD 按 content-type 定
                ext = None
                parsed_img = urlparse(img_url)
                path_lower = parsed_img.path.lower()
                for e in ['.jpg', '.jpeg', '.png', '.webp']:
                    if path_lower.endswith(e):
                        ext = e
                        break
                if ext is None:
                    ext = probe_image_ext(session, img_url)
                
                img_filename = f"{video_id}_{index+1:02d}{ext}"
                img_path = output_dir / img_filename
                
                print(f'  下载图片 {index+1}/{len(images)}: {img_filename}')
                download_file(img_url, img_path, session, f'图片{index+1}', result.get('platform', 'douyin'))

                # 下载 Live Photo 视频（如果有）
                if image.get('live_photo_url'):
                    live_filename = f"{video_id}_{index+1:02d}_live.mp4"
                    live_path = output_dir / live_filename
                    print(f'  下载 Live Photo {index+1}: {live_filename}')
                    download_file(image['live_photo_url'], live_path, session, f'LivePhoto{index+1}', result.get('platform', 'douyin'))
            
            print(f'图集下载完成，共 {len(images)} 张图片')

            # 保存互动数据到同目录的 互动数据.md
            if result.get('statistics'):
                stats_file = output_dir / '互动数据.md'
                write_statistics_file(stats_file, args.url, video_id, result)
                print(f'互动数据已保存到: {stats_file}')
        
        # 输出JSON格式结果
        print('')
        print('JSON格式:')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        
        return 0
        
    except Exception as e:
        print(f'解析失败: {str(e)}', file=sys.stderr)
        return 1
    finally:
        close_browser()  # 收尾统一关浏览器（防孤儿 Chromium 残留）


if __name__ == "__main__":
    sys.exit(main())
