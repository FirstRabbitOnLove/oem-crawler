# -*- coding: utf-8 -*-
"""
OEM 全站爬虫 worker（Python 端）

职责：
  1. 轮询 oem_crawl_run，领取 pending 任务；
  2. 发现 URL（sitemap / robots / 站内链接 BFS）；
  3. 抓取页面、提取正文/标题/图片/链接；
  4. 可选 Playwright 渲染 + 截图；
  5. 写入 oem_crawl_page，更新 oem_crawl_url 状态；
  6. 心跳、断点续跑、多 worker 并存。

不做的事：
  - 不做 AI 判断（AI 由 PHP 调用）；
  - 不修改网站内容；
  - 不访问 checkout/account/login。

运行：
  python crawler_worker.py
  python crawler_worker.py --once
  python crawler_worker.py --run-id 12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import signal
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape as html_unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse, unquote

try:
    import pymysql
except ImportError:
    print("缺少依赖 pymysql，请先 pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先 pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    from playwright.sync_api import sync_playwright
except Exception:  # Playwright 是可选能力
    sync_playwright = None


WORKER_ID = "%s-%d-%d" % (socket.gethostname()[:24], os.getpid(), random.randint(1000, 9999))

# 心跳超过该分钟数即视为 worker 已死，任务可被其他 worker 接管。
DEFAULT_STALE_MINUTES = 10

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 目标站/WAF 有时只对某个 UA 返回错误页；首页/页面失败时按顺序重试。
UA_LADDER = (
    UA,
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
)

# -- 反爬拦截页识别 ---------------------------------------------------
# Cloudflare / WAF 的人机验证页特征。命中后不再把验证页当正文送 AI，
# 该页标记为 blocked（PHP 侧 analyze 只取 status='ok'，会自动跳过）。
BLOCK_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "cf-chl",
    "cf_chl",
    "challenge-platform",
    "cf-mitigated",
    "enable javascript and cookies to continue",
    "ddos protection by cloudflare",
    "cloudflare ray id",
    "verify you are human",
    "checking if the site connection is secure",
    "正在进行安全验证",
    "安全验证",
    "验证您不是自动程序",
    "网站使用安全服务防护",
)

# 注入浏览器的反自动化脚本：抹掉 Playwright 的自动化指纹。
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) =>
        parameters && parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);
}
"""

# 可选的 playwright-stealth，装了就叠加使用；不同版本 API 不同，调用处容错。
try:
    from playwright_stealth import stealth_sync as _stealth_sync
except Exception:
    _stealth_sync = None


def detect_block(status, html_text, title):
    """判断返回内容是否为人机验证/拦截页，返回 (是否拦截, 原因)。"""
    if not html_text:
        return False, ""
    head = html_text[:40000].lower()
    title_low = (title or "").lower()
    for marker in BLOCK_MARKERS:
        if marker in head or marker in title_low:
            return True, "blocked:%s" % marker
    if status in (403, 429, 503) and ("cloudflare" in head or "captcha" in head):
        return True, "blocked:http-%s" % status
    return False, ""


ERROR_PAGE_RE = re.compile(
    r"\b(?:4\d\d|5\d\d)\s+"
    r"(?:internal server error|bad request|forbidden|not found|service unavailable|"
    r"bad gateway|gateway time-?out|too many requests)\b",
    re.I,
)


def detect_server_error(status, html_text, title=""):
    """识别「HTTP 200 但正文其实是 nginx/网关错误页」的假成功。"""
    if status >= 500:
        return True
    sample = (html_text or "")[:3000]
    if not sample:
        return False
    if len(sample) < 2000 and ERROR_PAGE_RE.search(sample):
        return True
    if title and re.match(r"^\s*(?:4\d\d|5\d\d)\b", title):
        return True
    return False


SKIP_PATTERNS = re.compile(
    r"/(cart|checkout|account|login|register|logout|wishlist|search|"
    r"password|order-status|pay|payment)(/|$|\?)",
    re.I,
)

SKIP_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|json|xml|pdf|zip|rar|7z|mp4|mp3|"
    r"woff|woff2|ttf|eot|doc|docx|xls|xlsx|ppt|pptx|csv|txt)$",
    re.I,
)

PRODUCT_PATTERNS = [
    re.compile(r"/(products?|item|goods|p|dp)/[^/]+", re.I),
    re.compile(r"/product/[^/]+", re.I),
]
COLLECTION_PATTERNS = [
    re.compile(r"/(collections?|category|categories|shop|store)(/|$)", re.I),
]
POLICY_PATTERNS = [
    re.compile(r"/(shipping|delivery|returns?|refund|privacy|terms|policy|faq|policies)(/|$)", re.I),
]
CONTACT_PATTERNS = [re.compile(r"/(contact|contact-us|support)(/|$)", re.I)]
ABOUT_PATTERNS = [re.compile(r"/(about|about-us|our-story|story|pages/about)(/|$)", re.I)]
BLOG_PATTERNS = [re.compile(r"/(blog|news|article|articles|journal)(/|$)", re.I)]
REVIEW_PATTERNS = [
    re.compile(r"/(comments?|reviews?|testimonials?)(/|$)", re.I),
    re.compile(r"/product[^/]*/reviews?(/|$)", re.I),
]

SKIP_TAGS = {"head", "script", "style", "noscript", "template", "svg", "canvas", "iframe"}
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "li", "tr", "h1",
    "h2", "h3", "h4", "h5", "h6", "br", "td", "th", "main", "aside", "form",
}

SOCIAL_HOSTS = {
    "facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "twitter.com": "Twitter",
    "x.com": "X",
    "youtube.com": "YouTube",
    "pinterest.com": "Pinterest",
    "linkedin.com": "LinkedIn",
    "t.me": "Telegram",
    "wa.me": "WhatsApp",
}

PAYMENT_KEYS = [
    "visa", "mastercard", "maestro", "paypal", "amex", "americanexpress",
    "apple pay", "applepay", "google pay", "googlepay", "klarna", "afterpay",
    "discover", "diners", "jcb", "unionpay",
]

# 主题布局词：单独成行时是模板/布局残留，不是页面文案。
LAYOUT_TOKENS = {
    "center", "left", "right", "flex", "block", "inline", "none", "auto",
    "hidden", "row", "column", "justify", "align", "start", "end", "middle",
    "top", "bottom", "grid", "wrap", "relative", "absolute", "static",
}

TRACKING_PATTERNS = [
    ("ga4", re.compile(r"\b(G-[A-Z0-9]{6,12})\b")),
    ("ua", re.compile(r"\b(UA-\d{4,10}-\d{1,4})\b")),
    ("gtm", re.compile(r"\b(GTM-[A-Z0-9]{4,10})\b")),
    ("fbid", re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{10,20})", re.I)),
    ("tiktok", re.compile(r"ttq\.load\(\s*['\"]([A-Z0-9]{10,30})", re.I)),
    ("adsense", re.compile(r"(ca-pub-\d{10,20})")),
    ("hotjar", re.compile(r"hjid['\"]?\s*[:=]\s*['\"]?(\d{5,10})", re.I)),
    ("clarity", re.compile(r"clarity\.ms/tag/([a-z0-9]{10})", re.I)),
    ("yandex", re.compile(r"ym\(\s*(\d{6,10})")),
    ("pinterest", re.compile(r"pintrk\(\s*['\"]load['\"]\s*,\s*['\"](\d{10,20})", re.I)),
]

PLACEHOLDER_IMAGE_RE = re.compile(
    r"(loading|placeholder|empty|blank|lazy|spacer|no[-_]?image|default)[^/]*\.(png|jpe?g|gif|webp|svg)",
    re.I,
)

POLICY_KEY_PATTERNS = (
    ("shipping", re.compile(r"shipping|delivery|发货|配送|运费", re.I)),
    ("returns", re.compile(r"returns?|refunds?|退货|退款", re.I)),
    ("privacy", re.compile(r"privacy|隐私", re.I)),
    ("terms", re.compile(r"terms|conditions|tos|条款", re.I)),
    ("contact", re.compile(r"contact|support|联系", re.I)),
    ("about", re.compile(r"about|story|关于", re.I)),
    ("faq", re.compile(r"faq|help|帮助", re.I)),
)


def policy_key(haystack):
    for key, pattern in POLICY_KEY_PATTERNS:
        if pattern.search(haystack or ""):
            return key
    return ""


