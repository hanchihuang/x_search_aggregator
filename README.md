<p align="center">
  <h1 align="center">🔍 X Search Aggregator</h1>
  <p align="center">
    <b>把 X 上的关键词、关注流和高价值内容，直接变成可读的 HTML 页面</b><br/>
    浏览器控制台 · 异步抓取 · 实时进度 · 排名排序 · 本地持久化
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white" alt="Python"/>
    <img src="https://img.shields.io/badge/Playwright-自动化-2EAD33?logo=playwright&logoColor=white" alt="Playwright"/>
    <img src="https://img.shields.io/badge/Flask-Frontend_Control-111827?logo=flask&logoColor=white" alt="Flask"/>
    <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License"/>
    <img src="https://img.shields.io/badge/Platform-X.com-000000?logo=x&logoColor=white" alt="X.com"/>
  </p>
</p>

---

## ✨ 它能做什么

- 输入一个关键词，自动抓取最新内容并生成 HTML 文章页
- 一键抓取你关注的所有人最新动态，再按“有用程度”排序
- 在浏览器里查看任务进度、滚动轮次、已抓条数、最近新增条数
- 任务关闭页面后仍保留，服务重启后会从磁盘恢复状态
- 所有结果落地为 `JSON / CSV / Markdown / HTML`
- 可在控制台中桥接启动 `BettaFish`，作为外部多 Agent 舆情系统入口

## 🧾 最终产出长什么样

下面不是后台截图，而是系统最终生成的 HTML 成品页：

**有用程度排序页**

![Demo Ranking](assets/demo-ranking.jpg)

**深度文章页**

![Demo Article](assets/demo-article.jpg)

## 🖥️ 像产品一样使用

### 1. 安装

```bash
git clone https://github.com/hhchhchhchhc/x_search_aggregator.git
cd x_search_aggregator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. 登录 X（一次性）

```bash
python login_x.py --state auth_state.json --timeout 180
```

> 会打开浏览器窗口，手动完成登录即可，登录状态自动保存。

### 3. 启动本地控制台

```bash
python web_app.py
```

然后打开 `http://127.0.0.1:8080`

### 4. 在页面里完成操作

- 输入关键词，生成专题 HTML
- 点击关注流任务，生成关注者动态排序页
- 输入任意 `x.com` 用户主页，抓取该博主全部历史推文
- 输入任意 `x.com` 用户主页，抓取该博主关注用户列表
- 在同一个页面里看任务日志、进度条和历史任务
- 直接点击生成结果打开 HTML

## 📷 界面预览

**控制台首页**

![Web Console Home](assets/web-console-home.png)

**任务运行中**

![Web Console Task](assets/web-console-task.png)

## ⚡ 典型工作流

### 关键词专题页

`输入关键词 -> 抓取最新 500 条 -> 生成 article.html -> 生成 usefulness_ranking.html`

### 关注流高价值排序

`抓取 Following 时间线 -> 聚合最新动态 -> 评分排序 -> 输出排名 HTML`

## ✨ 功能矩阵

| 功能 | 脚本 | 说明 |
|------|------|------|
| 🔎 **关键词搜索** | `search_x.py` | 按关键词搜索 X，自定义数量，支持语言过滤 |
| 🔎 **关键词搜索 Top 500** | `search_keyword_500.py` | 一键抓取关键词最新 **500 条**推文 |
| 📜 **用户历史推文** | `crawl_user_timeline.py` | 爬取任意用户的全部历史推文 |
| 👥 **用户关注列表** | `crawl_user_following.py` | 通过内部 API 爬取用户完整关注列表 |
| 📡 **关注者最新动态 500** | `crawl_following_timeline_500.py` | 一键抓取你关注的所有人的最新 **500 条**动态 |
| 📜 **博主全部历史推文** | `crawl_user_timeline.py` | 输入任意 `x.com` 用户主页，尽量抓取其全部历史推文 |
| 👥 **博主关注列表** | `crawl_user_following.py` | 输入任意 `x.com` 用户主页，抓取其关注用户列表并生成画像报告 |
| 📊 **有用程度排名** | `rank_usefulness.py` | 对任意采集结果按有用程度智能评分，生成可视化排名 HTML |
| 🖥️ **本地前端控制台** | `web_app.py` | 浏览器里输入关键词 / 点击按钮执行抓取，异步查看进度、日志和 HTML 结果 |
| 🐟 **BettaFish 集成** | `web_app.py` | 配置本地 BettaFish 路径并在控制台中启动 / 停止 / 打开它 |
| 🔑 **登录状态管理** | `login_x.py` | 一次登录，持久化 session，后续全自动 |

> **无需 X 开发者账号**，基于浏览器已登录会话自动化采集，零 API 配额限制。

---

