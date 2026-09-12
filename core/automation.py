"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息。

发送逻辑参考 douyin-cloud-streak（MIT），要点：
- 点击联系人后校验右侧会话确实切换（防止限流时错发给上一个人）；
- 列表点击失败时用搜索框兜底；
- 检测"操作频繁 / 安全验证"等提示，命中即停本轮；
- 发送前清空输入框，发送后校验输入框已清空。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from .config import DATA_DIR, load_config, normalize_video_url, video_id
from .runtime import advance_video_cursor, load_runtime, record_video_delivery, video_was_sent

logger = logging.getLogger("douyin-spark")

STATE_PATH = DATA_DIR / "state.json"
SCREENSHOT_PATH = DATA_DIR / "last_error.png"
CHAT_URL = "https://www.douyin.com/chat"
LIKES_URL = "https://www.douyin.com/user/self?showTab=like"

RATE_LIMIT_KEYWORDS = [
    "操作频繁",
    "操作太频繁",
    "发送过于频繁",
    "请稍后再试",
    "稍后再试",
    "安全验证",
    "滑动验证",
    "验证码",
    "验证中心",
    "人机验证",
    "网络异常",
    "请勿频繁",
]

LOGIN_TEXTS = ["扫码登录", "验证码登录", "登录后查看", "登录后即可"]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _screenshot(page) -> None:
    try:
        page.screenshot(path=str(SCREENSHOT_PATH), timeout=5000)
        logger.info("已保存页面截图: %s", SCREENSHOT_PATH)
    except Exception:
        pass


def check_login(page) -> tuple[bool, str]:
    """返回 (是否已登录, 说明)。宁可误报掉线，也不要带着过期登录态硬跑。"""
    url = page.url
    if "login" in url.lower() or "passport" in url.lower():
        return False, f"页面已跳转到登录页（{url}）"

    try:
        qr = page.locator("#animate_qrcode_container")
        if qr.count() and qr.first.is_visible():
            return False, "页面出现扫码登录二维码，登录态已过期"
    except Exception:
        pass

    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return False, f"页面出现登录提示「{text}」"
        except Exception:
            continue

    cookies = page.context.cookies()
    if not any(c["name"].startswith("sessionid") for c in cookies):
        return False, "未检测到 sessionid Cookie"
    return True, "ok"


def detect_rate_limit(page) -> str | None:
    for kw in RATE_LIMIT_KEYWORDS:
        try:
            loc = page.get_by_text(kw, exact=False)
            for i in range(loc.count()):
                if loc.nth(i).bounding_box():
                    return kw
        except Exception:
            continue
    return None


def _find_contact(page, name: str):
    """优先按全文精确匹配联系人标题，避免误点其他会话里的消息预览。"""
    exact = page.get_by_text(name, exact=True)
    if exact.count():
        return exact.first
    by_class = page.locator(".conversationConversationItemtitle, [class*='Itemtitle' i], [class*='ItemTitle' i]").filter(has_text=name)
    if by_class.count():
        return by_class.first
    return page.locator(".conversationConversationListwrapper, .componentsLeftPanelboxList").get_by_text(name).first


def verify_in_conversation(page, name: str) -> bool:
    """右侧会话顶部标题区域（x>300 且 y<100）出现目标昵称才算切换成功，防止错发。"""
    for exact in (True, False):
        try:
            loc = page.get_by_text(name, exact=exact)
            for i in range(loc.count()):
                try:
                    box = loc.nth(i).bounding_box()
                except Exception:
                    continue
                if box and box.get("x", 0) > 300 and box.get("y", 0) < 100:
                    return True
        except Exception:
            continue
    return False


def search_and_open(page, name: str) -> bool:
    try:
        box = page.get_by_placeholder("搜索", exact=False).first
        if box.count() == 0:
            return False
        box.click()
        box.fill(name)
        time.sleep(4)
        # 优先直接点搜索结果里的「发消息」按钮，最可靠
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(4)
            return True
        # 否则点精确匹配的结果卡片，再找「发消息」入口
        candidate = page.get_by_text(name, exact=True).first
        if candidate.count() == 0:
            candidate = page.get_by_text(name, exact=False).first
        if candidate.count() == 0:
            return False
        candidate.click(force=True)
        time.sleep(3)
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(3)
        return True
    except Exception as e:
        logger.info("搜索打开 %s 失败: %s", name, e)
        return False