def jsonld_types(blocks):
    types = set()

    def walk(obj):
        if isinstance(obj, dict):
            value = obj.get("@type")
            if isinstance(value, list):
                types.update(str(item) for item in value)
            elif value:
                types.add(str(value))
            for child in obj.values():
                walk(child)
        elif isinstance(obj, list):
            for child in obj:
                walk(child)

    for block in blocks or []:
        walk(block)
    return sorted(types)[:30]


def product_jsonld(blocks):
    """从 JSON-LD 中提取商品名、价格、币种、库存、SKU 和规格可选值。"""
    products = []

    def walk(obj):
        if isinstance(obj, dict):
            types = obj.get("@type")
            type_values = types if isinstance(types, list) else [types]
            if any(str(value).lower() == "product" for value in type_values):
                products.append(obj)
            for child in obj.values():
                walk(child)
        elif isinstance(obj, list):
            for child in obj:
                walk(child)

    for block in blocks or []:
        walk(block)

    if not products:
        return {}
    product = products[0]
    offers = product.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    offers = [item for item in offers if isinstance(item, dict)]
    prices = []
    for offer in offers:
        try:
            prices.append(float(offer.get("price")))
        except (TypeError, ValueError):
            pass
    names = sorted({
        str(offer.get("name") or "").strip()
        for offer in offers
        if str(offer.get("name") or "").strip()
    })[:50]
    first_offer = offers[0] if offers else {}
    return {
        "name": str(product.get("name") or "")[:500],
        "description": str(product.get("description") or "")[:4000],
        "description_chars": len(str(product.get("description") or "").strip()),
        "sku": str(product.get("sku") or first_offer.get("sku") or first_offer.get("mpn") or "")[:120],
        "price": round(min(prices), 2) if prices else None,
        "price_max": round(max(prices), 2) if prices else None,
        "currency": str(first_offer.get("priceCurrency") or "")[:12],
        "availability": str(first_offer.get("availability") or "")[:200],
        "offer_count": len(offers),
        "option_names": names,
    }


def tracking_ids(html_text):
    found = {}
    for key, pattern in TRACKING_PATTERNS:
        values = []
        for match in pattern.finditer(html_text or ""):
            value = match.group(1)
            if value and value not in values:
                values.append(value)
            if len(values) >= 3:
                break
        if values:
            found[key] = values
    return found


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print("[%s] %s" % (now(), msg), flush=True)