## 🚀 命令行入口

#### 🖥️ 本地前端控制台

```bash
python web_app.py
```

然后打开 `http://127.0.0.1:8080`，即可：

- 输入关键词，点击按钮抓取并直接打开生成的 HTML
- 一键抓取你关注的所有人最新动态，并打开排序页 / 摘要页 / 文章页
- 输入任意 `x.com` 博主主页，抓取其全部历史推文，并生成文章页 / 详细报告 / 排序页
- 输入任意 `x.com` 博主主页，抓取其关注用户列表，并生成详细画像报告
- 异步查看任务进度、实时日志、最近新增条数、已抓取条数、滚动轮次
- 在页面里查看最近生成的输出目录、切换历史任务、停止运行中的任务
- 页面关闭或服务重启后，任务状态会从磁盘恢复

前端控制台依赖 `Flask`，已经包含在 `requirements.txt` 中。

#### 🔎 关键词搜索最新 500 条

```bash
python search_keyword_500.py --keyword "AI Agent" --lang zh
```

#### 📡 关注者最新 500 条动态

```bash
python crawl_following_timeline_500.py
```

#### 🔎 关键词搜索（自定义数量）

```bash
python search_x.py --keyword "信息差" --max-items 300 --sort Latest --lang zh
```

#### 📜 爬取用户历史推文

```bash
python crawl_user_timeline.py --user-url "https://x.com/elonmusk" --max-items 0
```

> `--max-items 0` = 不设上限，尽量抓全部历史。

#### 👥 爬取用户关注列表

```bash
python crawl_user_following.py --user-url "https://x.com/elonmusk" --max-items 0
```

#### 📊 对采集结果按有用程度排名

```bash
python rank_usefulness.py --input output/following_timeline_500_20260306_193541
```

> 支持对任意 `results.json` 使用：关键词搜索、关注者动态、用户历史推文均可。
> 自动生成暗色主题可视化排名 HTML + 评分 JSON。

---

## 📊 输出示例

每次运行自动生成独立输出目录，包含多种格式：

```
output/AI_Agent_500_20260306_180318/
├── results.json          # 全量结构化数据
├── results.csv           # Excel 可直接打开
├── summary.json          # 统计摘要（热门标签/提及/高赞推文）
├── summary.md            # Markdown 版摘要
└── article.html          # 可视化 HTML 报告，浏览器直接打开
```

**关注者动态**额外输出：

```
output/following_timeline_500_20260306_193541/
├── results.json / csv
├── summary.json / md
├── summary.html          # 关注者活跃度排名、热门标签、Top 推文
└── article.html          # 完整动态文章
```

**有用程度排名**输出（`rank_usefulness.py` 生成）：

```
output/<任意采集目录>/
├── usefulness_ranking.html   # 🌟 暗色主题可视化排名页面
└── usefulness_ranking.json   # 带评分的结构化排名数据
```

**前端控制台任务状态持久化**：

```
output/.web_tasks.json        # 前端任务队列、日志、进度缓存
```

**用户关注列表**额外输出：

```
output/following_vista8_20260306/
├── results.json / csv
├── detailed_report.json  # 关注者画像分析（认证比例/Bio关键词/语言分布）
├── detailed_report.md
└── detailed_report.html
```

---

## 🛠️ 全部参数速查

### web_app.py

| 路径 | 说明 |
|------|------|
| `http://127.0.0.1:8080` | 本地前端控制台首页 |
| `/api/tasks` | 任务列表接口 |
| `/api/tasks/<task_id>` | 单个任务状态接口 |
| `/api/tasks/<task_id>/stop` | 停止指定任务 |

### search_keyword_500.py

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--keyword` | *必填* | 搜索关键词 |
| `--lang` | 空 | 语言过滤（`zh` / `en` 等） |
| `--headless` | 关 | 无头模式运行 |
| `--max-scrolls` | 200 | 最大滚动轮数 |
| `--no-new-stop` | 10 | 连续无新内容后停止 |

### crawl_following_timeline_500.py

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--headless` | 关 | 无头模式运行 |
| `--max-scrolls` | 300 | 最大滚动轮数 |
| `--no-new-stop` | 12 | 连续无新内容后停止 |

### search_x.py

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--keyword` | *必填* | 搜索关键词 |
| `--max-items` | 200 | 最大采集数 |
| `--sort` | Latest | 排序方式（`Top` / `Latest`） |
| `--lang` | 空 | 语言过滤 |
| `--headless` | 关 | 无头模式 |

### crawl_user_timeline.py

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--user-url` | *必填* | 用户主页 URL |
| `--max-items` | 0 | 最大条数（0=不限） |
| `--max-scrolls` | 1000 | 最大滚动轮数 |
| `--with-replies` | 关 | 包含回复推文 |

