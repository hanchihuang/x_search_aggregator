# x_search_aggregator

关键词抓取与分析工具、推特用户历史推文抓取分析工具、爬取某用户关注的所有用户。

## 一键爬取（先看这里）

### 0) 首次安装（只需一次）

```bash
cd /home/user/图片/x_search_aggregator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

### 1) 一键爬关键词（推荐先跑）

```bash
python search_x.py --keyword "信息差" --max-items 300 --sort Latest --state auth_state.json --lang zh --max-scrolls 180 --no-new-stop 10
```

输出目录：`output/<关键词>_<时间戳>/`

### 2) 一键爬某用户历史推文（示例：vista8）

```bash
python crawl_user_timeline.py --user-url "https://x.com/vista8" --state auth_state.json --max-items 0 --max-scrolls 3000 --no-new-stop 50
```

输出目录：`output/user_<handle>_<时间戳>/`

说明：
- `--max-items 0` = 不设条数上限，尽量抓全历史（受平台可见性与风控影响，不保证绝对全量）。
- 最近一次本地实测 `@vista8`：抓取 `435` 条，目录示例：`output/user_vista8_1772702515_cookie_full/`。

### 3) 一键爬某用户关注列表（示例：vista8）

```bash
python crawl_user_following.py --user-url "https://x.com/vista8" --state auth_state.json --max-items 0 --max-pages 300
```

输出目录：`output/following_<handle>_<时间戳>/`

## 登录状态（必须）

首次使用前，先执行一次登录状态保存：

```bash
python login_x.py --state auth_state.json --timeout 180
```

## 输出文件说明

关键词/历史推文抓取通常包含：

- `results.json`
- `results.csv`
- `summary.json`
- `summary.md`
- `article.html`
- `article_analysis.json`

关注列表抓取包含：

- `results.json`
- `results.csv`
- `detailed_report.json`
- `detailed_report.md`
- `detailed_report.html`

## 是否需要 X 开发者账号

不一定：

- 不需要开发者账号（你现在主要用这个）：
  - `search_x.py`
  - `crawl_user_timeline.py`
  - `crawl_user_following.py`
  - 基于网页端已登录会话（F12 抓包思路）
- 需要开发者账号：
  - `search_x_api.py`（官方 API）

## 常见问题（简版）

### 抓取到 0 条

- 登录态过期：重新执行 `login_x.py`
- 页面风控：稍后重试或更换网络
- 参数过小：增大 `--max-scrolls`

### 关注列表没抓全

- 这是平台侧可见性问题，不完全由脚本决定。
- 你可以看 `detailed_report.json` 里的：
  - `profile_following_count`
  - `total_following_collected`
  - `coverage_ratio`

## 脚本清单

- `search_x.py`：关键词网页抓取
- `crawl_user_timeline.py`：用户历史推文抓取
- `crawl_user_following.py`：用户关注列表抓取
- `search_x_api.py`：官方 API 关键词抓取
- `login_x.py`：登录状态保存
- `html_report.py`：HTML 报告生成

---

## 👥 AI 超级个体效率工具分享群

> 群聊：AI 超级个体效率工具分享群 3

![群聊二维码](assets/ai-super-individual-tools-group3-qr.jpg)

该二维码 7 天内有效，失效后会更新。

---

## 🔥 港美掘金 · 实战派财富系统

我正在「港美掘金」和朋友们讨论有趣的话题，你一起来吧？  
👉 https://t.zsxq.com/eQkvu
