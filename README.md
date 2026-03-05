# x_search_aggregator

关键词抓取与分析工具：从 X（Twitter）采集内容，支持可自定义滚动翻页与抓取条数控制，并生成结构化数据与深度汇总 HTML 文章。

## 功能概览

- 关键词抓取（Latest / Top）
- 可自定义滚动翻页爬取，爬多少由你控制（如 `--max-items`、`--max-scrolls`）
- API 模式与浏览器模式双支持
- 导出 `results.json` / `results.csv`
- 导出 `summary.json` / `summary.md`
- 自动生成排版精美的深度文章 `article.html`

## 脚本说明

- `search_x_api.py`：官方 API 抓取（推荐，稳定）
- `search_x.py`：Playwright + 登录态文件抓取
- `search_with_existing_chrome.py`：连接已有 Chrome（CDP）抓取
- `crawl_user_timeline.py`：按用户主页滚动抓取历史推文并生成详细分析报告
- `login_x.py`：登录并保存会话状态
- `html_report.py`：深度分析与 HTML 文章生成

## 环境安装

```bash
cd /home/user/图片/x_search_aggregator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 快速开始

### 方案 A：官方 API（推荐）

1. 在 X Developer Portal 获取 Bearer Token

```bash
export X_BEARER_TOKEN='your_token'
```

2. 抓取 300 条并生成报告

```bash
python search_x_api.py --keyword "信息差" --max-items 300 --lang zh
```

### 方案 B：浏览器登录态抓取

1. 登录并保存状态

```bash
python login_x.py --state auth_state.json --timeout 180
```

2. 执行抓取

```bash
python search_x.py --keyword "信息差" --max-items 300 --sort Latest --state auth_state.json --lang zh --max-scrolls 180 --no-new-stop 10
```

### 方案 C：抓取某用户历史推文（示例：`@vista8`）

```bash
python crawl_user_timeline.py \
  --user-url "https://x.com/vista8" \
  --state auth_state.json \
  --max-items 300 \
  --max-scrolls 500 \
  --no-new-stop 25
```

说明：
- `--max-items 0` 表示不设硬上限，滚动到页面无新增为止（受平台加载策略影响，不保证绝对全量）。
- 可加 `--with-replies` 抓取 `with_replies` 时间线。

实测示例（本地）：
- 目标：`https://x.com/vista8`
- 抓取数量：`300`
- 输出目录示例：`output/user_vista8_1772696133_cookie/`

## 输出结果

每次运行生成目录：

```text
output/<关键词>_<时间戳>/
  ├── results.json
  ├── results.csv
  ├── summary.json
  ├── summary.md
  ├── article.html
  └── article_analysis.json

用户历史抓取还会额外生成：

```text
output/user_<handle>_<时间戳>/
  ├── detailed_report.json
  ├── detailed_report.md
  └── detailed_report.html
```
```

## 常见问题

### 抓取到 0 条

- 登录态失效或未登录
- 页面风控/挑战
- 关键词当前结果少

建议优先使用 API 方案；浏览器方案下可重新运行 `login_x.py`。

### Chrome profile 锁（SingletonLock）

说明你的 Chrome 用户目录正被占用。关闭所有 Chrome 进程后重试。

## 安全与合规

- 不要泄露 `auth_token`、`ct0`、`Bearer Token`。
- 泄露后应立刻退出全部会话、改密码、重置 Token。
- 请遵守当地法律法规和平台服务条款。

## 发布到 GitHub

### 1. 创建 GitHub 仓库

在 GitHub 新建空仓库，例如：`x_search_aggregator`。

### 2. 本地提交

```bash
git init
git add .
git commit -m "feat: initial x search aggregator"
```

### 3. 绑定远程并推送

```bash
git branch -M main
git remote add origin https://github.com/<your_user>/x_search_aggregator.git
git push -u origin main
```

如果本地已经是 git 仓库，只执行 `git add/commit/remote/push` 即可。

---

## 👥 AI 超级个体效率工具分享群

> 群聊：AI 超级个体效率工具分享群 3

![群聊二维码](assets/ai-super-individual-tools-group3-qr.jpg)

该二维码 7 天内有效，失效后会更新。

---

## 🔥 港美掘金 · 实战派财富系统

我正在「港美掘金」和朋友们讨论有趣的话题，你一起来吧？  
👉 https://t.zsxq.com/eQkvu

### 三大核心价值：从“抄作业”到“建系统”

### 价值一：实盘验证的“暴利”策略，拒绝纸上谈兵

- 📈 年化 97% 的修复版涨停基因：从回测数据到实盘逻辑闭环，直接上手。
- 💰 低风险套利大全：覆盖美股、港股、Crypto；含 LOF 套利、可转债双低、链上资金费率套利等。
- 🤖 量化数据与源码免费领：机器学习策略、Meme 币策略构建源码、网格交易自动化脚本。

### 价值二：打破信息茧房，获取一手“内幕”情报

- 🏦 机构持仓大起底：深度解读 13F，跟踪巴菲特、段永平、Cathie Wood 等资金动向。
- 📊 独家研报内参：每日更新顶级投行核心摘要，覆盖高频重要变化。
- ⚡ 实时风险预警：VIX 异动、美联储政策拐点等关键风险信号提前提示。

### 价值三：AI 赋能 + 工具大全，一个人活成一支队伍

- 🤖 AI 搞钱工具箱：大模型高效用法、批量问答、自动化策略工具。
- 💻 零代码量化平台：不写代码也能搭建可运行的量化系统。
- 🌐 全球资源库：覆盖多行业学习资料与实战内容。

### 🎁 星主诚意承诺：加入即送“核武器”级福利

1. 知识资产：3000+ 份量化研报、私募量化方法论、全市场策略解析与高价值学习资源。
2. 技术基建：800+ 套策略源码、脱敏会议记录、实盘代码与多市场数据接口支持。
3. AI 特权：包含 Codex、Gemini、Claude 等高级模型使用方案与部分 Pro/API 资源。
4. 持续进化与变现：每周 14+ 条硬核更新、高端人脉圈、长期陪伴社群。

### 🌟 真实反馈

> @AI Quant：
> 这里不仅有投资策略，还有 AI 工具和效率体系。自动监控聪明钱地址的 Meme 币策略，直接打开了新世界的大门。

### ⏳ 行动建议

财富窗口期只留给行动快的人。与其购买看完即忘的零散课程，不如接入一套持续更新的财富系统。  
👇 立即加入「港美掘金」，开启你的财富自由之路。