def doh_query(name, rtype, timeout=6, session=None):
    """DoH 查询（Cloudflare），失败返回 None。"""
    try:
        client = session or requests
        resp = client.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": name, "type": rtype},
            headers={"accept": "application/dns-json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("Answer") or []
    except Exception:
        return None


def tls_info(host, timeout=8):
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as wrapped:
                cert = wrapped.getpeercert()
        issuer = dict(item[0] for item in cert.get("issuer", [])).get("organizationName", "")
        not_after = cert.get("notAfter", "")
        days_left = None
        try:
            expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            days_left = int((expires - datetime.now(timezone.utc)).total_seconds() // 86400)
        except Exception:
            pass
        return {"ok": True, "issuer": issuer, "not_after": not_after, "days_left": days_left}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def rdap_info(root_domain, timeout=8, session=None):
    try:
        client = session or requests
        resp = client.get("https://rdap.org/domain/%s" % root_domain, timeout=timeout)
        if resp.status_code != 200:
            return {}
        data = resp.json()
    except Exception:
        return {}

    events = {}
    for event in data.get("events") or []:
        events[(event.get("eventAction") or "").lower()] = event.get("eventDate")
    registered = events.get("registration") or ""
    expires = events.get("expiration") or ""
    registrar = ""
    for entity in data.get("entities") or []:
        if "registrar" in (entity.get("roles") or []):
            vcard = entity.get("vcardArray") or []
            if len(vcard) > 1:
                for item in vcard[1]:
                    if item and item[0] == "fn":
                        registrar = str(item[3])
                        break
    age_days = None
    try:
        reg_dt = datetime.fromisoformat(registered.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - reg_dt).days
    except Exception:
        pass
    return {
        "registered": registered[:40],
        "expires": expires[:40],
        "registrar": registrar[:200],
        "age_days": age_days,
    }


def load_dotenv():
    """
    读取脚本同目录的 .env（进程守护不会自动注入）。

    已存在的环境变量优先，方便临时用 export 覆盖。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def db_settings():
    """集中校验数据库配置，缺项时给出明确报错而不是回落成 root。"""
    host = os.environ.get("OEM_CRAWL_DB_HOST", "").strip()
    user = os.environ.get("OEM_CRAWL_DB_USER", "").strip()
    name = os.environ.get("OEM_CRAWL_DB_NAME", "").strip()
    password = os.environ.get("OEM_CRAWL_DB_PASSWORD", "")
    port = os.environ.get("OEM_CRAWL_DB_PORT", "3306").strip() or "3306"

    missing = [
        key
        for key, value in (
            ("OEM_CRAWL_DB_HOST", host),
            ("OEM_CRAWL_DB_USER", user),
            ("OEM_CRAWL_DB_NAME", name),
            ("OEM_CRAWL_DB_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "数据库配置缺失：%s\n"
            "请在 %s 写入这些变量，或在进程守护的环境变量里配置。"
            % (", ".join(missing), os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        )

    try:
        port_int = int(port)
    except ValueError:
        raise SystemExit("OEM_CRAWL_DB_PORT 必须是数字，当前为 %r" % port)

    return {
        "host": host,
        "port": port_int,
        "user": user,
        "password": password,
        "database": name,
    }


def json_dumps_safe(value):
    """把事实字典序列化为 MySQL JSON 可接受的字符串（去掉控制字符）。"""
    def sanitize(item):
        if isinstance(item, str):
            return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", item)
        if isinstance(item, (list, tuple)):
            return [sanitize(entry) for entry in item]
        if isinstance(item, dict):
            return {str(key): sanitize(entry) for key, entry in item.items()}
        return item

    return json.dumps(sanitize(value), ensure_ascii=False)


class PageParser(HTMLParser):
    """HTML 提取：标题、meta、标题层级、正文（含主体/页脚）、结构化链接与图片。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.meta_description = ""
        self.metas = {}
        self.meta_list = []
        self.lang = ""
        self.favicon = ""
        self.links = []
        self.images = []
        self.headings = {"h1": [], "h2": [], "h3": []}
        self.text_parts = []
        self.main_parts = []
        self.main_content_parts = []
        self.description_parts = []
        self.description_found = False
        self.footer_parts = []
        self.jsonld = []
        self.forms = 0
        self._in_title = False
        self._heading_tag = None
        self._heading_buf = []
        self._open_a = None
        self._stack = []
        self._jsonld_buf = None

    def _flags(self, tag, attrs):
        parent = self._stack[-1] if self._stack else {}
        cls = ((attrs.get("class") or "") + " " + (attrs.get("id") or "")).lower()
        tokens = set(cls.split())
        footer = (
            parent.get("footer", False) or tag == "footer"
            or bool(tokens & {"footer", "site-footer", "main-footer", "footer-wrap", "footer-inner"})
        )
        header = (
            parent.get("header", False) or tag == "header"
            or bool(tokens & {"header", "site-header", "main-header", "top-header", "topbar", "top-bar"})
        )
        nav = (
            parent.get("nav", False) or tag == "nav"
            or bool(tokens & {"nav", "navbar", "navigation", "main-nav", "site-nav", "menu", "main-menu", "nav-bar"})
        )
        main = (
            parent.get("main", False) or tag == "main"
            or bool(tokens & {"main-content", "main_content", "maincontent"})
        )
        description = (
            parent.get("description", False)
            or bool(tokens & {
                "description", "product-description", "product__description",
                "product-description-content", "product-detail", "detail-content",
                "detail-desc", "goods-detail", "rich-text", "rte",
                "plugin-product_detail-content", "plugin-product-detail-content",
                "product_detail_content", "product-detail-content",
            })
        )
        skip = parent.get("skip", False) or tag in SKIP_TAGS
        return {"tag": tag, "footer": footer, "header": header, "nav": nav,
                "main": main, "description": description, "skip": skip}

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = {k.lower(): (v or "") for k, v in attrs}
        flags = self._flags(tag, attrs)
        if flags["description"] and not flags["skip"]:
            self.description_found = True

        if tag == "script":
            if "ld+json" in (attrs.get("type") or "").lower():
                flags["skip"] = False
                self._jsonld_buf = []
            else:
                flags["skip"] = True

        if tag == "html":
            self.lang = (attrs.get("lang") or "")[:16]
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            self.meta_list.append(attrs)
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            content = (attrs.get("content") or "")[:1000]
            if name:
                self.metas.setdefault(name, content)
            if name in ("description", "og:description") and not self.meta_description:
                self.meta_description = content
        elif tag == "link":
            rel = (attrs.get("rel") or "").lower()
            if "icon" in rel and attrs.get("href"):
                self.favicon = attrs["href"][:600]
        elif tag == "form":
            self.forms += 1
        elif tag == "a":
            href = (attrs.get("href") or "").strip()
            if self._open_a is not None:
                self._close_link()
            if href and not href.lower().startswith(("javascript:", "#")):
                self._open_a = {
                    "href": href,
                    "text": "",
                    "footer": flags["footer"],
                    "header": flags["header"],
                    "nav": flags["nav"],
                }
        elif tag == "img":
            src = attrs.get("src") or ""
            data_src = (
                attrs.get("data-src") or attrs.get("data-original")
                or attrs.get("data-lazy-src") or attrs.get("data-url") or ""
            )
            if src or data_src:
                self.images.append({
                    "src": src,
                    "data_src": data_src,
                    "alt": attrs.get("alt") or "",
                    "footer": flags["footer"],
                })
        elif tag in self.headings:
            self._heading_tag = tag
            self._heading_buf = []

        if tag in BLOCK_TAGS:
            self.text_parts.append("\n")
            if not flags["header"] and not flags["footer"] and not flags["nav"]:
                self.main_parts.append("\n")
            if flags["main"]:
                self.main_content_parts.append("\n")
            if flags["description"]:
                self.description_parts.append("\n")

        if tag not in VOID_TAGS:
            self._stack.append(flags)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def _close_link(self):
        if self._open_a is None:
            return
        self._open_a["text"] = re.sub(r"\s+", " ", self._open_a.get("text") or "").strip()[:300]
        self.links.append(self._open_a)
        self._open_a = None

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._jsonld_buf is not None:
            raw = "".join(self._jsonld_buf).strip()
            if raw:
                try:
                    self.jsonld.append(json.loads(raw))
                except Exception:
                    pass
            self._jsonld_buf = None
        elif tag == "a":
            self._close_link()
        elif tag in self.headings and self._heading_tag == tag:
            text = re.sub(r"\s+", " ", "".join(self._heading_buf)).strip()
            if text:
                self.headings[tag].append(text[:500])
            self._heading_tag = None
            self._heading_buf = []

        if tag in BLOCK_TAGS:
            self.text_parts.append("\n")
            self.main_parts.append("\n")
            if any(entry.get("main") for entry in self._stack):
                self.main_content_parts.append("\n")
            if any(entry.get("description") for entry in self._stack):
                self.description_parts.append("\n")

        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].get("tag") == tag:
                del self._stack[index:]
                break

    def handle_data(self, data):
        if self._jsonld_buf is not None:
            self._jsonld_buf.append(data)
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._heading_tag:
            self._heading_buf.append(data)
        if self._open_a is not None:
            self._open_a["text"] += data
        if any(entry.get("skip") for entry in self._stack):
            return
        stripped = data.strip()
        if not stripped:
            return
        self.text_parts.append(stripped)
        in_header_footer = any(
            entry.get("header") or entry.get("footer") or entry.get("nav")
            for entry in self._stack
        )
        if not in_header_footer:
            self.main_parts.append(stripped)
        if any(entry.get("main") for entry in self._stack):
            self.main_content_parts.append(stripped)
        if any(entry.get("footer") for entry in self._stack):
            self.footer_parts.append(stripped)
        if any(entry.get("description") for entry in self._stack):
            self.description_parts.append(stripped)

    @property
    def title(self):
        return re.sub(r"\s+", " ", "".join(self.title_parts)).strip()[:500]

    @staticmethod
    def _clean(parts):
        text = "".join(parts)
        text = text.replace("\u00a0", " ").replace("\u200b", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n[ ]+", "\n", text)
        text = re.sub(r"\n{2,}", "\n", text)
        # 去掉单独成行的主题布局词（如首页正文里孤立的 center），
        # 避免被本地/AI 当成占位文案或页面正文。
        text = "\n".join(
            line for line in text.splitlines()
            if not (
                line.strip()
                and len(line.strip()) <= 12
                and line.strip().lower() in LAYOUT_TOKENS
            )
        )
        return text.strip()

    @property
    def text(self):
        return self._clean(self.text_parts)

    @property
    def main_text(self):
        return self._clean(self.main_parts)

    @property
    def main_content_text(self):
        return self._clean(self.main_content_parts)

    @property
    def description_text(self):
        return self._clean(self.description_parts)

    @property
    def footer_text(self):
        return self._clean(self.footer_parts)


def parse_page(html_text, base_url):
    parser = PageParser()
    parser.raw = html_text or ""
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:
        pass
    parser._close_link()
    for link in parser.links:
        href = link.get("href") or ""
        if href.lower().startswith(("http://", "https://", "/", "./", "../")):
            link["url"] = urljoin(base_url, href)
        else:
            link["url"] = ""
    for image in parser.images:
        image["src"] = urljoin(base_url, image["src"]) if image.get("src") else ""
        image["data_src"] = urljoin(base_url, image["data_src"]) if image.get("data_src") else ""
    if parser.favicon:
        parser.favicon = urljoin(base_url, parser.favicon)
    return parser


def clean_visible_content(value):
    """清理模板/标签噪声，作为无法定位 <main> 时的正文兜底。"""
    text = str(value or "")
    text = re.sub(r"<(script|style|template|svg|noscript)\b[^>]*>.*?</\1\s*>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\{%.*?%\}|\{\{.*?\}\}", " ", text, flags=re.S)
    text = html_unescape(text)
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ ]+", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def content_blocks_of(text, headings=None):
    """把正文按行拆成可定位的块，供 AI 判断段落是否粘连/缺失。

    只做结构化拆分，不做任何语义判断；真正的判定仍留在 PHP。
    """
    headings = headings or {}
    heading_lines = set()
    for values in headings.values():
        for value in values or []:
            normalized = re.sub(r"\s+", " ", str(value or "")).strip()
            if normalized:
                heading_lines.add(normalized)

    blocks = []
    for line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        blocks.append({
            "i": len(blocks),
            "tag": "h" if line in heading_lines else "text",
            "text": line[:300],
        })
        if len(blocks) >= 80:
            break
    return blocks


def template_noise_of(html_text):
    """模板噪声计数：用于区分「前台真的显示占位」和「抓到了主题源码」。"""
    raw = str(html_text or "")
    return {
        "liquid_tokens": len(re.findall(r"\{%.*?%\}|\{\{.*?\}\}", raw, re.S)),
        "svg_tags": len(re.findall(r"<svg\b", raw, re.I)),
        "script_tags": len(re.findall(r"<script\b", raw, re.I)),
        "style_tags": len(re.findall(r"<style\b", raw, re.I)),
        "template_tags": len(re.findall(r"<template\b", raw, re.I)),
    }


def text_quality_of(text):
    """正文质量事实：行数、最长行、是否换行，供 AI 判断排版/粘连。"""
    value = str(text or "")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return {
        "chars": len(value),
        "lines": len(lines),
        "has_linebreak": "\n" in value,
        "max_line_chars": max((len(line) for line in lines), default=0),
        "liquid_tokens": len(re.findall(r"\{%.*?%\}|\{\{.*?\}\}", value, re.S)),
        "svg_tokens": len(re.findall(r"<svg\b", value, re.I)),
    }


def build_page_facts(parser, page_type, html_text, final_url):
    footer_links = [link for link in parser.links if link.get("footer")]
    if not footer_links:
        footer_links = parser.links[-30:]
    footer_text = " ".join((link.get("text") or "") for link in footer_links).lower()
    policy_keys = set()
    for link in footer_links:
        key = policy_key((link.get("url") or "") + " " + (link.get("text") or ""))
        if key:
            policy_keys.add(key)

    socials = {}
    for link in parser.links:
        try:
            host = (urlparse(link.get("url") or "").netloc or "").lower().split(":")[0]
        except Exception:
            continue
        for known, name in SOCIAL_HOSTS.items():
            if host == known or host.endswith("." + known):
                socials.setdefault(name, link.get("url") or "")

    payment_hits = set()
    for image in parser.images:
        haystack = " ".join([
            image.get("src") or "", image.get("data_src") or "", image.get("alt") or "",
        ]).lower()
        for key in PAYMENT_KEYS:
            if key in haystack:
                payment_hits.add(key)
    for key in PAYMENT_KEYS:
        if key in footer_text:
            payment_hits.add(key)

    placeholder = []
    for image in parser.images:
        # 懒加载页面的 src 通常是 empty_loading.png，真实图片在 data_src；
        # 有 data_src 时必须以 data_src 为准，否则会把正常商品图全部误报为占位图。
        source = (image.get("data_src") or image.get("src") or "").strip()
        if source and PLACEHOLDER_IMAGE_RE.search(source):
            placeholder.append(image)

    product_info = product_jsonld(parser.jsonld) if page_type == "product" else {}
    if product_info:
        if product_info.get("price") is None:
            meta_price = parser.metas.get("product:price:amount") or parser.metas.get("og:price:amount") or ""
            try:
                product_info["price"] = round(float(meta_price), 2) if str(meta_price).strip() else None
            except ValueError:
                product_info["price"] = None
        if not product_info.get("currency"):
            product_info["currency"] = (
                parser.metas.get("product:price:currency") or parser.metas.get("og:price:currency") or ""
            )[:12]
        if not product_info.get("availability"):
            product_info["availability"] = (parser.metas.get("product:availability") or parser.metas.get("og:availability") or "")[:200]

    dom_description = parser.description_text
    if dom_description:
        description_found = True
        description_text = dom_description
    elif product_info.get("description"):
        description_found = True
        description_text = str(product_info.get("description") or "")
    else:
        description_found = bool(parser.description_found)
        description_text = ""

    main_content = clean_visible_content(parser.main_content_text) or clean_visible_content(parser.main_text or parser.text)

    return {
        "page_type": page_type,
        "final_url": (final_url or "")[:1000],
        "meta_description": parser.meta_description[:600],
        "og_image": (parser.metas.get("og:image") or "")[:600],
        "favicon": parser.favicon[:600],
        "jsonld_types": jsonld_types(parser.jsonld),
        "product": product_info,
        "tracking": tracking_ids(html_text or ""),
        "forms": parser.forms,
        "footer_policies": sorted(policy_keys),
        "footer_link_count": len(footer_links),
        "footer_text": " ".join((link.get("text") or "") for link in footer_links)[:1200],
        "has_copyright": bool(re.search(
            r"©|\(c\)\s*\d{4}|copyright|all rights reserved|版权所有", parser.text, re.I
        )),
        "payment_icons": sorted(payment_hits),
        "social_links": socials,
        "has_cookie_consent": bool(re.search(
            r"cookie[-_ ]?(consent|banner|notice|policy|settings)|onetrust|cookiebot|cookieyes|gdpr|consent\.",
            html_text or "", re.I
        )),
        "review_links": sum(
            1 for link in parser.links
            if re.search(
                r"review|comment|testimonial|评价",
                (link.get("url") or "") + " " + (link.get("text") or ""),
                re.I,
            )
        ),
        "tel_links": sum(1 for link in parser.links if (link.get("href") or "").lower().startswith("tel:")),
        "mailto_links": sum(1 for link in parser.links if (link.get("href") or "").lower().startswith("mailto:")),
        "image_count": len(parser.images),
        "placeholder_images": len(placeholder),
        "html_bytes": len((html_text or "").encode("utf-8")),
        "main_chars": len(main_content),
        "main_content_chars": len(main_content),
        "main_content_text": main_content[:8000],
        "content_blocks": content_blocks_of(main_content, parser.headings),
        "template_noise": template_noise_of(html_text),
        "text_quality": text_quality_of(main_content),
        "description_found": bool(description_found),
        "description_chars": len(description_text),
        "description_text": description_text[:4000],
        "word_count": len(parser.text.split()),
    }


def normalize_url(url, base):
    try:
        url = urljoin(base, url.strip())
    except Exception:
        return ""
    if not re.match(r"^https?://", url, re.I):
        return ""
    parsed = urlparse(url)
    if not parsed.netloc:
        return ""
    # 去掉 fragment 与常见跟踪参数
    query = [
        (k, v) for k, v in
        (item.split("=", 1) if "=" in item else (item, "")
         for item in (parsed.query or "").split("&") if item)
        if not re.match(r"^(utm_|gclid|fbclid|ref|spm)", k, re.I)
    ]
    query.sort()
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        path,
        "",
        "&".join("%s=%s" % (k, v) for k, v in query),
        "",
    ))


