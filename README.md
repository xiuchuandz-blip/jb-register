# baiqi-GhostReg — JetBrains 账号半自动注册机

一键批量注册 JetBrains 账号。自动填邮箱、自动接验证码、自动填密码，你只需要手动过一下图片验证码。

## 功能

- **YYDS Mail 临时邮箱**（261 个域名随机切换，防风控封禁）
- 用户自带 API Key，保存一次永久生效
- 自动打开浏览器填写注册信息
- 自动点击 "I'm not a robot"
- **你手动完成图片验证码**（唯一需要人工的步骤，无时间限制）
- 验证码完成后自动点击 Continue（3 策略提交，含 Enter 键兜底）
- 自动接收邮箱验证码并填入
- 自动勾选协议 + 填写密码和姓名，完成注册
- **注册后可选自动跳转绑卡页**（tokens 页 + 选国家 + Add credit card）
- 支持同时开 1-10 个窗口并行注册
- **强制新建窗口**（不中断已有任务，追加新窗口）
- **一键关闭所有浏览器**（并发关闭，快速清理）
- Web 控制面板实时显示进度
- 一键填卡 / 一键清空 / 一键确认
- 注册成功的账号永久保存，关掉再开还在

## 下载

点击上方绿色的 **Code** 按钮 → **Download ZIP** → 解压到任意位置即可。

## 使用前准备

在运行本工具之前，你需要准备以下环境：

| 需要准备 | 说明 | 下载地址 |
|---|---|---|
| **Python 3.10+** | 运行环境，安装时必须勾选 "Add Python to PATH" | https://www.python.org/downloads/ |
| **Chrome 或 Edge** | 浏览器，Edge 是 Windows 自带的不用额外装 | Chrome: https://www.google.com/chrome/ |
| **梯子/VPN** | 中国大陆用户需要，用来访问 Google 验证码和 JetBrains | — |
| **YYDS Mail API Key** | 免费临时邮箱密钥，注册后获取 | https://vip.215.im |

> Python 依赖（DrissionPage、FastAPI 等）不需要单独下载，一条命令自动安装。

## 快速开始

**1.** 解压下载的 ZIP 文件

**2.** 打开解压后的文件夹，在地址栏输入 `cmd` 回车，打开命令行

**3.** 安装依赖（只需要第一次）：
```
pip install -r requirements.txt
```

**4.** 启动程序：
```
python -m ghostreg
```

**5.** 浏览器自动打开控制面板 → **先配置 API Key**（首次使用，只需一次）→ 设置密码和窗口数 → 点「开始注册」→ 手动过验证码 → 完成！

### 获取 API Key（免费，只需一次）

1. 访问 [vip.215.im/login](https://vip.215.im/login) 注册/登录（支持 GitHub 登录）
2. 访问 [vip.215.im/api-keys](https://vip.215.im/api-keys) 创建 API Key
3. 选择「读写」权限 + 「所有可访问域名」
4. 复制生成的 `AC-xxxxxx` 密钥，粘贴到 GhostReg 控制面板顶部的输入框，点「保存」
5. **保存后永久生效**，重启程序不需要重新配置

详细步骤请看压缩包里的 **使用教程.txt**。

## 注意事项

- 密码**只能用英文字母和数字**，不要加 @#$ 等特殊符号，否则注册失败
- 注册成功后浏览器窗口会保留不关闭，方便你查看
- CMD 窗口是后台服务，注册过程中不要关
- 注册结果保存在 `output/` 文件夹，`accounts.csv` 可以用 Excel 打开

## 项目结构

```
baiqi-GhostReg/
├── 使用教程.txt             ← 详细使用教程
├── requirements.txt         ← Python 依赖
├── ghostreg/                ← 主程序
│   ├── main.py              ← 启动入口
│   ├── server.py            ← Web 控制面板后端
│   ├── register.py          ← 注册流程（8步自动化）
│   ├── email_service.py     ← YYDS Mail 临时邮箱 API
│   ├── config.py            ← 配置 + API Key 持久化
│   └── static/index.html    ← 控制面板网页
└── output/                  ← 注册结果 + 设置文件（自动生成）
    ├── accounts.json/csv    ← 注册结果
    └── settings.json        ← API Key 等配置
```

## 技术栈

Python 3.10+ / DrissionPage / FastAPI / WebSocket / YYDS Mail API

## 免责声明

本项目仅供学习和研究自动化技术使用。使用者应遵守 JetBrains 的服务条款和相关法律法规。作者不对因使用本工具造成的任何后果承担责任。