def _type_and_send(page, input_box, msg_text: str) -> bool:
    """把文字输入输入框并按 Enter 发送，返回文字是否成功进入输入框。"""
    try:
        input_box.click()
        time.sleep(0.4)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        time.sleep(0.3)
        page.keyboard.type(msg_text, delay=100)
        time.sleep(0.8)
        cur = input_box.inner_text() or ""
        if msg_text not in cur:
            logger.warning("文字未进入输入框，当前内容: %r", cur[:30])
            return False
        page.keyboard.press("Enter")
        return True
    except Exception as e:
        logger.info("输入/发送异常: %s", str(e)[:100])
        return False


def _wait_input_cleared(input_box, msg_text: str, wait: float = 8) -> bool:
    """消息发出后输入框应不再包含发送文字，以此确认真正发出。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1)
        try:
            cur = input_box.inner_text() or ""
            if msg_text not in cur:
                return True
        except Exception:
            pass
    return False


def _prepare_conversation(page, name: str) -> tuple[bool, str, any]:
    """切换并验证好友会话，确保聊天输入框就绪。返回 (ok, why, input_box)。"""
    switched = False
    for attempt in range(10):
        try:
            target = _find_contact(page, name)
            if target.count():
                try:
                    target.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                target.click(force=True, timeout=10000)
                time.sleep(random.uniform(2, 4))
                if verify_in_conversation(page, name):
                    switched = True
                    break
            else:
                # 目标可能因列表懒加载尚未渲染，滚动侧边栏继续找
                try:
                    page.mouse.move(200, 350)
                    page.mouse.wheel(0, 800)
                except Exception:
                    pass
                time.sleep(1.5)
        except Exception as e:
            logger.info("点击联系人 %s 异常: %s", name, str(e)[:100])
        time.sleep(random.uniform(1, 2))

    if not switched and search_and_open(page, name):
        time.sleep(random.uniform(1, 3))
        switched = verify_in_conversation(page, name)

    if not switched:
        _screenshot(page)
        return False, "未能切换到该好友会话（名字不在聊天列表，或页面结构变化）", None

    if detect_rate_limit(page):
        return False, "检测到「操作频繁 / 安全验证」提示", None

    input_box = page.locator('div[contenteditable="true"]').first
    try:
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return False, "找不到聊天输入框", None
        input_box.wait_for(state="visible", timeout=8000)
    except Exception:
        return False, "找不到聊天输入框", None

    return True, "", input_box


def send_to_contact(page, name: str, msg_text: str, dry_run: bool) -> tuple[bool, str]:
    ok, why, input_box = _prepare_conversation(page, name)
    if not ok:
        return False, why

    if dry_run:
        return True, "dry-run"

    try:
        if detect_rate_limit(page):
            return False, "发送前检测到验证提示"
        if not _type_and_send(page, input_box, msg_text):
            return False, "文字未能输入到输入框"
        if _wait_input_cleared(input_box, msg_text, wait=8):
            return True, "ok"
        logger.warning("未检测到消息发出，重试一次：%s", name)
        if detect_rate_limit(page):
            return False, "重试时检测到验证提示"
        if not _type_and_send(page, input_box, msg_text):
            return False, "重试时文字未能输入"
        if _wait_input_cleared(input_box, msg_text, wait=8):
            return True, "ok"
        return False, "发送后输入框未清空，消息可能未发出"
    except Exception as e:
        logger.info("向 %s 发送异常: %s", name, e)
        return False, f"发送异常: {e}"


def _try_native_video_share(page, video_url: str) -> bool:
    """Best-effort native share-card attempt; callers always have a text fallback."""
    selectors = [
        page.get_by_role("button", name="分享", exact=False),
        page.locator('[aria-label*="分享"]'),
        page.locator('button:has-text("分享")'),
    ]
    try:
        clicked = False
        for loc in selectors:
            if loc.count() and loc.first.is_visible():
                loc.first.click(force=True, timeout=2500)
                clicked = True
                break
        if not clicked:
            return False
        page.wait_for_timeout(800)
        # Some Douyin builds expose a share-to-DM action after opening the menu.
        dm = page.get_by_text("私信", exact=False)
        if dm.count() and dm.first.is_visible():
            dm.first.click(force=True, timeout=2500)
            page.wait_for_timeout(800)
        if video_url and page.get_by_text(video_url, exact=False).count():
            return True
    except Exception:
        pass
    return False


def send_video_to_contact(page, name: str, video: dict, dry_run: bool) -> tuple[bool, str]:
    """Send one video to the active conversation, falling back to a URL message."""
    ok, why, input_box = _prepare_conversation(page, name)
    if not ok:
        return False, why

    if dry_run:
        return True, "dry-run"

    if _try_native_video_share(page, video.get("url", "")):
        return True, "ok"

    msg = video.get("url", "")
    if not _type_and_send(page, input_box, msg):
        return False, "视频卡片失败，链接也未能输入"
    if _wait_input_cleared(input_box, msg, wait=8):
        return True, "fallback"
    return False, "视频卡片失败，链接发送后输入框未清空"


def fetch_liked_videos(limit: int = 20) -> dict:
    """Read the current account's recent liked videos from the logged-in page."""
    result = {"at": _now(), "videos": [], "error": None}
    if not STATE_PATH.exists():
        result["error"] = "尚未上传登录态 state.json"
        return result
    browser = None
    try:
        p = sync_playwright().start()
        try:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                storage_state=str(STATE_PATH),
                viewport={"width": 1366, "height": 768},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = context.new_page()
            page.goto(LIKES_URL, timeout=90000, wait_until="domcontentloaded")
            page.wait_for_timeout(10000)
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result
            extract_js = """
                () => Array.from(document.querySelectorAll('a[href*="/video/"]')).map(a => {
                  const card = a.closest('li, article, [data-e2e]') || a;
                  const img = card.querySelector('img[alt]');
                  return {url: a.href, title: (img?.alt || card.innerText || '').trim().split('\\n')[0]};
                })
            """
            seen = set()
            for _ in range(15):
                for item in page.evaluate(extract_js) or []:
                    url = normalize_video_url(urljoin(page.url, str(item.get("url", ""))))
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    result["videos"].append({"id": video_id(url), "url": url, "title": str(item.get("title") or "")[:200], "source": "likes", "added_at": _now()})
                    if len(result["videos"]) >= max(1, min(int(limit or 20), 20)):
                        return result
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(900)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("读取点赞视频异常: %s", e)
        result["error"] = f"读取点赞视频异常: {e}"
    return result