def page_type_of(url):
    path = (urlparse(url).path or "/").lower()
    if path in ("", "/"):
        return "home"
    for name, patterns in (
        # 评价页先判：/products/{handle}/reviews 这类商品评价子页不能被当成商品页。
        ("reviews", REVIEW_PATTERNS),
        ("product", PRODUCT_PATTERNS),
        ("collection", COLLECTION_PATTERNS),
        ("contact", CONTACT_PATTERNS),
        ("about", ABOUT_PATTERNS),
        ("policy", POLICY_PATTERNS),
        ("blog", BLOG_PATTERNS),
    ):
        for pattern in patterns:
            if pattern.search(path):
                return name
    return "other"


class Crawler:
    def __init__(self, db, run, settings):
        self.db = db
        self.run = run
        self.settings = settings
        self.host = (urlparse(run["start_url"]).hostname or "").lower()
        self.root_domain = self._root_domain(self.host)
        self.allow_subdomain = bool(run.get("allow_subdomain"))
        self.max_pages = max(1, int(run.get("max_pages") or 5000))
        self.max_depth = max(0, int(run.get("max_depth") or 8))
        self.timeout = max(3, int(run.get("timeout") or 20))
        self.concurrency = max(1, min(32, int(run.get("concurrency") or 8)))
        self.delay_ms = max(0, int(run.get("delay_ms") or 0))
        self.render_js = bool(run.get("render_js"))
        self.screenshot = bool(run.get("screenshot"))
        self.can_render = sync_playwright is not None
        # 代理出口：形如 http://user:pass@host:port 或 socks5://host:port。
        # 用于绕过 Cloudflare 对云厂商 IP 的封锁，未配置则直连。
        self.proxy = (os.environ.get("OEM_CRAWL_PROXY") or "").strip()
        self.home_html = ""
        self.home_final_url = ""
        self.include = self._patterns(run.get("include_patterns"))
        self.exclude = self._patterns(run.get("exclude_patterns"))
        self.stop = threading.Event()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if self.proxy:
            self.session.proxies.update({"http": self.proxy, "https": self.proxy})
        self.robots_rules = []
        self.robots_loaded = False
        self._db_lock = threading.Lock()
        self.page_columns = self._load_columns("oem_crawl_page")
        self.run_columns = self._load_columns("oem_crawl_run")
        self.output_dir = settings["output_dir"]
        self.html_dir = os.path.join(self.output_dir, "html", str(run["id"]))
        self.shot_dir = os.path.join(self.output_dir, "shots", str(run["id"]))
        os.makedirs(self.html_dir, exist_ok=True)
        os.makedirs(self.shot_dir, exist_ok=True)

    @staticmethod
    def _root_domain(host):
        parts = (host or "").split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host

    @staticmethod
    def _patterns(value):
        if not value:
            return []
        return [p.strip() for p in str(value).split(",") if p.strip()]

    def load_robots(self):
        """解析 robots.txt 的 Disallow 规则，请求失败时视为允许。"""
        if self.robots_loaded or not self.run.get("respect_robots"):
            return
        self.robots_loaded = True
        base = "%s://%s" % (urlparse(self.run["start_url"]).scheme, self.host)
        try:
            resp = self.session.get(base + "/robots.txt", timeout=self.timeout)
            if resp.status_code != 200:
                return
        except Exception:
            return

        active = False
        for line in (resp.text or "").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = [part.strip() for part in line.split(":", 1)]
            key = key.lower()
            if key == "user-agent":
                active = value == "*"
            elif key == "disallow" and active and value:
                pattern = re.escape(value).replace(r"\*", ".*")
                try:
                    self.robots_rules.append(re.compile("^" + pattern, re.I))
                except re.error:
                    continue

    def robots_blocked(self, url):
        if not self.robots_rules:
            return False
        path = urlparse(url).path or "/"
        return any(rule.search(path) for rule in self.robots_rules)

    def allowed_url(self, url):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        if self.allow_subdomain:
            if self._root_domain(host) != self.root_domain:
                return False
        elif host != self.host:
            return False
        path = parsed.path or "/"
        if SKIP_EXT.search(path) or SKIP_PATTERNS.search(path):
            return False
        # 评价页默认不抓取：新站通常没有评价页，或只有空模板页；
        # 抓取和分析都不会产生有效结论，还会占用抓取配额和 AI 预算。
        if any(pattern.search(path) for pattern in REVIEW_PATTERNS):
            return False
        if self.include and not any(re.search(p, url, re.I) for p in self.include):
            return False
        if self.exclude and any(re.search(p, url, re.I) for p in self.exclude):
            return False
        if self.robots_blocked(url):
            return False
        return True

    # -- DB -------------------------------------------------------------
    def _load_columns(self, table):
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                    (table,),
                )
                return {row["name"] for row in cur.fetchall()}
        except Exception:
            return set()

    def db_exec(self, sql, args=None):
        with self._db_lock:
            with self.db.cursor() as cur:
                cur.execute(sql, args or ())
            self.db.commit()

    def db_fetchall(self, sql, args=None):
        with self._db_lock:
            with self.db.cursor() as cur:
                cur.execute(sql, args or ())
                return cur.fetchall()

    def url_state_counts(self):
        rows = self.db_fetchall(
            "SELECT state, COUNT(*) AS c FROM oem_crawl_url WHERE run_id=%s GROUP BY state",
            (self.run["id"],),
        )
        counts = {str(row["state"]): int(row["c"]) for row in rows}
        counts["total"] = sum(counts.values())
        return counts

    def heartbeat(self, status=None, **extra):
        fields = ["heartbeat_at = %s", "worker_id = %s"]
        args = [now(), WORKER_ID]
        for key, value in extra.items():
            fields.append("`%s` = %%s" % key)
            args.append(value)
        if status:
            fields.append("status = %s")
            args.append(status)
        args.append(self.run["id"])
        self.db_exec(
            "UPDATE oem_crawl_run SET %s WHERE id = %%s" % ", ".join(fields),
            args,
        )

    def enqueue(self, url, depth, priority=0):
        """入队并返回是否真的新增。

        同一个 URL 在页面里可能以多个 <a> 重复出现（商品卡片常见），
        INSERT IGNORE 会丢弃重复行，此时必须返回 False，否则调用方会把
        重复链接当成“新增”计入预算。
        """
        if not self.allowed_url(url):
            return False
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        with self._db_lock:
            with self.db.cursor() as cur:
                cur.execute(
                    "INSERT IGNORE INTO oem_crawl_url "
                    "(run_id, url, url_hash, depth, page_type, priority, state, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,%s)",
                    (self.run["id"], url[:1000], url_hash, depth, page_type_of(url), priority, now(), now()),
                )
                inserted = cur.rowcount > 0
            self.db.commit()
        return inserted

    def skip_review_urls(self):
        """把已有任务中尚未抓取的评价类 URL 标记跳过，避免升级后继续抓取。

        按 URL 路径重新匹配，而不是只看 page_type：升级前入队的
        /products/{handle}/reviews 可能被旧规则标成 product，同样要跳过。
        """
        rows = self.db_fetchall(
            "SELECT id, url, page_type FROM oem_crawl_url WHERE run_id=%s AND state='pending'",
            (self.run["id"],),
        )
        ids = []
        for row in rows:
            path = urlparse(row["url"] or "").path or "/"
            if row.get("page_type") == "reviews" or any(p.search(path) for p in REVIEW_PATTERNS):
                ids.append(row["id"])
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join(["%s"] * len(chunk))
            self.db_exec(
                "UPDATE oem_crawl_url SET state='skipped', error='评价页不抓取', updated_at=%s "
                "WHERE run_id=%s AND state='pending' AND id IN (" + placeholders + ")",
                tuple([now(), self.run["id"]] + chunk),
            )
        if ids:
            log("评价页 URL 已跳过 %d 个" % len(ids))

    def claim_url(self):
        """多 worker 安全领取；FOR UPDATE SKIP LOCKED 避免互相等待。"""
        with self._db_lock:
            with self.db.cursor() as cur:
                self.db.begin()
                cur.execute(
                    "SELECT id, url, depth FROM oem_crawl_url "
                    "WHERE run_id = %s AND state = 'pending' "
                    "ORDER BY priority DESC, id ASC LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (self.run["id"],),
                )
                row = cur.fetchone()
                if not row:
                    self.db.commit()
                    return None
                cur.execute(
                    "UPDATE oem_crawl_url SET state='fetching', worker_id=%s, locked_at=%s, updated_at=%s WHERE id=%s",
                    (WORKER_ID, now(), now(), row["id"]),
                )
                self.db.commit()
                return row

    def finish_url(self, url_id, state, status=0, error=""):
        self.db_exec(
            "UPDATE oem_crawl_url SET state=%s, http_status=%s, error=%s, updated_at=%s WHERE id=%s",
            (state, status, (error or "")[:500], now(), url_id),
        )

    @staticmethod
    def _parse(html_text):
        parser = PageParser()
        try:
            if html_text:
                parser.feed(html_text)
                parser.close()
        except Exception:
            pass
        return parser

    def save_page(self, url_row, final_url, status, html_text, parser, rendered, shot_path,
                  page_status=None, error="", analyze_status="pending"):
        # 页面正文优先取 <main>/main-content，避免把公共头部、页脚、隐藏购物车
        # 和 Liquid 模板代码当成正文送去分析和查重。
        text = parser.main_content_text or clean_visible_content(parser.main_text or parser.text)
        main_text = text
        content_hash = hashlib.md5(re.sub(r"\s+", "", text.lower()).encode("utf-8")).hexdigest() if text else ""
        html_path = ""
        if html_text:
            name = hashlib.md5(url_row["url"].encode("utf-8")).hexdigest()
            html_path = os.path.join(self.html_dir, name + ".html")
            try:
                with open(html_path, "w", encoding="utf-8", errors="ignore") as f:
                    f.write(html_text[:3_000_000])
            except Exception:
                html_path = ""

        page_type = page_type_of(url_row["url"])
        facts = build_page_facts(parser, page_type, html_text, final_url)

        if page_status is None:
            visible = main_text or text
            page_status = "ok" if visible and len(visible) >= 40 else ("empty" if status and status < 400 else "error")
        # blocked 页不参与 AI 分析，直接置为 skipped，避免看起来像“待分析”。
        if analyze_status == "pending" and page_status == "blocked":
            analyze_status = "skipped"

        columns = [
            "run_id", "url_id", "url", "final_url", "page_type", "http_status", "title",
            "meta_description", "h1", "headings", "content_text", "word_count", "content_hash",
            "images", "links", "html_path", "screenshot_path", "render_used", "lang",
            "status", "error", "analyze_status", "fetched_at", "created_at", "updated_at",
        ]
        values = [
            self.run["id"], url_row["id"], url_row["url"][:1000], (final_url or "")[:1000],
            page_type, int(status or 0), parser.title, parser.meta_description,
            (parser.headings["h1"][0] if parser.headings["h1"] else "")[:500],
            json.dumps(parser.headings, ensure_ascii=False),
            text, len(text.split()), content_hash,
            json.dumps(parser.images[:80], ensure_ascii=False),
            json.dumps(parser.links[:300], ensure_ascii=False),
            html_path[:500], (shot_path or "")[:500], 1 if rendered else 0,
            parser.lang, page_status, (error or "")[:500], analyze_status, now(), now(), now(),
        ]
        if "main_text" in self.page_columns:
            columns.append("main_text")
            values.append(main_text)
        if "page_facts" in self.page_columns:
            columns.append("page_facts")
            values.append(json_dumps_safe(facts))

        placeholders = ",".join(["%s"] * len(columns))
        updates = ", ".join(
            "%s=VALUES(%s)" % (column, column)
            for column in columns
            if column not in ("run_id", "url_id", "created_at")
        )
        self.db_exec(
            "INSERT INTO oem_crawl_page (%s) VALUES (%s) "
            "ON DUPLICATE KEY UPDATE %s" % (", ".join(columns), placeholders, updates),
            tuple(values),
        )
        return page_status

    # -- 抓取 ------------------------------------------------------------
    def fetch(self, url, ua=None):
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000.0)
        headers = {"User-Agent": ua} if ua else None
        resp = self.session.get(url, timeout=self.timeout, allow_redirects=True, headers=headers)
        content_type = (resp.headers.get("content-type") or "").lower()
        if "html" not in content_type and "xml" not in content_type:
            return resp.status_code, resp.url, "", None
        text = resp.text or ""
        return resp.status_code, resp.url, text, resp

    def render(self, url, force_shot=False):
        """Playwright 渲染 + 截图；带反自动化指纹，可配代理；失败返回 None。"""
        if sync_playwright is None:
            return None
        want_shot = self.screenshot or force_shot
        try:
            with sync_playwright() as p:
                launch_kwargs = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                }
                if self.proxy:
                    launch_kwargs["proxy"] = {"server": self.proxy}
                browser = p.chromium.launch(**launch_kwargs)
                context = browser.new_context(
                    user_agent=UA,
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                    timezone_id="America/Los_Angeles",
                )
                context.add_init_script(STEALTH_JS)
                page = context.new_page()
                if _stealth_sync is not None:
                    try:
                        _stealth_sync(page)
                    except Exception:
                        pass
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                except Exception as exc:
                    log("渲染 goto 超时/失败 %s: %s" % (url, exc))
                # 给人机挑战留出自动通过的时间
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                # Cloudflare 的 "Just a moment..." 有时会在 JS 验证后延迟跳转，最多再等 8 秒。
                for _ in range(8):
                    try:
                        page_title = (page.title() or "").lower()
                    except Exception:
                        break
                    if "just a moment" not in page_title and "security verification" not in page_title:
                        break
                    try:
                        page.wait_for_timeout(1000)
                    except Exception:
                        break
                html_text = page.content()
                shot_path = ""
                if want_shot:
                    name = hashlib.md5(url.encode("utf-8")).hexdigest()
                    shot_path = os.path.join(self.shot_dir, name + ".jpg")
                    try:
                        page.screenshot(path=shot_path, full_page=False, type="jpeg", quality=70)
                    except Exception:
                        shot_path = ""
                context.close()
                browser.close()
                return html_text, shot_path
        except Exception as exc:
            log("渲染失败 %s: %s" % (url, exc))
            return None

    def process(self, url_row):
        url = url_row["url"]
        # 兜底：升级前入队、断点恢复后重新回到 pending 的评价页不再发起请求。
        path = urlparse(url).path or "/"
        if any(pattern.search(path) for pattern in REVIEW_PATTERNS):
            self.finish_url(url_row["id"], "skipped", 0, "评价页不抓取")
            return "skipped"
        status, final_url, html_text, resp = 0, url, "", None
        rendered = False
        shot_path = ""

        try:
            if (self.home_html and page_type_of(url) == "home"
                    and url.rstrip("/") == (self.run["start_url"] or "").rstrip("/")):
                # 直接复用站点事实阶段成功抓到的首页 HTML，避免第二次请求被 Cloudflare 挑战。
                status, final_url, html_text, resp = 200, self.home_final_url or url, self.home_html, None
            else:
                status, final_url, html_text, resp = self.fetch(url)
        except Exception as exc:
            self.finish_url(url_row["id"], "failed", 0, str(exc))
            return "failed"

        parser = parse_page(html_text, final_url)
        blocked, block_reason = detect_block(status, html_text, parser.title)
        server_error = status < 400 and detect_server_error(status, html_text, parser.title)

        # 目标站可能对某个 UA/瞬时请求返回 500 或验证页；先用不同 UA 重试，
        # 避免一次瞬时错误把整个任务判失败。
        if status == 0 or status >= 500 or blocked or server_error:
            for attempt, retry_ua in enumerate(UA_LADDER[1:], start=1):
                time.sleep(min(5, attempt * 2))
                try:
                    r_status, r_final, r_html, r_resp = self.fetch(url, ua=retry_ua)
                except Exception as exc:
                    log("重试抓取失败 %s（%s）：%s" % (url, retry_ua[:30], exc))
                    continue
                r_parser = parse_page(r_html, r_final)
                r_blocked, r_reason = detect_block(r_status, r_html, r_parser.title)
                r_error = r_status < 400 and detect_server_error(r_status, r_html, r_parser.title)
                if not r_blocked and not r_error and r_status < 400:
                    status, final_url, html_text, resp = r_status, r_final, r_html, r_resp
                    parser = r_parser
                    blocked, block_reason = r_blocked, r_reason
                    server_error = False
                    log("重试成功 %s（UA=%s）" % (url, retry_ua[:30]))
                    break

        if (blocked or server_error) and self.can_render:
            # 被 WAF 拦截或站点以 HTTP 200 返回错误页时，强制用真实浏览器重试一次，
            # 不受 render_js 开关限制（浏览器指纹可能拿到正常页面）。
            result = self.render(url, force_shot=True)
            if result:
                html_text, shot_path = result
                rendered = True
                parser = parse_page(html_text, final_url)
                blocked, block_reason = detect_block(status, html_text, parser.title)
                server_error = status < 400 and detect_server_error(status, html_text, parser.title)
        elif self.render_js and self.can_render and len(parser.main_text) < 200:
            # 主体正文过短（常见于 JS 渲染站）才用浏览器重新抓取；
            # 不要无条件渲染首页，否则浏览器拿到 500/验证页时会覆盖已成功的 HTTP 页面。
            result = self.render(url)
            if result:
                html_text, shot_path = result
                rendered = True
                parser = parse_page(html_text, final_url)
                blocked, block_reason = detect_block(status, html_text, parser.title)
                server_error = status < 400 and detect_server_error(status, html_text, parser.title)

        if blocked:
            # 拦截页不入正文分析：标记 blocked，PHP analyze 只取 status='ok' 会自动跳过。
            # 也不从验证页里发现链接（链接无意义）。
            self.save_page(url_row, final_url, status, html_text, parser, rendered, shot_path,
                           page_status="blocked", error=block_reason or "blocked")
            self.finish_url(url_row["id"], "blocked", status, block_reason or "blocked")
            return "blocked"

        if server_error:
            # 站点/网关把错误页以 HTTP 200 返回（例如 nginx 500 页面），不能当正常页送分析。
            reason = "HTTP %s 但正文是错误页" % status
            self.save_page(url_row, final_url, status, html_text, parser, rendered, shot_path,
                           page_status="error", error=reason, analyze_status="skipped")
            self.finish_url(url_row["id"], "failed", status, reason)
            return "failed"

        if status == 0 or status >= 400:
            self.finish_url(url_row["id"], "failed", status, "HTTP %s" % status)
            return "failed"

        self.save_page(url_row, final_url, status, html_text, parser, rendered, shot_path)

        # 发现新链接（BFS，受 max_depth / max_pages 限制）
        depth = int(url_row["depth"] or 0)
        if depth < self.max_depth:
            added = 0
            seen_links = set()
            for link in parser.links:
                if self.stop.is_set():
                    break
                href = link.get("url") or normalize_url(link.get("href") or "", final_url)
                if not href:
                    continue
                norm = normalize_url(href, final_url)
                if not norm or norm in seen_links:
                    continue
                seen_links.add(norm)
                if not self.allowed_url(norm):
                    continue
                # 预算只统计“真正新增”的 URL：同一商品卡片的多个 <a> 指向同一 URL，
                # 旧逻辑把它们全部计入，导致预算被重复链接吃光后整页中断，
                # 后面的商品与分页链接（?page=N）被静默丢弃。
                if self.enqueue(norm, depth + 1, self.priority_of(norm)):
                    added += 1
                if added >= 200:
                    break

        self.finish_url(url_row["id"], "done", status, "")
        return "done"

    def priority_of(self, url):
        return {
            "home": 100,
            "product": 80,
            "collection": 60,
            "reviews": 55,
            "policy": 50,
            "contact": 50,
            "about": 40,
        }.get(page_type_of(url), 10)

    # -- sitemap ---------------------------------------------------------
    def seed_sitemap(self):
        base = "%s://%s" % (urlparse(self.run["start_url"]).scheme, self.host)
        candidates = [
            base + "/sitemap.xml",
            base + "/sitemap_index.xml",
            base + "/sitemap-index.xml",
        ]
        found = 0
        for candidate in candidates:
            try:
                status, _, text, _ = self.fetch(candidate)
            except Exception:
                continue
            if status != 200 or not text:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
            for loc in locs[:50]:
                loc = loc.strip()
                if loc.endswith(".xml") or "/sitemap" in loc:
                    try:
                        s_status, _, s_text, _ = self.fetch(loc)
                        if s_status == 200 and s_text:
                            for sub in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", s_text)[:self.max_pages]:
                                if self.enqueue(sub.strip(), 2, self.priority_of(sub.strip())):
                                    found += 1
                    except Exception:
                        continue
                elif self.enqueue(loc, 2, self.priority_of(loc)):
                    found += 1
                if found >= self.max_pages:
                    break
            if found:
                break
        return found

    # -- 站点级事实 -------------------------------------------------------
    def run_status(self):
        try:
            rows = self.db_fetchall("SELECT status FROM oem_crawl_run WHERE id=%s", (self.run["id"],))
            return str(rows[0]["status"]) if rows else ""
        except Exception:
            return ""

    def cancelled_by_user(self):
        status = self.run_status()
        return status not in ("", "pending", "running")

    def save_site_facts(self, facts):
        if "site_facts" not in getattr(self, "run_columns", set()):
            return
        try:
            self.db_exec(
                "UPDATE oem_crawl_run SET site_facts=%s WHERE id=%s",
                (json_dumps_safe(facts), self.run["id"]),
            )
        except Exception as exc:
            log("写入站点事实失败：%s" % exc)

    def collect_site_facts(self):
        """采集域名/TLS/DNS/robots/sitemap/headers 等站点级事实，供 PHP 规则判断。"""
        scheme = urlparse(self.run["start_url"]).scheme or "https"
        base = "%s://%s" % (scheme, self.host)
        root = self._root_domain(self.host)
        facts = {"host": self.host, "root_domain": root, "base_url": base, "collected_at": now()}

        try:
            resp = self.session.get(base, timeout=self.timeout, allow_redirects=True)
            headers = {key.lower(): value for key, value in resp.headers.items()}
            blocked, block_reason = detect_block(resp.status_code, resp.text or "", "")
            facts["home"] = {
                "status": resp.status_code,
                "final_url": resp.url,
                "blocked": bool(blocked),
                "block_reason": block_reason[:200] if blocked else "",
                "html_bytes": len(resp.content or b""),
                "elapsed_ms": int(resp.elapsed.total_seconds() * 1000) if resp.elapsed else None,
                "headers": {
                    key: headers.get(key, "")
                    for key in (
                        "strict-transport-security", "content-security-policy",
                        "x-content-type-options", "x-frame-options", "referrer-policy", "server",
                    )
                },
            }
            home_html = resp.text or ""
            if (resp.status_code == 200 and not blocked
                    and len(home_html) > 1000
                    and not detect_server_error(resp.status_code, home_html, "")):
                # 复用这次成功抓到的首页，避免首页被 Cloudflare 第二次挑战。
                self.home_html = home_html
                self.home_final_url = resp.url
        except Exception as exc:
            facts["home"] = {"error": str(exc)[:300]}
        home_blocked = bool(isinstance(facts.get("home"), dict) and facts["home"].get("blocked"))

        robots = {"status": 0, "disallow_all": False, "has_sitemap": False, "snippet": ""}
        if home_blocked:
            robots["error"] = "首页被人机验证拦截，未采集 robots"
            facts["robots"] = robots
        else:
            try:
                resp = self.session.get(base + "/robots.txt", timeout=self.timeout)
                robots["status"] = resp.status_code
                text = resp.text or ""
                robots["snippet"] = text[:2000]
                robots["has_sitemap"] = "sitemap:" in text.lower()
                active = False
                for line in text.splitlines():
                    line = line.split("#", 1)[0].strip()
                    if ":" not in line:
                        continue
                    key, value = [part.strip() for part in line.split(":", 1)]
                    if key.lower() == "user-agent":
                        active = value == "*"
                    elif key.lower() == "disallow" and active and value == "/":
                        robots["disallow_all"] = True
            except Exception as exc:
                robots["error"] = str(exc)[:300]
            facts["robots"] = robots

        sitemap = {"found": False, "status": 0, "url_count": 0, "url": "", "error": "", "is_index": False}
        if home_blocked:
            sitemap["error"] = "首页被人机验证拦截，未采集 sitemap"
        for candidate in ([] if home_blocked else (base + "/sitemap.xml", base + "/sitemap_index.xml", base + "/sitemap-index.xml")):
            try:
                resp = self.session.get(candidate, timeout=self.timeout)
            except Exception as exc:
                sitemap["error"] = str(exc)[:300]
                continue
            sitemap["status"] = resp.status_code
            if resp.status_code == 200 and "<loc" in (resp.text or "").lower():
                sitemap["found"] = True
                sitemap["url"] = candidate
                sitemap["url_count"] = len(re.findall(r"<loc>", resp.text or "", re.I))
                sitemap["is_index"] = "<sitemapindex" in (resp.text or "").lower()
                sitemap["error"] = ""
                break
        facts["sitemap"] = sitemap

        dns = {"ip": ""}
        try:
            dns["ip"] = socket.gethostbyname(self.host)
        except Exception:
            pass
        mx = doh_query(root, "MX", session=self.session)
        dns["mx"] = None if mx is None else [str(item.get("data", "")) for item in mx]
        txt = doh_query(root, "TXT", session=self.session)
        dns["spf"] = None if txt is None else any(
            "v=spf1" in str(item.get("data", "")).lower() for item in txt
        )
        dmarc = doh_query("_dmarc." + root, "TXT", session=self.session)
        dns["dmarc"] = None if dmarc is None else any(
            "v=dmarc1" in str(item.get("data", "")).lower() for item in dmarc
        )
        facts["dns"] = dns

        facts["tls"] = tls_info(self.host)
        facts["domain"] = rdap_info(root, session=self.session)

        wayback = {"captures": None, "closest": ""}
        try:
            resp = self.session.get(
                "https://archive.org/wayback/available",
                params={"url": root},
                timeout=8,
            )
            if resp.status_code == 200:
                closest = ((resp.json() or {}).get("archived_snapshots") or {}).get("closest") or {}
                wayback["closest"] = str(closest.get("timestamp") or "")
                wayback["captures"] = 1 if closest.get("available") else 0
        except Exception:
            pass
        facts["wayback"] = wayback

        return facts

    def run_loop(self):
        # 每次领取任务时重新读列，避免 worker 常驻进程在 SQL 加列后仍用旧列清单。
        self.page_columns = self._load_columns("oem_crawl_page")
        self.run_columns = self._load_columns("oem_crawl_run")
        self.load_robots()
        if os.environ.get("OEM_CRAWL_SITE_FACTS", "1") != "0" and "site_facts" in self.run_columns:
            try:
                facts = self.collect_site_facts()
                self.save_site_facts(facts)
                log("站点事实已采集：%s" % self.host)
            except Exception as exc:
                log("站点事实采集失败（不影响抓取）：%s" % exc)
        discovered = self.seed_sitemap() if self.run.get("discover_sitemap") else 0
        log("sitemap 发现 %d 个 URL" % discovered)
        try:
            self.skip_review_urls()
        except Exception as exc:
            log("评价页 URL 跳过失败（不影响抓取，分析阶段仍会跳过）：%s" % exc)
        self.heartbeat("running", started_at=self.run.get("started_at") or now())

        fetched = 0
        failed = 0
        blocked_count = 0
        started = time.time()

        while not self.stop.is_set():
            status = self.run_status()
            if status not in ("", "pending", "running"):
                if status == "cancelled":
                    log("任务 %s 已被取消，worker 停止抓取" % self.run["id"])
                else:
                    log("任务 %s 已是终态(%s)，worker 停止抓取" % (self.run["id"], status))
                return
            # 控制总量
            count = self.db_fetchall(
                "SELECT COUNT(*) AS c FROM oem_crawl_url WHERE run_id=%s", (self.run["id"],)
            )[0]["c"]
            if count >= self.max_pages:
                with self._db_lock:
                    with self.db.cursor() as cur:
                        cur.execute(
                            "UPDATE oem_crawl_url SET state='skipped', error='超过 max_pages' "
                            "WHERE run_id=%s AND state='pending' AND depth > 0 LIMIT %s",
                            (self.run["id"], max(1, count - self.max_pages + 100)),
                        )
                    self.db.commit()

            batch = []
            for _ in range(self.concurrency * 2):
                row = self.claim_url()
                if not row:
                    break
                batch.append(row)

            if not batch:
                counts = self.url_state_counts()
                if counts.get("pending", 0) > 0:
                    time.sleep(0.5)
                    continue
                if counts.get("fetching", 0) > 0:
                    # 其他 worker 正在处理最后几个 URL：等它结束，不要抢先写 finished。
                    waited = 0
                    while counts.get("fetching", 0) > 0 and waited < 60 and not self.stop.is_set():
                        time.sleep(1)
                        waited += 1
                        counts = self.url_state_counts()
                    if counts.get("fetching", 0) > 0:
                        log("其他 worker 仍在抓取，本进程退出且不写 finished")
                        return
                    if counts.get("pending", 0) > 0:
                        time.sleep(0.5)
                        continue
                break

            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = {pool.submit(self.process, row): row for row in batch}
                for future in as_completed(futures):
                    try:
                        state = future.result()
                    except Exception as exc:
                        row = futures[future]
                        self.finish_url(row["id"], "failed", 0, str(exc))
                        state = "failed"
                    if state == "done":
                        fetched += 1
                    elif state == "failed":
                        failed += 1
                    elif state == "blocked":
                        blocked_count += 1

            counts = self.url_state_counts()
            self.heartbeat(
                "running",
                total_fetched=counts.get("done", 0),
                total_failed=counts.get("failed", 0),
                total_skipped=counts.get("skipped", 0),
                total_discovered=counts.get("total", 0),
            )
            log("进度 已抓 %d / 失败 %d / 拦截 %d / 待抓 %d / 用时 %.1fs"
                % (counts.get("done", 0), counts.get("failed", 0), blocked_count,
                   counts.get("pending", 0), time.time() - started))

        status = self.run_status()
        if status == "cancelled":
            log("任务 %s 已被取消，不写回 finished" % self.run["id"])
            return
        if status in ("finished", "failed"):
            log("任务 %s 已由其他 worker 结束(%s)，本进程退出" % (self.run["id"], status))
            return

        counts = self.url_state_counts()
        done = counts.get("done", 0)
        failed = counts.get("failed", 0)
        blocked = counts.get("blocked", 0)
        if done == 0 and (failed > 0 or blocked > 0):
            self.heartbeat(
                "failed",
                finished_at=now(),
                total_fetched=0,
                total_failed=failed,
                total_skipped=counts.get("skipped", 0),
                total_discovered=counts.get("total", 0),
                error="全部 URL 抓取失败或被拦截：目标站错误页/人机验证或 worker 出口异常",
            )
            log("任务 %s 失败：抓取 0，失败 %d，拦截 %d（目标站错误页/人机验证）"
                % (self.run["id"], failed, blocked))
            return

        self.heartbeat(
            "finished",
            finished_at=now(),
            total_fetched=done,
            total_failed=failed,
            total_skipped=counts.get("skipped", 0),
            total_discovered=counts.get("total", 0),
        )
        log("任务 %s 完成：抓取 %d，失败 %d" % (self.run["id"], done, failed))