### crawl_user_following.py

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--user-url` | *必填* | 用户主页 URL |
| `--max-items` | 0 | 最大条数（0=不限） |
| `--max-pages` | 200 | 最大 API 翻页数 |

### rank_usefulness.py

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--input` | *必填* | `results.json` 路径或包含它的目录 |
| `--title` | 自动 | 自定义页面标题 |
| `--output` | 自动 | 输出 HTML 路径（默认同目录下 `usefulness_ranking.html`） |

**评分算法：**

| 维度 | 权重/规则 |
|------|-----------|
| ❤️ Like | ×3 |
| 🔁 Retweet | ×5 |
| 💬 Reply | ×2 |
| 🔖 Bookmark | ×8 |
| 👁️ 浏览量 | 归一化加分（上限 50） |
| 📏 内容长度 | 越充实越高分（上限 30） |
| 🏷️ 技术关键词 | AI/GPT/Claude/量化/开源等命中 ×3 |
| 📋 结构化内容 | 含步骤/列表 +8 |
| 🔗 资源链接 | 含 URL +5 |
| 🚫 垃圾内容 | "关注我"/"抽奖"等 −5 |

当前版本已改为：

- 互动分对数归一化，避免超大号单纯靠体量霸榜
- 增加时效加分
- HTML 卡片展示评分构成（互动 / 内容 / 时效）

---

## 🖥️ 前端控制台能力

- 关键词抓取：提交后后台异步执行 `search_keyword_500.py` + `rank_usefulness.py`
- 关注流抓取：提交后后台异步执行 `crawl_following_timeline_500.py` + `rank_usefulness.py`
- 博主历史推文抓取：提交后后台异步执行 `crawl_user_timeline.py` + `rank_usefulness.py`
- 博主关注列表抓取：提交后后台异步执行 `crawl_user_following.py`
- 多任务队列：可切换查看历史任务、运行中任务、失败任务
- 实时进度：显示目标条数、已抓取条数、滚动轮次、最近新增条数
- 停止任务：可终止当前运行中的抓取子进程
- 状态持久化：服务重启后从 `output/.web_tasks.json` 恢复任务历史

---

## 🏗️ 技术架构

```
┌─────────────────────────────────────────────┐
│              X Search Aggregator            │
├─────────────┬───────────────────────────────┤
│  采集层     │  Playwright 浏览器自动化       │
│             │  ├─ 模拟真实用户滚动浏览       │
│             │  ├─ 反检测指纹 (browser_config) │
│             │  └─ 内部 GraphQL API 调用      │
├─────────────┼───────────────────────────────┤
│  解析层     │  DOM 解析 + 多级回退策略        │
│             │  ├─ 推文内容/时间/指标提取      │
│             │  ├─ 用户信息提取               │
│             │  └─ 智能去重 (tweet_id)        │
├─────────────┼───────────────────────────────┤
│  分析层     │  智能评分 & 排名引擎           │
│             │  ├─ 互动加权 + 内容质量信号    │
│             │  ├─ 技术关键词 / 结构化检测    │
│             │  └─ 垃圾内容过滤              │
├─────────────┼───────────────────────────────┤
│  输出层     │  JSON / CSV / Markdown / HTML  │
│             │  ├─ 统计摘要 & 可视化报告      │
│             │  └─ 有用程度排名 & 深度文章    │
└─────────────┴───────────────────────────────┘
```

---

## ❓ 常见问题

<details>
<summary><b>抓取到 0 条怎么办？</b></summary>

1. **登录态过期** → 重新执行 `python login_x.py`
2. **平台风控** → 稍后重试或更换网络环境
3. **参数过小** → 增大 `--max-scrolls` 和 `--no-new-stop`
</details>

<details>
<summary><b>关注列表没抓全？</b></summary>

这是 X 平台侧可见性限制，非脚本问题。查看 `detailed_report.json` 中的覆盖率：
- `profile_following_count`：账号显示的关注数
- `total_following_collected`：实际抓到的数量
- `coverage_ratio`：覆盖比例
</details>

<details>
<summary><b>需要 X 开发者账号吗？</b></summary>

**不需要！** 主要脚本全部基于浏览器会话自动化，无需任何 API Key。

仅 `search_x_api.py` 需要 Bearer Token（官方 API 方案，可选）。
</details>

<details>
<summary><b>支持无头模式吗？</b></summary>

所有脚本都支持 `--headless` 参数，适合服务器/后台运行。
</details>

---

## 📁 项目结构

