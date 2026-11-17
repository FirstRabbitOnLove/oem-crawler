#!/usr/bin/env python3
"""市场基准研究 worker（无需 Amazon/eBay API 凭据）。

用 Playwright 打开公开的 eBay 搜索结果和商品页，提取同品类商品的价格、
标题、规格、描述长度，计算 P25/中位数/P75/P90 和规格覆盖率，写入本地 JSON
缓存，并可选生成导入 oem_market_benchmark 的 SQL。

这不是每次分析都实时联网：同一 category_key 的缓存有效期内，PHP 规则直接读
缓存/数据库；缓存过期或样本不足时才重新研究。
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, unquote, urlparse

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 公开 Shopify 店铺；/products.json 和 /meta.json 不需要 API Key。
SHOPIFY_SEEDS = [
    "your-store.myshopify.com",
    "your-second-store.myshopify.com",
]
SOURCE_REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_sources.json")
SOURCE_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_sources_state.json")

SIZE_RE = re.compile(r"\b(?:size|sizes?)\b\s*[:：]?\s*[A-Z0-9/,\-]+|尺码|尺寸", re.I)
COLOR_RE = re.compile(
    r"\b(?:black|white|pink|blue|red|gray|grey|beige|green|purple|floral|flamingo|stripe|striped|print)\b|颜色",
    re.I,
)
PAJAMA_RE = re.compile(r"\b(?:pajama|pajamas|pyjama|pyjamas|sleepwear|nightwear|loungewear|sleep\s*set)\b|睡衣|家居服", re.I)
EXCLUDE_RE = re.compile(
    r"\b(?:nightgown|chemise|babydoll|robe|for parts|used|pre-owned|preowned|lot of|wholesale)\b|二手|旧|批量",
    re.I,
)
PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")


def percentile(values, p):
    values = sorted(float(v) for v in values if v is not None)
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 2)
    k = (len(values) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    fraction = k - lo
    return round(values[lo] * (1 - fraction) + values[hi] * fraction, 2)


def category_key(query, titles):
    text = (query + " " + " ".join(titles)).lower()
    audience = ""
    for word in ("baby", "toddler", "kids", "children", "girls", "boys"):
        if re.search(r"\b" + word + r"\b", text):
            audience = "kids"
            break
    if audience == "":
        if re.search(r"\bwomen|womens|ladies\b", text):
            audience = "women"
        elif re.search(r"\bmen|mens\b", text):
            audience = "men"
    material = ""
    if re.search(r"\bsatin|silk|silky\b|缎|丝绸", text):
        material = "satin"
    elif re.search(r"\bcotton\b|棉", text):
        material = "cotton"
    base = "pajama_set" if PAJAMA_RE.search(text) else "apparel"
    parts = [p for p in (audience, material, base) if p]
    return "_".join(parts)


def parse_price(text):
    match = PRICE_RE.search(text or "")
    if not match:
        return None
    try:
        return round(float(match.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def http_text(url, timeout=30):
    if requests is None:
        return ""
    headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
    # 有些环境设置了失效的 HTTP(S)_PROXY；先按系统代理，失败后直连重试。
    for trust_env in (True, False):
        try:
            session = requests.Session()
            session.trust_env = trust_env
            response = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if response.status_code == 200 and response.text:
                return response.text
        except Exception:
            continue
    return ""


def strip_html(value):
    text = str(value or "")
    text = re.sub(r"<(script|style|template)\b[^>]*>.*?</\1\s*>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def discover_shopify_domains(query, limit=30):
    """用公开 DuckDuckGo 结果找 Shopify 店，不依赖任何搜索引擎 API。"""
    domains = list(SHOPIFY_SEEDS)
    search_url = "https://html.duckduckgo.com/html/?q=" + quote_plus("site:myshopify.com " + query)
    html = http_text(search_url)
    if html:
        for href in re.findall(r'result__a[^>]+href="([^"]+)"', html, re.I):
            href = html_lib.unescape(href)
            if "uddg=" in href:
                try:
                    href = unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
                except Exception:
                    pass
            host = (urlparse(href).hostname or "").lower()
            if host.endswith(".myshopify.com") and host not in domains:
                domains.append(host)
            if len(domains) >= limit:
                break
    return domains[:limit]


def matches_query(query, title, body, product_type="", tags=""):
    query_lower = (query or "").lower()
    title_lower = (title or "").lower()
    haystack = " ".join([title_lower, (body or "").lower(), (product_type or "").lower(), (tags or "").lower()])
    if "women" in query_lower or "womens" in query_lower or "ladies" in query_lower:
        if re.search(r"\b(?:men|mens|man|boys|girls|kids|children|toddler|baby)\b", title_lower):
            return False
    if "satin" in query_lower and "satin" not in haystack:
        return False
    if re.search(r"\bpajama", query_lower) and not re.search(r"\bpajama", haystack):
        return False
    if re.search(r"\bset\b", query_lower) and not re.search(r"\bset\b", haystack):
        return False
    return True


def collect_shopify(query, limit=20, domains=None, pages=3, per_store=None):
    """公开 Shopify /products.json 采集；无需 API Key。"""
    domains = domains or discover_shopify_domains(query)
    per_store = max(1, int(per_store or limit))
    buckets = []
    rejected = []
    for domain in domains:
        meta_raw = http_text("https://" + domain + "/meta.json")
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except Exception:
            meta = {}
        currency = str(meta.get("currency") or "USD").upper()
        if currency != "USD":
            log(f"跳过店铺 {domain}：币种 {currency}，当前只做 USD 基准")
            continue
        products = []
        for page in range(1, max(1, int(pages)) + 1):
            raw = http_text(f"https://{domain}/products.json?limit=250&page={page}")
            try:
                batch = (json.loads(raw).get("products") or []) if raw else []
            except Exception:
                batch = []
            if not batch:
                break
            products.extend(batch)
            if len(batch) < 250:
                break
        log(f"Shopify {domain}：{len(products)} 个商品，币种 {currency}")
        domain_items = []
        for product in products:
            title = str(product.get("title") or "")
            body = strip_html(product.get("body_html") or "")
            product_type = str(product.get("product_type") or "")
            tags = " ".join(str(t) for t in (product.get("tags") or []))
            haystack = " ".join([title, body, product_type, tags])
            if not matches_query(query, title, body, product_type, tags):
                rejected.append((title, "不符合查询词/性别"))
                continue
            if not PAJAMA_RE.search(haystack) or EXCLUDE_RE.search(haystack):
                rejected.append((title, "不是睡衣/家居服或命中排除词"))
                continue
            variants = product.get("variants") or []
            prices = []
            for variant in variants:
                try:
                    prices.append(float(variant.get("price")))
                except (TypeError, ValueError):
                    pass
            if not prices:
                continue
            options = product.get("options") or []
            option_names = " ".join(
                str(option.get("name") or "") for option in options if isinstance(option, dict)
            )
            option_haystack = title + " " + body + " " + option_names
            item = {
                "source": "shopify",
                "store": domain,
                "href": "https://" + domain + "/products/" + str(product.get("handle") or ""),
                "title": title,
                "price": round(min(prices), 2),
                "currency": currency,
                "description_chars": len(body),
                "description_text": body[:500],
                "has_size": bool(re.search(r"\b(?:size|sizing)\b|尺码|尺寸", option_haystack, re.I)),
                "has_color": bool(re.search(r"\b(?:colou?r)\b|颜色", option_haystack, re.I)),
                "has_material": bool(re.search(r"\b(?:satin|silk|silky)\b|缎|丝绸", option_haystack, re.I)),
                "image_count": len(product.get("images") or []),
            }
            domain_items.append(item)
            if len(domain_items) >= per_store:
                break
        if domain_items:
            buckets.append(domain_items)

    # 每个店铺轮流取样，避免第一家店把 limit 额度全部吃掉。
    items = []
    while len(items) < limit and any(buckets):
        for bucket in list(buckets):
            if bucket and len(items) < limit:
                items.append(bucket.pop(0))
            if not bucket:
                buckets.remove(bucket)
            if len(items) >= limit:
                break
    return items, rejected


def collect_woocommerce(query, limit=20, domains=None, per_store=5):
    """WooCommerce Store API；WordPress 独立站公开接口，无需 key。"""
    domains = domains or []
    items = []
    rejected = []
    per_store = max(1, int(per_store or limit))
    for domain in domains:
        search_terms = [term for term in ("satin", "pajama", "set") if term in query.lower()]
        search_query = " ".join(search_terms) or query
        url = "https://" + domain + "/wp-json/wc/store/v1/products"
        raw = http_text(url + "?search=" + quote_plus(search_query) + "&per_page=" + str(per_store * 2))
        try:
            products = json.loads(raw) if raw else []
        except Exception:
            products = []
        if not isinstance(products, list):
            products = []
        log(f"WooCommerce {domain}：{len(products)} 个商品")
        domain_items = []
        for product in products:
            title = str(product.get("name") or "")
            body = strip_html(
                str(product.get("description") or "") + " " + str(product.get("short_description") or "")
            )
            haystack = " ".join([title, body])
            if not matches_query(query, title, body):
                rejected.append((title, "不符合查询词/性别"))
                continue
            if not PAJAMA_RE.search(haystack) or EXCLUDE_RE.search(haystack):
                rejected.append((title, "不是睡衣/家居服或命中排除词"))
                continue
            prices = product.get("prices") or {}
            currency = str(prices.get("currency_code") or "USD").upper()
            if currency != "USD":
                rejected.append((title, "币种 " + currency))
                continue
            try:
                minor_unit = int(prices.get("currency_minor_unit") or 2)
                price = round(float(prices.get("price")) / (10 ** minor_unit), 2)
            except (TypeError, ValueError):
                rejected.append((title, "没有解析到价格"))
                continue
            attributes = product.get("attributes") or []
            attribute_text = " ".join(
                str(attribute.get("name") or "") + " "
                + " ".join(str(term.get("name") or term.get("slug") or "") for term in (attribute.get("terms") or []))
                for attribute in attributes
                if isinstance(attribute, dict)
            )
            option_haystack = " ".join([title, body, attribute_text])
            domain_items.append({
                "source": "woocommerce",
                "store": domain,
                "href": str(product.get("permalink") or ("https://" + domain)),
                "title": title,
                "price": price,
                "currency": currency,
                "description_chars": len(body),
                "description_text": body[:500],
                "has_size": bool(re.search(r"\b(?:size|sizing)\b|尺码|尺寸", option_haystack, re.I)),
                "has_color": bool(re.search(r"\b(?:colou?r)\b|颜色", option_haystack, re.I)),
                "has_material": bool(re.search(r"\b(?:satin|silk|silky)\b|缎|丝绸", option_haystack, re.I)),
                "image_count": len(product.get("images") or []),
            })
            if len(domain_items) >= per_store:
                break
        items.extend(domain_items)
    return items, rejected


def log(message):
    print(message, flush=True)


def load_registry(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("sources"), list):
            return data
    except Exception:
        pass
    return {"sources": [], "updated_at": ""}


def save_registry(path, registry):
    try:
        registry["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(registry, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        log(f"来源清单写入失败（不影响本次采集）：{exc}")


def rotating_sources(registry, state_path):
    """按上次位置轮询所有可用的独立站来源。"""
    sources = [
        {"domain": str(source["domain"]), "platform": str(source.get("platform") or "shopify").lower()}
        for source in (registry.get("sources") or [])
        if str(source.get("status") or "").lower() not in {"blocked", "skip-currency", "demo"}
        and source.get("domain")
        and str(source.get("platform") or "").lower() in {"shopify", "woocommerce"}
    ]
    if not sources:
        return [{"domain": domain, "platform": "shopify"} for domain in SHOPIFY_SEEDS]
    state = {}
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except Exception:
        state = {}
    index = int(state.get("next_source_index") or 0) % len(sources)
    ordered = sources[index:] + sources[:index]
    try:
        state["next_source_index"] = (index + 1) % len(sources)
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return ordered


def mark_source_results(registry_path, registry, attempted_domains, items):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stores = {str(item.get("store")) for item in items if item.get("store")}
    changed = False
    for source in registry.get("sources") or []:
        domain = str(source.get("domain") or "")
        if domain not in attempted_domains:
            continue
        status = str(source.get("status") or "")
        if status in {"blocked", "skip-currency", "demo"}:
            continue
        source["last_checked"] = now
        if domain in stores:
            source["status"] = "ok"
            source["last_ok"] = now
        else:
            source["status"] = "available"
        changed = True
    if changed:
        save_registry(registry_path, registry)


def extract_search_items(page, limit):
    # 新版 eBay 搜索结果没有稳定的 li.s-item；从商品链接向上找含价格的卡片。
    return page.evaluate(
        """
        (limit) => {
          const out = [];
          const seen = new Set();
          const links = [
            ...document.querySelectorAll('a[href*="/itm/"]'),
            ...document.querySelectorAll('[data-testid="item-card"] a[href*="/itm/"]'),
            ...document.querySelectorAll('.s-item a[href*="/itm/"]')
          ];
          for (const a of links) {
            const href = a.href.split('?')[0];
            if (seen.has(href) || /\\/itm\\/123456/.test(href)) continue;
            let el = a;
            let card = null;
            for (let i = 0; i < 12 && el; i++) {
              const text = el.innerText || '';
              if (/\\$[\\d,]+\\.\\d{2}/.test(text)) { card = el; break; }
              el = el.parentElement;
            }
            if (!card) card = a.closest('li, [data-testid="item-card"], .s-item');
            if (!card) continue;
            seen.add(href);
            const text = (card.innerText || '').replace(/\\s+/g, ' ').trim();
            const title = (a.innerText || a.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
            out.push({href, title, text: text.slice(0, 700)});
            if (out.length >= limit) break;
          }
          return out;
        }
        """,
        limit,
    )


def extract_item(context, url):
    """用同一浏览器上下文的 request 抓商品页，避免逐个 goto 导致 Chromium 崩溃。"""
    try:
        response = context.request.get(url, headers={"User-Agent": UA}, timeout=45000)
        if not response.ok:
            return {"description_chars": 0}
        html = response.text()
    except Exception:
        return {"description_chars": 0}

    title_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    title = re.sub(r"<[^>]+>", " ", title_match.group(1)) if title_match else ""
    title = html_lib.unescape(re.sub(r"\s+", " ", title)).strip()
    price = parse_price(html)
    desc_frame = ""
    match = re.search(r"https://itm\.ebaydesc\.com/itmdesc/[^\"'&\\s]+", html, re.I)
    if match:
        desc_frame = html_lib.unescape(match.group(0))
    description = ""
    if desc_frame:
        try:
            desc_response = context.request.get(desc_frame, headers={"User-Agent": UA}, timeout=30000)
            if desc_response.ok:
                desc_html = desc_response.text()
                desc_html = re.sub(r"<(script|style|template)\b[^>]*>.*?</\1\s*>", " ", desc_html, flags=re.I | re.S)
                desc_html = re.sub(r"<[^>]+>", " ", desc_html)
                description = re.sub(r"\s+", " ", html_lib.unescape(desc_html)).strip()
        except Exception:
            description = ""
    text_for_options = title + " " + description
    return {
        "title": title,
        "price": price,
        "description_chars": len(description) if description else 0,
        "has_size": bool(SIZE_RE.search(text_for_options)),
        "has_color": bool(COLOR_RE.search(text_for_options)),
        "description_text": description[:500],
    }


def build_benchmark(query, items):
    prices = [i["price"] for i in items if i.get("price")]
    descriptions = [i["description_chars"] for i in items if i.get("description_chars", 0) >= 50]
    size_count = sum(1 for i in items if i.get("has_size"))
    color_count = sum(1 for i in items if i.get("has_color"))
    material_count = sum(1 for i in items if i.get("has_material"))
    sample_count = len(items)
    sources = sorted({str(i.get("source") or "ebay") for i in items})
    stores = sorted({str(i.get("store")) for i in items if i.get("store")})
    source = sources[0] if len(sources) == 1 else "multi"
    return {
        "category_key": category_key(query, [i.get("title", "") for i in items]),
        "source": source,
        "currency": "USD",
        "sample_count": sample_count,
        "description_chars_p25": percentile(descriptions, 0.25),
        "description_chars_median": percentile(descriptions, 0.50),
        "description_chars_p75": percentile(descriptions, 0.75),
        "price_p25": percentile(prices, 0.25),
        "price_median": percentile(prices, 0.50),
        "price_p75": percentile(prices, 0.75),
        "price_p90": percentile(prices, 0.90),
        "size_coverage": round(size_count / sample_count * 100, 2) if sample_count else 0,
        "color_coverage": round(color_count / sample_count * 100, 2) if sample_count else 0,
        "material_coverage": round(material_count / sample_count * 100, 2) if sample_count else 0,
        "image_count_median": percentile([i.get("image_count") for i in items if i.get("image_count")], 0.50),
        "sample_urls": [i["href"] for i in items],
        "stores": stores,
        "samples": [
            {
                "title": i.get("title", ""),
                "price": i.get("price"),
                "store": i.get("store", ""),
                "source": i.get("source", ""),
                "href": i.get("href", ""),
                "description_chars": i.get("description_chars", 0),
                "has_size": bool(i.get("has_size")),
                "has_color": bool(i.get("has_color")),
                "has_material": bool(i.get("has_material")),
                "image_count": i.get("image_count"),
            }
            for i in items
        ],
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "notes": "公开市场样本快照（" + ", ".join(sources) + ("；店铺：" + ", ".join(stores) if stores else "") + "）；价格为页面展示价，未做品牌溢价假设。",
    }


def write_cache(cache_dir, benchmark):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, benchmark["category_key"] + ".json")
    payload = dict(benchmark)
    payload["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def write_references(cache_dir, query, items):
    """即使只有 1 条，也保存商品级参考，下一次/断网时可直接复用。"""
    os.makedirs(cache_dir, exist_ok=True)
    key = category_key(query, [i.get("title", "") for i in items])
    path = os.path.join(cache_dir, key + ".samples.json")
    payload = {
        "category_key": key,
        "query": query,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "samples": [
            {
                "title": i.get("title", ""),
                "price": i.get("price"),
                "currency": i.get("currency", "USD"),
                "store": i.get("store", ""),
                "source": i.get("source", ""),
                "href": i.get("href", ""),
                "description_chars": i.get("description_chars", 0),
                "has_size": bool(i.get("has_size")),
                "has_color": bool(i.get("has_color")),
                "has_material": bool(i.get("has_material")),
                "image_count": i.get("image_count"),
            }
            for i in items
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def read_cached_benchmark(cache_dir, query):
    key = category_key(query, [])
    path = os.path.join(cache_dir, key + ".json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and int(data.get("sample_count") or 0) >= 1:
            return data
    except Exception:
        pass
    return None


def read_references(cache_dir, query):
    key = category_key(query, [])
    path = os.path.join(cache_dir, key + ".samples.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data.get("samples"):
            return data
    except Exception:
        pass
    return None


def sql_escape(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def write_sql(path, benchmark):
    fields = [
        "category_key", "source", "currency", "sample_count",
        "description_chars_p25", "description_chars_median", "description_chars_p75",
        "price_p25", "price_median", "price_p75", "price_p90",
        "size_coverage", "color_coverage", "material_coverage", "image_count_median",
        "sample_urls", "captured_at", "notes",
    ]
    values = []
    for field in fields:
        value = benchmark.get(field)
        if field == "sample_urls":
            value = json.dumps(benchmark.get("sample_urls") or [], ensure_ascii=False)
        values.append(sql_escape(value))
    sql = (
        "INSERT INTO oem_market_benchmark (" + ", ".join(fields) + ") VALUES ("
        + ", ".join(values) + ")\n"
        + "ON DUPLICATE KEY UPDATE "
        + ", ".join(f"{f}=VALUES({f})" for f in fields if f not in ("category_key", "source", "currency"))
        + ";\n"
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(sql)
    return path


def emit_benchmark(items, args, rejected=None):
    reference_path = write_references(args.cache_dir, args.query, items) if items else ""
    min_samples = max(1, int(getattr(args, "min_samples", 1) or 1))
    if len(items) < min_samples:
        for title, reason in (rejected or [])[:10]:
            log(f"跳过：{reason}：{str(title)[:120]}")
        raise SystemExit(
            f"有效样本不足：{len(items)} 个，至少需要 {min_samples} 个；未写入基准，避免凭空判断。"
            + (f"；已保存 {len(items)} 条商品级参考：{reference_path}" if reference_path else "")
        )

    benchmark = build_benchmark(args.query, items)
    benchmark["cache_path"] = write_cache(args.cache_dir, benchmark)
    benchmark["reference_path"] = reference_path
    if args.sql_out:
        benchmark["sql_path"] = write_sql(args.sql_out, benchmark)
    print(json.dumps(benchmark, ensure_ascii=False, indent=2))
    return benchmark


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True, help="例如 women satin short sleeve pajama set")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--cache-dir", default=os.path.join(os.getcwd(), "market_benchmarks"))
    parser.add_argument("--sql-out", default="")
    parser.add_argument("--debug-dir", default="")
    parser.add_argument("--source", choices=["auto", "independent", "shopify", "woocommerce", "ebay"], default="auto")
    parser.add_argument("--shopify-domain", action="append", default=[])
    parser.add_argument("--shopify-pages", type=int, default=3)
    parser.add_argument("--per-store", type=int, default=5)
    parser.add_argument("--min-samples", type=int, default=1, help="1=单条也可作为弱参考；5=更稳的市场基准")
    parser.add_argument("--source-registry", default=SOURCE_REGISTRY_PATH)
    parser.add_argument("--source-state", default=SOURCE_STATE_PATH)
    parser.add_argument("--offline", action="store_true", help="直接复用本地基准 JSON，不联网")
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()

    if args.offline:
        cached = read_cached_benchmark(args.cache_dir, args.query)
        if cached is not None:
            print(json.dumps(cached, ensure_ascii=False, indent=2))
            return
        references = read_references(args.cache_dir, args.query)
        if references is not None:
            print(json.dumps(references, ensure_ascii=False, indent=2))
            return
        raise SystemExit("本地没有可复用的基准/参考缓存：请先联网跑一次，或检查 --cache-dir。")

    items = []
    rejected = []
    if args.source in ("auto", "independent", "shopify", "woocommerce"):
        registry = load_registry(args.source_registry)
        plan = [{"domain": domain, "platform": "shopify"} for domain in args.shopify_domain] if args.shopify_domain else rotating_sources(registry, args.source_state)
        shopify_domains = [s["domain"] for s in plan if s["platform"] == "shopify"]
        woocommerce_domains = [s["domain"] for s in plan if s["platform"] == "woocommerce"]
        attempted = []
        if args.source in ("auto", "independent", "shopify") and shopify_domains:
            shopify_items, shopify_rejected = collect_shopify(
                args.query, args.limit, shopify_domains, args.shopify_pages, args.per_store
            )
            items.extend(shopify_items)
            rejected.extend(shopify_rejected)
            attempted.extend(shopify_domains)
            log(f"Shopify 有效样本：{len(shopify_items)}")
        if args.source in ("auto", "independent", "woocommerce") and woocommerce_domains:
            woo_items, woo_rejected = collect_woocommerce(
                args.query, args.limit, woocommerce_domains, args.per_store
            )
            items.extend(woo_items)
            rejected.extend(woo_rejected)
            attempted.extend(woocommerce_domains)
            log(f"WooCommerce 有效样本：{len(woo_items)}")
        if attempted:
            mark_source_results(args.source_registry, registry, attempted, items)
        log(f"独立站有效样本：{len(items)}（店铺：{', '.join(sorted({i.get('store', '') for i in items if i.get('store')})) or '无'}）")
        if len(items) >= max(1, int(args.min_samples or 1)):
            emit_benchmark(items, args, rejected)
            return
        if args.source in ("shopify", "woocommerce", "independent"):
            emit_benchmark(items, args, rejected)
            return
        if items:
            log(f"独立站样本不足 5 个，已先保存商品级参考，后续可 --offline 复用")
            write_references(args.cache_dir, args.query, items)

    if sync_playwright is None:
        raise SystemExit("eBay 采集需要 Playwright：pip install playwright && playwright install chromium")

    base_urls = [
        "https://m.ebay.com/sch/i.html?_nkw=" + quote_plus(args.query) + "&_sacat=0&LH_BIN=1&_sop=12",
        "https://www.ebay.com/sch/i.html?_nkw=" + quote_plus(args.query) + "&_sacat=0&LH_BIN=1&_sop=12&_ipg=24",
    ]
    cards = []
    with sync_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        }
        proxy = (os.environ.get("OEM_CRAWL_PROXY") or "").strip()
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=UA,
            locale="en-US",
            timezone_id="America/Los_Angeles",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        # eBay 搜索页很大，图片字体等资源会显著增加 Chromium 内存和崩溃概率。
        # 只阻断展示资源，保留 HTML/JS。
        try:
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_(),
            )
        except Exception:
            pass

        cards = []
        debug_html = ""
        last_search_url = ""
        for search_url in base_urls:
            last_search_url = search_url
            page = context.new_page()
            try:
                response = page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                log(f"搜索页 HTTP {getattr(response, 'status', 0)}：{search_url}")
                try:
                    page.wait_for_selector('a[href*="/itm/"]', timeout=8000)
                except Exception:
                    pass
                # 不再滚动/长时间 wait：大页面在低内存机器上会直接崩溃。
                page.wait_for_timeout(500)
                title = page.title()
                debug_html = page.content()
                log(f"页面标题：{title!r}；HTML 长度：{len(debug_html)}")
                cards = extract_search_items(page, max(8, args.limit))
                log(f"识别到商品卡片：{len(cards)}")
            except Exception as exc:
                log(f"搜索页处理失败（{search_url}）：{exc}")
                cards = []
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            if cards:
                break

        if not cards and args.debug_dir and debug_html:
            os.makedirs(args.debug_dir, exist_ok=True)
            try:
                with open(os.path.join(args.debug_dir, "ebay_search.html"), "w", encoding="utf-8") as handle:
                    handle.write(debug_html)
            except Exception:
                pass
            log(f"调试 HTML 已写入：{args.debug_dir}（最后尝试：{last_search_url}）")

        items = []
        rejected = []
        for card in cards:
            title = card.get("title") or ""
            text = card.get("text") or ""
            if not PAJAMA_RE.search(title + " " + text):
                rejected.append((title or text, "不是睡衣/家居服"))
                continue
            if EXCLUDE_RE.search(title + " " + text):
                rejected.append((title, "命中排除词/非全新"))
                continue
            price = parse_price(text)
            if price is None:
                rejected.append((title or text, "没有解析到价格"))
                continue
            item = {
                "href": card["href"],
                "title": title,
                "price": price,
                "has_size": bool(SIZE_RE.search(title + " " + text)),
                "has_color": bool(COLOR_RE.search(title + " " + text)),
                "has_material": bool(re.search(r"\b(?:satin|silk|silky)\b|缎|丝绸", title + " " + text, re.I)),
            }
            try:
                extra = extract_item(context, card["href"])
                for key, value in extra.items():
                    if key == "title" and not value:
                        continue
                    if key in ("has_size", "has_color"):
                        item[key] = bool(item.get(key)) or bool(value)
                    elif key == "price":
                        continue
                    else:
                        item[key] = value
            except Exception as exc:
                log(f"商品页提取失败，保留搜索卡片数据：{card['href']}：{exc}")
            items.append(item)
            if len(items) >= args.limit:
                break
        browser.close()

    emit_benchmark(items, args, rejected)


if __name__ == "__main__":
    main()