def load_settings():
    output_dir = os.environ.get("OEM_CRAWL_OUTPUT_DIR") or os.path.join(os.path.dirname(__file__), "storage")
    os.makedirs(output_dir, exist_ok=True)
    return {"output_dir": output_dir}


def connect_db(settings=None):
    settings = settings or db_settings()
    return pymysql.connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        database=settings["database"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=10,
        read_timeout=120,
        write_timeout=120,
    )


def reset_stale_urls(db, run_id):
    """worker 崩溃后，把长时间锁定的 URL 放回 pending。"""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE oem_crawl_url SET state='pending', worker_id='', locked_at=NULL "
            "WHERE run_id=%s AND state='fetching' AND locked_at < DATE_SUB(NOW(), INTERVAL 15 MINUTE)",
            (run_id,),
        )
    db.commit()


_DB_RETRYABLE = (2003, 2006, 2013, 2055)


def is_conn_lost(exc):
    """判断是否属于连接层错误，可以重连。"""
    if isinstance(exc, (pymysql.err.OperationalError, pymysql.err.InterfaceError)):
        code = exc.args[0] if exc.args else 0
        return code in _DB_RETRYABLE or isinstance(exc, pymysql.err.InterfaceError)
    return False


def reclaim_stale_runs(db, run_id=None, minutes=None):
    """
    回收僵尸任务：worker 被杀掉后 run 会卡在 running。

    只有心跳超过 N 分钟（默认 10）才回收，避免抢走正在跑的任务。
    """
    if minutes is None:
        try:
            minutes = int(os.environ.get("OEM_CRAWL_STALE_MINUTES", DEFAULT_STALE_MINUTES))
        except ValueError:
            minutes = DEFAULT_STALE_MINUTES
    sql = (
        "UPDATE oem_crawl_run SET status='pending', worker_id='' "
        "WHERE status='running' AND (heartbeat_at IS NULL OR heartbeat_at < DATE_SUB(NOW(), INTERVAL %s MINUTE))"
    )
    args = [minutes]
    if run_id:
        sql += " AND id=%s"
        args.append(run_id)
    with db.cursor() as cur:
        cur.execute(sql, args)
    db.commit()