def fetch_chat_contacts() -> dict:
    """从抖音私信页左侧聊天列表完整读取所有联系人（含火花天数/重燃状态），火花好友自动置顶。"""
    result = {"at": _now(), "names": [], "error": None}
    if not STATE_PATH.exists():
        result["error"] = "尚未上传登录态 state.json"
        return result

    browser = None
    try:
        p = sync_playwright().start()
        try:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                storage_state=str(STATE_PATH),
                viewport={"width": 1366, "height": 768},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = context.new_page()

            goto_ok = False
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=90000, wait_until="domcontentloaded")
                    goto_ok = True
                    break
                except Exception as e:
                    logger.info("获取联系人时第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            if not goto_ok:
                result["error"] = "无法打开抖音私信页面"
                return result

            page.wait_for_timeout(8000)
            logged, why = check_login(page)
            if not logged:
                _screenshot(page)
                result["error"] = why
                return result

            # 智能等待联系人列表就绪（避免粗暴 reload 掐断 WebSocket 握手）
            check_list_js = """
                () => {
                    const selectors = [
                        '.conversationConversationItemtitle',
                        '[class*="Itemtitle" i]',
                        '[class*="ItemTitle" i]',
                        '[class*="conversationConversationItem"]',
                        '[class*="conversationItem" i]',
                        '.conversationConversationListwrapper > div',
                        '.componentsLeftPanelboxList [role="listitem"]'
                    ];
                    let count = 0;
                    for (const sel of selectors) {
                        const found = document.querySelectorAll(sel);
                        if (found.length > 0) {
                            count = found.length;
                            break;
                        }
                    }
                    const emptyWrapper = document.querySelector('.LeftPanelEmptywrapper, [class*="Emptywrapper" i]');
                    const emptyText = emptyWrapper ? (emptyWrapper.innerText || '').trim() : '';
                    const hasEmpty = Boolean(emptyWrapper) || emptyText.includes('暂无会话');
                    const hasSearch = Boolean(document.querySelector('input[placeholder*="搜索"]'));

                    return { count, hasEmpty, hasSearch };
                }
            """

            list_ready = False
            reloaded_once = False
            wait_start = time.time()
            max_wait_seconds = 65

            while time.time() - wait_start < max_wait_seconds:
                # 检查风控提示
                rl = detect_rate_limit(page)
                if rl:
                    logger.warning("等待联系人时检测到风控提示: %s", rl)
                    break

                info = page.evaluate(check_list_js) or {}
                cnt = info.get("count", 0)
                has_empty = info.get("hasEmpty", False)

                if cnt > 0:
                    list_ready = True
                    logger.info("已检测到联系人列表就绪，共发现至少 %s 个候选元素", cnt)
                    break

                elapsed = int(time.time() - wait_start)
                if elapsed in (10, 22, 35):
                    # 适时轻触页面激活前端 IM SDK 数据同步（避免空等）
                    try:
                        page.mouse.move(200, 300)
                        page.mouse.wheel(0, 80)
                    except Exception:
                        pass

                # 若页面超过 40 秒依然处于空态且未重试过，执行一次温和刷新重试
                if elapsed > 40 and has_empty and not reloaded_once:
                    logger.info("页面处于加载空态超过 40 秒，执行一次温和刷新重连 IM...")
                    reloaded_once = True
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(8000)
                    except Exception:
                        pass
                    continue

                page.wait_for_timeout(2000)

            if not list_ready:
                _screenshot(page)
                rl = detect_rate_limit(page)
                if rl:
                    result["error"] = f"触发了安全风控拦截（{rl}），页面未正常渲染会话列表"
                else:
                    result["error"] = "等待联系人列表超时（请检查登录态有效性或云服务器到抖音聊天服务的长连接状况）"
                return result

            # 稍微等待首屏卡片渲染完全
            page.wait_for_timeout(2000)

            extract_js = """
                () => {
                    const out = [];
                    // 严格匹配标题元素，排除外层 wrapper
                    let titles = Array.from(document.querySelectorAll(
                        '.conversationConversationItemtitle, [class*="Itemtitle" i], [class*="ItemTitle" i], [class*="item_title" i], [class*="itemTitle" i]'
                    )).filter(el => {
                        const cls = el.className || '';
                        return typeof cls === 'string' && !cls.includes('wrapper') && !cls.includes('Wrapper');
                    });

                    // 如果类名未命中，从列表容器内部兜底遍历
                    if (titles.length === 0) {
                        const wrapper = document.querySelector('.conversationConversationListwrapper, .componentsLeftPanelboxList');
                        if (wrapper) {
                            const items = wrapper.querySelectorAll('div[role="listitem"], div[class*="Itemwrapper" i]');
                            items.forEach(row => {
                                const h = row.querySelector('[class*="title" i]') || row.querySelector('span, p');
                                if (h && (h.textContent || '').trim()) {
                                    titles.push(h);
                                }
                            });
                        }
                    }

                    const seenNames = new Set();
                    titles.forEach(t => {
                        let name = (t.textContent || '').trim().split('\\n')[0].trim();
                        // 过滤干扰词
                        if (!name || name === '暂无会话' || name === '搜索' || name === '未找到相关结果' || name.length > 50) return;
                        if (seenNames.has(name)) return;
                        seenNames.add(name);

                        // 寻找所属卡片容器
                        let cur = t;
                        for (let i = 0; i < 6; i++) {
                            if (cur.parentElement && cur.parentElement.tagName !== 'BODY') {
                                cur = cur.parentElement;
                                if (cur.getAttribute('role') === 'listitem' || 
                                    (cur.className && typeof cur.className === 'string' && 
                                     (cur.className.includes('Item') || cur.className.includes('item')))) {
                                    break;
                                }
                            }
                        }

                        let streakText = '';
                        let hasFlame = false;

                        // 仅在当前卡片容器内部寻找火焰/火花图片或 svg
                        const imgs = cur ? cur.querySelectorAll('img, svg') : [];
                        imgs.forEach(img => {
                            const src = img.getAttribute('src') || '';
                            const cls = (img.className && typeof img.className === 'string') ? img.className : '';
                            if (src.includes('flame') || src.includes('streak') || src.includes('chat_days') ||
                                cls.includes('flame') || cls.includes('streak')) {
                                hasFlame = true;
                            }
                        });

                        // 标准及备选 streak 文本类名
                        const s = cur ? cur.querySelector('.commonStreaknormalText, [class*="Streak" i], [class*="streak" i]') : null;
                        if (s && (s.textContent || '').trim()) {
                            streakText = s.textContent.trim();
                            if (/^\\d+$/.test(streakText)) {
                                streakText += '天';
                            }
                        }

                        // 卡片整体文本智能正则：提取重燃/消失提醒/火花天数
                        if (!streakText && cur) {
                            const txt = cur.innerText || '';
                            const m = txt.match(/重燃中\\s*\\d+\\/\\d+|\\d+\\s*天后消失/);
                            if (m) {
                                streakText = m[0].trim();
                            } else if (hasFlame) {
                                const numMatch = txt.match(/(?:\\b|\\s)(\\d{1,4})(?:\\s*天|\\s*🔥|\\s*$)/);
                                if (numMatch) {
                                    streakText = numMatch[1].trim() + '天';
                                }
                            }
                        }

                        // 有火焰图标但未能提取到具体天数时兜底标为火花
                        if (!streakText && hasFlame) {
                            streakText = '火花';
                        }

                        out.push({
                            name: name,
                            streak: streakText,
                            has_spark: Boolean(streakText || hasFlame)
                        });
                    });
                    return out;
                }
            """

            contacts_map: dict[str, dict] = {}
            consecutive_no_new = 0
            max_scrolls = 100

            for scroll_idx in range(max_scrolls):
                data = page.evaluate(extract_js) or []
                new_items_count = 0

                # 初始防空保护：如果第一轮没有抓到，等待 2.5 秒重试一次
                if scroll_idx == 0 and len(data) == 0:
                    page.wait_for_timeout(2500)
                    data = page.evaluate(extract_js) or []

                for item in data:
                    name = item["name"]
                    if name not in contacts_map:
                        contacts_map[name] = item
                        new_items_count += 1
                    else:
                        if not contacts_map[name].get("streak") and item.get("streak"):
                            contacts_map[name]["streak"] = item["streak"]
                            contacts_map[name]["has_spark"] = True
                        elif not contacts_map[name].get("has_spark") and item.get("has_spark"):
                            contacts_map[name]["has_spark"] = True

                if new_items_count > 0:
                    consecutive_no_new = 0
                else:
                    consecutive_no_new += 1
                    # 连续 8 次滚动未发现新联系人，判定已真正滚动至底部
                    if consecutive_no_new >= 8 and len(contacts_map) > 0:
                        break

                # 驱动列表滚动：鼠标滚轮
                try:
                    page.mouse.move(200, 350)
                    page.mouse.wheel(0, 900)
                except Exception:
                    pass

                # 若出现连续没有新增，借助 scrollIntoView 与容器 wheel 事件双重兜底触发虚拟列表加载
                if consecutive_no_new >= 2:
                    try:
                        page.evaluate("""
                            () => {
                                const titles = document.querySelectorAll(
                                    '.conversationConversationItemtitle, [class*="Itemtitle" i], [class*="ItemTitle" i]'
                                );
                                if (titles.length > 0) {
                                    titles[titles.length - 1].scrollIntoView({ behavior: 'auto', block: 'end' });
                                }
                                const container = document.querySelector('.conversationConversationListwrapper') || 
                                                  document.querySelector('.componentsLeftPanelboxList') ||
                                                  document.querySelector('#imSaasContainerId');
                                if (container) {
                                    container.dispatchEvent(new WheelEvent('wheel', { deltaY: 1000, bubbles: true }));
                                }
                            }
                        """)
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)
                else:
                    page.wait_for_timeout(1000)


            # 整理联系人列表：火花好友优先置顶排序
            def _sort_key(c: dict) -> tuple[int, int, str]:
                st = c.get("streak", "")
                has_s = bool(c.get("has_spark") or st)
                # 优先级 0 为有火花，1 为无火花
                prio = 0 if has_s else 1
                # 尝试解析火花纯天数数字（倒序排列）
                numeric_streak = 0
                if st:
                    digits = "".join(ch for ch in st if ch.isdigit())
                    if digits:
                        try:
                            numeric_streak = -int(digits)
                        except Exception:
                            numeric_streak = 0
                return (prio, numeric_streak, c.get("name", ""))

            all_contacts = list(contacts_map.values())
            all_contacts.sort(key=_sort_key)

            result["names"] = all_contacts
            spark_count = sum(1 for c in all_contacts if c.get("has_spark") or c.get("streak"))
            logger.info("已读取聊天列表联系人共 %s 个（其中火花好友 %s 个）", len(result["names"]), spark_count)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("获取联系人异常: %s", e)
        result["error"] = f"获取联系人异常: {e}"
    return result


