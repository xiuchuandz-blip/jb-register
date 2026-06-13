# GhostReg — JetBrains 账号批量注册机

批量注册 JetBrains 账号，支持三种模式：浏览器半自动、纯协议批量、Outlook 自备邮箱直注。

## 功能概览

### 🌐 浏览器注册（半自动）
- 使用 YYDS Mail 临时邮箱（261 个域名随机切换）
- 自动填写邮箱 / 验证码 / 姓名 / 密码，**只需手动过一次图片验证码**
- 支持 1-10 个窗口并行注册
- 支持强制新建窗口（不中断已有任务）
- 注册后可选自动跳转绑卡页

### ⚡ 协议注册（纯 HTTP，10x 速）
- 无需浏览器，全程 HTTP 请求
- 支持 YesCaptcha / CapSolver 自动打码
- 支持代理 / Cloudflare Worker 中转
- 可配置并发数批量注册

### 📧 Outlook 邮箱直注（自备邮箱）
- 用你自己的 Outlook / Hotmail / Live / Office365 邮箱直接注册
- 邮箱即 JetBrains 账号地址，无需临时邮箱
- **4 段 OAuth 格式**（推荐，无需开 IMAP）：`邮箱----密码----client_id----refresh_token`
- 2 段 IMAP 格式（需开启 IMAP）：`邮箱----密码`
- 读邮件方式自动选择：IMAP XOAUTH2 → Graph API → IMAP Basic Auth
- 密码留空自动为每个账号随机生成

### 通用
- Web 控制面板实时显示注册进度（WebSocket）
- 注册成功账号持久化保存（JSON + CSV）
- 支持历史记录查看 / 删除 / 导出

---

## 使用前准备

| 需要准备 | 说明 |
|---|---|
| **Python 3.10+** | 安装时勾选 "Add Python to PATH" |
| **Chrome / Edge / Brave** | 浏览器注册模式需要；协议/Outlook 模式不需要 |
| **梯子/VPN** | 中国大陆用户访问 JetBrains 和 Google reCAPTCHA 需要 |
| **YYDS Mail API Key** | 浏览器/协议模式需要，免费获取：https://vip.215.im |
| **验证码平台账号** | 协议模式需要：YesCaptcha 或 CapSolver |

---

## 快速开始

**1. 安装依赖（仅第一次）**
```bash
pip install -r requirements.txt
```

**2. 启动程序**
```bash
python -m ghostreg          # 默认端口 5000，自动打开浏览器
python -m ghostreg --no-browser   # 不自动打开浏览器
python -m ghostreg --port 8080    # 自定义端口
```

**3. 打开控制面板**

浏览器访问 `http://localhost:5000`，选择对应标签页开始注册。

### 获取 YYDS Mail API Key（浏览器/协议模式需要，免费）

1. 访问 [vip.215.im/login](https://vip.215.im/login) 注册/登录
2. 访问 [vip.215.im/api-keys](https://vip.215.im/api-keys) 创建 API Key
3. 选择「读写」权限 + 「所有可访问域名」
4. 复制 `AC-xxxxxx` 密钥，粘贴到控制面板顶部保存（永久生效）

---

## Outlook 邮箱注册说明

### 凭证格式

**推荐：4 段 OAuth 格式（无需开 IMAP）**
```
邮箱----密码----client_id----refresh_token
```
示例：
```
example@outlook.com----mypassword----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C508_BAY.xxxxx
```

**备选：2 段 IMAP 格式（需在 Microsoft 账户中开启 IMAP）**
```
邮箱----密码
```

每行一个账号，支持批量导入。

### 读邮件方式（自动选择，无需配置）

1. **IMAP XOAUTH2**：用 refresh_token 换取 Bearer token 登录 IMAP（最兼容）
2. **Microsoft Graph API**：调用 `graph.microsoft.com` 读取邮件
3. **IMAP Basic Auth**：用户名+密码直接登录（2 段格式兜底）

---

## 项目结构

```
GhostReg/
├── requirements.txt              ← Python 依赖
├── ghostreg/                     ← 主模块（Web UI + 浏览器注册）
│   ├── main.py                   ← 启动入口 / CLI
│   ├── server.py                 ← FastAPI 后端 + WebSocket
│   ├── register.py               ← 浏览器自动化注册流程
│   ├── email_service.py          ← YYDS Mail API
│   ├── outlook_email_service.py  ← Outlook OAuth / IMAP 邮件读取
│   ├── captcha_service.py        ← 验证码平台接口
│   ├── config.py                 ← 配置持久化
│   └── static/index.html         ← 控制面板前端
├── protocol_reg/                 ← 协议注册模块（纯 HTTP）
│   ├── jb_api.py                 ← JetBrains API 封装
│   ├── worker.py                 ← 批量注册调度
│   └── outlook_worker.py         ← Outlook 邮箱注册 worker
├── workers/                      ← Cloudflare Workers 代理脚本
│   └── ghostreg_proxy.js
└── output/                       ← 自动生成
    ├── accounts.json / .csv      ← 注册结果
    ├── outlook_accounts.json / .csv
    └── settings.json             ← 配置文件
```

---

## 技术栈

- **语言**：Python 3.12
- **后端**：FastAPI + Uvicorn (ASGI)
- **浏览器自动化**：DrissionPage
- **HTTP 客户端**：httpx、curl_cffi、requests
- **前端**：原生 HTML / JS / CSS + WebSocket 实时推送
- **邮件读取**：Microsoft Graph API、IMAP XOAUTH2、imaplib

---

## 注意事项

- 密码只支持英文字母和数字，不要加特殊符号
- CMD / 终端窗口是后台服务，注册过程中不要关
- 注册结果保存在 `output/` 目录，CSV 可用 Excel 打开
- 使用代理时建议每个账号切换不同 IP

## 免责声明

本项目仅供学习和研究自动化技术使用。使用者应遵守 JetBrains 服务条款及相关法律法规。作者不对因使用本工具造成的任何后果承担责任。
