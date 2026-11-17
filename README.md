# OEM Crawler

面向独立站（Shopify / WooCommerce 等）的全站页面采集工具。通过 `sitemap.xml` 与站内链接 BFS 发现页面，采集 HTML、页面结构与站点级事实，写入 MySQL，供上层业务系统做规则分析或人工复核。

> 本项目只负责「采集事实」，不做 AI 判断、内容修改，也不会访问 checkout / account / login 等敏感页面。

## 功能

- 轮询 MySQL 任务表领取任务，支持多 worker 并发（`FOR UPDATE SKIP LOCKED`）
- `sitemap.xml` + 站内链接 BFS 发现 URL，可配置是否允许同主域子域
- 采集标题、meta、标题层级、正文、图片、链接、JSON-LD、支付图标、追踪代码等
- 可选 Playwright 渲染与截图
- 站点级事实：域名 RDAP、TLS、DNS（含 DMARC）、robots.txt、sitemap、安全响应头、Wayback
- 识别 Cloudflare / WAF 人机验证页，标记为 `blocked`，不当作正文
- 断点续跑：心跳超时的任务自动回收；URL 与正文哈希去重
- 数据库断连自动重连

## 环境要求

| 项目 | 要求 |
| --- | --- |
| Python | 3.11+ |
| MySQL | 8.0+（或兼容版本） |
| Playwright | 可选，需要 JS 渲染或截图时安装 |

## 安装

```bash
git clone https://github.com/FirstRabbitOnLove/oem-crawler.git
cd oem-crawler

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

# 可选：JS 渲染 / 截图
playwright install chromium
```

## 配置

```bash
cp .env.example .env
vi .env
```

worker 只从环境变量读取配置（已存在的系统环境变量优先），不接受命令行传递密码。必填项缺失时会打印缺失的变量名并退出。

主要变量：

| 变量 | 说明 |
| --- | --- |
| `OEM_CRAWL_DB_HOST` | MySQL 地址 |
| `OEM_CRAWL_DB_PORT` | MySQL 端口，默认 3306 |
| `OEM_CRAWL_DB_NAME` | 数据库名，需与上层业务系统一致 |
| `OEM_CRAWL_DB_USER` / `OEM_CRAWL_DB_PASSWORD` | 数据库账号 |
| `OEM_CRAWL_OUTPUT_DIR` | HTML / 截图产物目录 |
| `OEM_CRAWL_STALE_MINUTES` | 心跳超过多少分钟视为 worker 已死，默认 10 |
| `OEM_CRAWL_SITE_FACTS` | 是否采集站点级事实，默认 1 |
| `OEM_CRAWL_PROXY` | 可选出口代理，支持 http / socks5 |
| `PLAYWRIGHT_BROWSERS_PATH` | 可选，Playwright 浏览器目录 |

## 运行

```bash
# 常驻
.venv/bin/python crawler_worker.py

# 只跑一轮
.venv/bin/python crawler_worker.py --once

# 只跑指定任务
.venv/bin/python crawler_worker.py --run-id 12
```

## 数据表

采集端依赖以下三张表，由上层业务系统创建（本仓库不包含建表 SQL）：

- `oem_crawl_run`：任务
- `oem_crawl_url`：待抓取 / 已抓取 URL
- `oem_crawl_page`：页面采集结果

状态流转：

```text
oem_crawl_url:  pending → fetching → done / failed / skipped / blocked
oem_crawl_run:  pending → running → finished / failed / cancelled
oem_crawl_page: status = ok / empty / error / blocked
```

## 抓取范围

包含：首页、商品、集合、政策、联系、关于、博客、sitemap 内同域页面。

排除：cart、checkout、account、login、register、search、评价页（reviews / comments / testimonials）、静态资源、外部域名。

`allow_subdomain=1` 时允许同主域名子域。

## 市场基准

`market_benchmark_worker.py` 用于采集品类市场基准，支持 Shopify `/products.json`、WooCommerce Store API、eBay 搜索结果等来源。

`market_sources.json` 为示例站点清单，使用前请替换成你自己的目标站点。

## 反爬与合规

- 命中 Cloudflare / WAF 验证页时标记 `blocked`，不送入上层分析，并保留截图
- 可通过 `OEM_CRAWL_PROXY` 更换出口 IP；自有站点建议在 WAF 中放行 worker 出口 IP
- 默认只抓目标任务域名，避免 SSRF
- 请遵守目标站点的 `robots.txt` 与服务条款，仅采集公开页面

## 部署

见 [DEPLOY.md](DEPLOY.md)。

## 免责声明

本项目仅供合法的市场调研、竞品分析与自有站点巡检使用。使用者需自行确保采集行为符合目标网站条款及当地法律法规。

## 许可证

本项目采用 [GNU General Public License v2.0](LICENSE) 许可证。