def claim_run(db, run_id=None):
    """原子领取 pending 任务；多个 worker 同时启动时只有一个能领到。"""
    with db.cursor() as cur:
        try:
            db.begin()
            if run_id:
                cur.execute(
                    "SELECT * FROM oem_crawl_run WHERE id=%s AND status='pending' "
                    "FOR UPDATE SKIP LOCKED",
                    (run_id,),
                )
            else:
                cur.execute(
                    "SELECT * FROM oem_crawl_run WHERE status='pending' "
                    "ORDER BY id ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
            run = cur.fetchone()
            if not run:
                db.commit()
                return None
            stamp = now()
            cur.execute(
                "UPDATE oem_crawl_run SET status='running', worker_id=%s, heartbeat_at=%s, "
                "started_at=COALESCE(started_at, %s) WHERE id=%s",
                (WORKER_ID, stamp, stamp, run["id"]),
            )
            db.commit()
            run["status"] = "running"
            run["worker_id"] = WORKER_ID
            run["heartbeat_at"] = stamp
            return run
        except Exception:
            db.rollback()
            raise


def main():
    parser = argparse.ArgumentParser(description="OEM 全站爬虫 worker")
    parser.add_argument("--once", action="store_true", help="只执行一个任务后退出")
    parser.add_argument("--run-id", type=int, default=0, help="指定任务 ID")
    parser.add_argument("--poll", type=int, default=5, help="空闲轮询间隔秒")
    args = parser.parse_args()

    load_dotenv()
    settings = load_settings()
    db_config = db_settings()
    log(
        "连接数据库 %s:%s/%s（用户 %s）"
        % (db_config["host"], db_config["port"], db_config["database"], db_config["user"])
    )
    db = connect_db(db_config)

    def handle_signal(signum, frame):
        log("收到信号 %s，准备退出" % signum)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while True:
        try:
            reclaim_stale_runs(db, args.run_id or None)
            run = claim_run(db, args.run_id or None)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if not is_conn_lost(exc):
                raise
            log("数据库连接断开（%s），5 秒后重连" % exc)
            time.sleep(5)
            try:
                db = connect_db(db_config)
            except Exception as reconnect_exc:
                log("重连失败：%s" % reconnect_exc)
                time.sleep(10)
            continue

        if not run:
            if args.once:
                log("没有待执行任务")
                return
            time.sleep(args.poll)
            continue

        log("领取任务 %s：%s" % (run["id"], run["start_url"]))
        try:
            reset_stale_urls(db, run["id"])
            crawler = Crawler(db, run, settings)
            crawler.run_loop()
        except KeyboardInterrupt:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE oem_crawl_run SET status='pending', worker_id='' WHERE id=%s",
                    (run["id"],),
                )
            db.commit()
            log("任务 %s 已释放回 pending" % run["id"])
            return
        except Exception as exc:
            log("任务 %s 失败：%s" % (run["id"], exc))
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE oem_crawl_run SET status='failed', error=%s, finished_at=%s WHERE id=%s",
                        (str(exc)[:1000], now(), run["id"]),
                    )
                db.commit()
            except Exception as update_exc:
                log("写回失败状态出错：%s" % update_exc)
            if is_conn_lost(exc):
                try:
                    db = connect_db(db_config)
                except Exception as reconnect_exc:
                    log("重连失败：%s" % reconnect_exc)
                    time.sleep(10)

        if args.once or args.run_id:
            return


if __name__ == "__main__":
    main()
