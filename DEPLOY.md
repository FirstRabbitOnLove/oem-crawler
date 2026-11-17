# 部署（Linux）

## 1. 目录

建议放在 Web 根目录之外，避免源码和 `.env` 被公网访问：

```text
/path/to/oem-crawler/
  crawler_worker.py
  market_benchmark_worker.py
  requirements.txt
  market_sources.json
  .env
  storage/
```

`.env`、`.venv/`、`storage/` 已被 `.gitignore` 排除，不会被提交或覆盖。

## 2. Python

```bash
cd /path/to/oem-crawler
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

也可以用宝塔面板的 Python 项目管理器安装 Python 3.11 / 3.12。

## 3. Playwright（可选）

worker 以哪个用户运行，就用哪个用户安装浏览器：

```bash
source .venv/bin/activate
export PLAYWRIGHT_BROWSERS_PATH=/path/to/oem-crawler/ms-playwright
sudo -u www-data env HOME=/home/www-data PLAYWRIGHT_BROWSERS_PATH=$PLAYWRIGHT_BROWSERS_PATH \
  /path/to/oem-crawler/.venv/bin/playwright install chromium
sudo /path/to/oem-crawler/.venv/bin/playwright install-deps chromium
```

在 `.env` 中加一行：

```bash
PLAYWRIGHT_BROWSERS_PATH=/path/to/oem-crawler/ms-playwright
```

如果 `install-deps` 失败，可改用官方 Playwright Docker 镜像，或先关闭任务的 `render_js`，只用静态抓取。

## 4. 数据库

采集端需要以下三张表，由上层业务系统创建：

- `oem_crawl_run`
- `oem_crawl_url`
- `oem_crawl_page`

建议使用独立账号，只授予这三张表的读写权限：

```sql
CREATE USER 'oem_crawl_user'@'%' IDENTIFIED BY '强密码';
GRANT SELECT, INSERT, UPDATE, DELETE ON `oem_crawl_test`.`oem_crawl_run` TO 'oem_crawl_user'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `oem_crawl_test`.`oem_crawl_url` TO 'oem_crawl_user'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `oem_crawl_test`.`oem_crawl_page` TO 'oem_crawl_user'@'%';
FLUSH PRIVILEGES;
```

云数据库需在控制台白名单中加入 worker 服务器的出口 IP，否则会报 2003 连接失败。

## 5. 环境变量

```bash
cd /path/to/oem-crawler
cp .env.example .env
chmod 600 .env
vi .env
```

```bash
OEM_CRAWL_DB_NAME=oem_crawl_test
OEM_CRAWL_DB_HOST=db.example.com
OEM_CRAWL_DB_PORT=3306
OEM_CRAWL_DB_USER=oem_crawl_user
OEM_CRAWL_DB_PASSWORD=******
OEM_CRAWL_OUTPUT_DIR=/path/to/oem-crawler/storage
OEM_CRAWL_STALE_MINUTES=10
```

注意：

- `OEM_CRAWL_DB_NAME` 必须与上层业务系统使用的库名一致，否则两边看不到同一批任务
- 密码含 `#`、`$`、空格等字符时用单引号包起来
- 文件必须保存为 UTF-8 无 BOM，建议用 `vi` 编辑

## 6. 进程守护

systemd 示例：

```ini
[Unit]
Description=OEM Crawler Worker
After=network.target

[Service]
User=www-data
WorkingDirectory=/path/to/oem-crawler
ExecStart=/path/to/oem-crawler/.venv/bin/python crawler_worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now oem-crawler
```

也可以使用 supervisor 或宝塔「进程守护管理器」，启动命令：

```bash
/path/to/oem-crawler/.venv/bin/python /path/to/oem-crawler/crawler_worker.py
```

需要提高吞吐时，可再启动 1–3 个同样的进程；它们通过 `FOR UPDATE SKIP LOCKED` 自动分工，不会重复抓同一 URL。

## 7. 容量建议

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `max_pages` | 5000 | 大站可调到 20000 甚至更高 |
| `concurrency` | 8 | 单 worker 并发；4 核 8G 建议 8–12 |
| `delay_ms` | 0 | 有 WAF 时加 100–300 |
| `timeout` | 20 | 慢站可调 30 |
| worker 数 | 1–3 | 按 CPU 和内存调整，避免挤占其他服务 |

## 8. 清理

`storage/html` 和 `storage/shots` 会持续增长，建议用 crontab 定期清理：

```bash
find /path/to/oem-crawler/storage -type f -mtime +14 -delete
```

数据库中的 `oem_crawl_page.content_text` 占用较大，可按需保留最近 N 轮任务。

## 9. 故障排查

| 现象 | 检查 |
|---|---|
| `Access denied for user 'root'@'localhost'` | `.env` 没被读到：确认文件在脚本同目录、名字是 `.env`、保存为 UTF-8 无 BOM |
| `Can't connect to MySQL server`（2003） | 云数据库白名单未加入服务器 IP，或 host 写错 |
| `cryptography package is required` | `pip install cryptography` |
| 任务一直 pending | 两边库名是否一致；任务是否真的创建成功 |
| 心跳超 10 分钟 | 进程是否被 OOM 杀掉；任务会被自动回收重跑 |
| 抓取数为 0 | 域名是否正确、是否被 WAF 拦截、`robots.txt` 是否全站 Disallow |
| 页面正文很短 | 站点是 JS 渲染；开启 `render_js` 或降低判断阈值 |
| `Executable doesn't exist at ...ms-playwright` | 浏览器装到了别的用户目录；用运行 worker 的用户重装 |
| 目标页只有几百字节，内容是 `nginx` 500 页面 | 出口 IP 被目标站 / WAF 拦截，或源站 500；配置 `OEM_CRAWL_PROXY` |
