# AI 媒体信息抓取 & 推送工具

## 系统架构

```
定时触发 (cron / GitHub Actions)
        │
        ▼
┌───────────────────────────────┐
│         数据抓取层             │
│  arXiv API │ 公众号RSS │ 小红书 │
└───────────┬───────────────────┘
            │
            ▼
    过滤(24h内) + 去重
            │
            ▼
    GPT-4o-mini 摘要
            │
            ▼
  企业微信 / 飞书 Webhook 推送
```

---

## 依赖库说明

| 库 | 用途 | 选择原因 |
|---|---|---|
| `requests` | HTTP 请求 | 最简单的网络请求库 |
| `feedparser` | 解析 RSS/Atom | 自动处理各种 RSS 格式，无需手写解析 |
| `openai` | 调用 GPT API | 官方 SDK，稳定可靠 |
| `python-dotenv` | 读取 .env 配置文件 | 避免把密钥写死在代码里 |
| `schedule` | 本地定时任务 | 纯 Python，无需配置系统 cron |

---

## 快速上手（非技术人员版）

### 第一步：安装 Python

1. 打开 https://www.python.org/downloads/
2. 下载并安装 Python 3.11 或更高版本
3. 安装时勾选 **"Add Python to PATH"**

### 第二步：下载项目并安装依赖

打开终端（Mac：搜索"终端"；Windows：搜索"cmd"），依次运行：

```bash
# 进入项目文件夹（替换为你的实际路径）
cd /你的路径/project8-AI媒体公众号信息抓取

# 安装所需库
pip install -r requirements.txt
```

### 第三步：配置密钥

1. 复制 `.env.example` 文件，重命名为 `.env`
2. 用记事本打开 `.env`，填入以下内容：

```
OPENAI_API_KEY=sk-你的OpenAI密钥
WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key
```

**获取企业微信 Webhook：**
> 企业微信群 → 右键群名 → 添加群机器人 → 新建机器人 → 复制 Webhook 地址

**获取 OpenAI API Key：**
> 登录 https://platform.openai.com → API Keys → Create new secret key

### 第四步：填写公众号列表

打开 `main.py`，找到这一段，填入你要监控的公众号微信号：

```python
WECHAT_ACCOUNT_LIST = [
    "AIxiaoge",      # 替换为真实公众号微信号
    "quantumrun",
]
```

> 公众号微信号查找方式：进入公众号主页 → 右上角"..." → 详细资料 → 微信号

### 第五步：测试运行

```bash
python main.py --now
```

看到 `流程执行完毕` 且企业微信群收到消息，说明配置成功。

### 第六步：设置自动运行（二选一）

**方案 A：GitHub Actions（推荐，免费、无需电脑常开）**

1. 在 GitHub 创建一个私有仓库，上传项目文件
2. 进入仓库 → Settings → Secrets → Actions → New repository secret
3. 分别添加 `OPENAI_API_KEY`、`WECOM_WEBHOOK_URL` 两个 Secret
4. 推送代码后，Actions 会在每个工作日北京时间 10:00 自动运行

**方案 B：本地定时运行（需要电脑常开）**

```bash
# 直接运行，程序会在后台等待每天 10:00 执行
python main.py
```

---

## 关于微信公众号抓取的说明

微信公众号没有公开 API，本工具通过 **RSSHub** 服务将公众号转为 RSS 订阅。

- 公共实例：`https://rsshub.app`（免费，但偶尔不稳定）
- 自建实例（推荐）：参考 https://docs.rsshub.app/zh/deploy/

如果公共实例不可用，在 `.env` 中配置自建地址：
```
RSSHUB_BASE_URL=https://你的rsshub地址
```

---

## 常见问题

**Q: 运行后没有收到消息？**
- 检查 `.env` 文件是否存在且格式正确
- 运行 `python main.py --now` 查看终端日志

**Q: arXiv 有内容但公众号没有？**
- RSSHub 公共实例可能限流，等几分钟重试，或自建实例

**Q: 想修改推送时间？**
- 编辑 `main.py` 底部的 `schedule.every().monday.at("10:00")` 改为你想要的时间