```
x_search_aggregator/
├── search_keyword_500.py          # ⭐ 关键词搜索 500 条
├── crawl_following_timeline_500.py # ⭐ 关注者动态 500 条
├── search_x.py                    # 关键词搜索（自定义数量）
├── crawl_user_timeline.py         # 用户历史推文爬取
├── crawl_user_following.py        # 用户关注列表爬取
├── rank_usefulness.py              # ⭐ 有用程度智能排名
├── search_x_api.py                # 官方 API 搜索（可选）
├── login_x.py                     # 登录状态保存
├── html_report.py                 # HTML 报告生成引擎
├── browser_config.py              # 反检测浏览器配置
├── search_x_long_runner.py        # 长时间运行采集器
├── requirements.txt
└── output/                        # 所有输出自动归档于此
```

---

## 🤝 贡献

欢迎 Issue 和 PR！如果觉得有用，请点个 ⭐ Star 支持一下。

---

## 👥 AI 超级个体效率工具分享群

> 群聊：AI 超级个体效率工具分享群 3

![群聊二维码](assets/ai-super-individual-tools-group3-qr.jpg)

该二维码 7 天内有效，失效后会更新。

---

## 🔥 港美掘金 · 实战派财富系统

> 我正在「港美掘金」和朋友们讨论有趣的话题，你一起来吧？
> 👉 **https://t.zsxq.com/eQkvu**

### 三大核心价值：从"抄作业"到"建系统" 🚀

<details open>
<summary><b>💎 价值一：实盘验证的"暴利"策略，拒绝纸上谈兵</b></summary>

我们不只讲逻辑，更给结果。

- 📈 **年化 97% 的修复版涨停基因**：宝藏策略全公开，从回测数据到实盘逻辑闭环，直接抄作业。
- 💰 **低风险套利大全**：涵盖美股、港股、Crypto 全方位教程。从 LOF 基金套利、可转债双低策略，到链上资金费率套利，一周内即可看到正反馈。
- 🤖 **量化数据、源码免费领**：聚宽社区效果最好的 9 个机器学习策略、Meme 币交易策略构建源码、网格交易全自动脚本，直接导入自动运行。
</details>

<details open>
<summary><b>🧠 价值二：打破信息茧房，获取一手"内幕"情报</b></summary>

- 🏦 **机构持仓大起底**：深度解读 13F 报告，跟着巴菲特、段永平、Cathie Wood 等大佬抄作业，看清华尔街真实动向。
- 📊 **独家研报内参**：每日更新高盛、摩根士丹利、桥水等顶级投行核心摘要，甚至包含未公开的私募路演资料。
- ⚡ **实时风险预警**：VIX 指数异常波动、美联储政策转向？我们比新闻更快发出警报，助你提前布局尾部风险管理。
</details>

<details open>
<summary><b>🛠️ 价值三：AI 赋能 + 工具大全，一个人活成一支队伍</b></summary>

- 🤖 **AI 搞钱工具箱**：免费白嫖 Opus 4.6、Gemini Pro 等顶级大模型的方法；一键批量 AI 提问、网盘搜索神器、自动化跟单机器人（SoberBot）。
- 💻 **零代码量化平台**：推荐 Ephod 等 No Code 平台，不懂代码也能搭建自己的量化交易系统。
- 🌐 **全球资源库**：90T+ 宝藏资源库，涵盖各行各业付费课程、神级视频学习资源，加入即送。
</details>

### 🎁 星主的诚意承诺：加入即送"核武器"级福利

现在加入「港美掘金」，不仅仅是入群，更是直接接管一套成熟的财富系统：

| 福利类别 | 内容 |
|----------|------|
| 📚 **知识资产** | 3000+ 份量化研报、私募一线量化大神 RL 实践方法论、每周高质量全市场策略解析；十几万知识付费内容免费送；永久免费学术论文下载账号 |
| 🔧 **技术基建** | 800+ 套优质策略源码、数万行实盘代码、Tushare Pro API Key、港美A股外汇加密货币数据接口、近 16 年日线数据、2CPU 2G VPS 一年免费 |
| 🤖 **AI 特权** | 免费使用 Codex、Gemini、Claude 等最高级大模型的方法，以及部分 Pro 账号与 API Key，每年省下几千元订阅费 |
| 🔄 **持续进化** | 每周 14+ 条硬核更新、高端人脉圈、终身陪伴社群 |

### 🌟 用户说

> *"不仅仅是投资策略，这里的 AI 工具和效率技巧让我一个人活成了一个团队。特别是那个自动监控聪明钱地址的 Meme 币策略，简直打开了新世界的大门！"*
> — @AI Quant

---

> ⏳ **财富的窗口期，只留给行动快的人。** 与其花几千块买一堆看了就忘的网课，不如给自己买一个持续更新的财富大脑。
>
> 👉 **立即加入：https://t.zsxq.com/eQkvu**

---

<p align="center">
  <sub>Made with ❤️ for the X/Twitter research community</sub>
</p>
