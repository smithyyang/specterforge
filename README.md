# 🦊 Camoufox Profile Manager

一个基于 Camoufox 的本地指纹浏览器管理界面，无限 Profile，完全免费。

## 安装

```bash
# 1. 克隆 / 解压到任意目录
cd camoufox-manager

# 2. 创建虚拟环境（推荐）
python3 -m venv venv
source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 下载 Camoufox 浏览器二进制（首次必须运行）
python3 -m camoufox fetch

# 5. 启动管理界面
python3 app.py
```

然后在浏览器访问：http://localhost:7070

## 功能

- ✅ 无限 Profile（本地存储，无账号限制）
- ✅ 每个 Profile 独立的浏览器数据目录（Cookie/localStorage 完全隔离）
- ✅ 配置：操作系统指纹（Windows/macOS/Linux）、语言/Locale、屏幕分辨率
- ✅ 代理绑定：HTTP / SOCKS5，per-profile 独立配置
- ✅ 标签 Tags + 备注 Notes
- ✅ 启动 / 关闭浏览器实例，实时状态显示
- ✅ 指纹由 Camoufox 内置的 BrowserForge 自动生成（每个 Profile 第一次启动时固定）

## 目录结构

```
camoufox-manager/
├── app.py              # Flask 后端
├── requirements.txt
├── templates/
│   └── index.html      # 前端 UI
└── profiles_data/      # 自动创建，每个 Profile 一个子目录
    └── <profile_id>/
        ├── meta.json   # Profile 配置
        ├── launch.py   # 自动生成的启动脚本
        └── userdata/   # 浏览器数据（Cookie、缓存等）
```

## 注意事项

- **隐私**：管理界面只监听 127.0.0.1，不对外暴露
- **代理**：强烈建议每个 Profile 绑定不同代理，否则 IP 相同会被关联
- **Camoufox 限制**：基于 Firefox 内核，无法模拟 Chrome 指纹
- **当前状态**：Camoufox 2026 年处于 beta 阶段，偶有不稳定