def run_send(
    dry_run: bool = False,
    only_names: list[str] | None = None,
    force_video: dict | None = None,
    advance_video: bool = False,
) -> dict:
    cfg = load_config()
    friends = cfg.get("friends") or []
    if only_names is not None:
        friends = [f for f in friends if f in only_names]
    messages = cfg.get("messages") or ["🔥"]
    max_n = int(cfg.get("max_friends_per_run", 20) or 20)
    gap_min = max(1, int(cfg.get("send_gap_min", 6) or 6))
    gap_max = max(gap_min, int(cfg.get("send_gap_max", 12) or 12))
    queue = cfg.get("video_queue") or []
    selected_video = force_video
    if selected_video is None and cfg.get("video_mode_enabled") and queue:
        rt = load_runtime()
        cursor = int(rt.get("video_queue_cursor", 0) or 0) % len(queue)
        selected_video = queue[cursor]

    result = {
        "at": _now(),
        "dry_run": bool(dry_run),
        "ok": [],
        "failed": [],
        "logged_out": False,
        "rate_limited": False,
        "video": selected_video,
        "video_advanced": False,
        "video_fallback_count": 0,
        "video_text_fallback_count": 0,
    }

    if not STATE_PATH.exists():
        result["failed"].append({"name": "_system", "reason": "尚未上传登录态 state.json"})
        return result

    targets = friends[:max_n] if max_n > 0 else friends
    browser = None
    try:
        p = sync_playwright().start()
        try:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                storage_state=str(STATE_PATH),
                viewport={"width": 1366, "height": 768},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = context.new_page()

            goto_ok = False
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                    goto_ok = True
                    break
                except Exception as e:
                    logger.info("第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            if not goto_ok:
                result["failed"].append({"name": "_system", "reason": "无法打开抖音私信页面"})
                return result

            time.sleep(5)
            logged, why = check_login(page)
            if not logged:
                result["logged_out"] = True
                result["failed"].append({"name": "_system", "reason": why})
                _screenshot(page)
                return result

            # 关键：等待左侧会话列表渲染完成，未渲染时自动刷新重试
            for attempt in range(3):
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=30000)
                    break
                except Exception:
                    logger.info("发送前等待联系人列表超时，刷新重试第 %s 次", attempt + 1)
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=90000)
                        page.wait_for_timeout(8000)
                    except Exception:
                        pass
            time.sleep(2)

            if not targets:
                logger.info("未配置任何好友，跳过发送")
                return result

            logger.info("待发送好友 %s 人，dry_run=%s", len(targets), dry_run)
            for name in targets:
                if selected_video:
                    if not dry_run and video_was_sent(selected_video.get("id", ""), name):
                        logger.info("跳过已发送视频 %s -> %s", selected_video.get("id"), name)
                        result["ok"].append(name)
                        continue
                    ok, why = send_video_to_contact(page, name, selected_video, dry_run)
                    if why == "fallback":
                        result["video_fallback_count"] += 1
                    if not ok and not dry_run:
                        text_msg = random.choice(messages)
                        ok, text_why = send_to_contact(page, name, text_msg, dry_run)
                        if ok:
                            result["video_text_fallback_count"] += 1
                            why = "text-fallback"
                else:
                    msg = random.choice(messages)
                    ok, why = send_to_contact(page, name, msg, dry_run)
                if ok:
                    result["ok"].append(name)
                    if selected_video and not dry_run and why != "text-fallback":
                        record_video_delivery(selected_video.get("id", ""), name, fallback=(why == "fallback"), at=result["at"])
                    sent_label = "文字回退" if why == "text-fallback" else (selected_video.get("url") if selected_video else (msg if not dry_run else "(干跑，未真实发送)"))
                    logger.info("已发送给 %s：%s", name, sent_label)
                else:
                    result["failed"].append({"name": name, "reason": why})
                    logger.warning("发送给 %s 失败：%s", name, why)
                    if detect_rate_limit(page):
                        result["rate_limited"] = True
                        logger.warning("疑似触发限流，停止本轮")
                        break
                time.sleep(random.uniform(gap_min, gap_max))
            if selected_video and not dry_run and targets and not result["logged_out"]:
                if (force_video is None and cfg.get("video_mode_enabled") and queue) or (force_video is not None and advance_video and queue):
                    advance_video_cursor(len(queue))
                    result["video_advanced"] = True
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("运行异常: %s", e)
        result["failed"].append({"name": "_system", "reason": f"运行异常: {e}"})
    return result
