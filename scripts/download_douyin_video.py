#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
解析抖音/快手分享链接，下载无水印视频/图集

平台支持（2026-08-18 更新）：
  ✅ 抖音（v.douyin.com / www.iesdouyin.com / www.douyin.com）
  ✅ 快手（v.kuaishou.com）

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

    statistics = {
        'digg_count': like_count,
        'collect_count': 0,
        'comment_count': 0,
        'share_count': 0,
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
    else:
        raise Exception(f"不支持的域名: {host}")


def download_hls_stream(m3u8_url, output_path, session, platform='kuaishou', file_type='视频'):
    """下载 HLS(m3u8) 分段流并拼接成 mp4（快手偶发 HLS 源，2026-09-03 补）。
    m3u8 里是 .ts 段（无 KEY 加密可直接纯拼接）；带加密则报错提示。
    """
    referer = 'https://www.kuaishou.com/' if platform == 'kuaishou' else 'https://www.douyin.com/'
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

    referer = 'https://www.kuaishou.com/' if platform == 'kuaishou' else 'https://www.douyin.com/'
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
    抖音：点赞/收藏/评论/转发；快手：点赞/播放（快手网页不暴露评论收藏转发 → 标未提供）。
    """
    from datetime import date
    st = result.get('statistics', {}) or {}
    author = result.get('author', {}) or {}
    platform = result.get('platform', 'douyin')
    platform_label = '快手' if platform == 'kuaishou' else '抖音'
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
        f"| 💬 评论 | {st.get('comment_count', 0) if platform == 'douyin' else '未提供'} |",
        f"| 🔁 转发 | {st.get('share_count', 0) if platform == 'douyin' else '未提供'} |",
        '',
    ]
    stats_file.write_text('\n'.join(lines), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='解析抖音/快手分享链接，下载无水印视频/图集')
    parser.add_argument('url', type=str, help='抖音/快手分享链接')
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
                # 从 URL 中提取扩展名，默认 .jpg
                ext = '.jpg'
                parsed_img = urlparse(img_url)
                path_lower = parsed_img.path.lower()
                for e in ['.jpg', '.jpeg', '.png', '.webp']:
                    if path_lower.endswith(e):
                        ext = e
                        break
                
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
