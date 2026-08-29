# 贡献指南 (Contributing Guide)

欢迎参与 **Douyin Cloud Streak Pro** 的开发与维护！无论是提交 Bug 反馈、完善文档还是提交功能代码，我们都非常感谢您的贡献。

---

## 🛠️ 本地开发环境准备

1. **Fork 本仓库** 到你自己的 GitHub 账号；
2. **克隆代码到本地**：
   ```bash
   git clone https://github.com/<你的用户名>/douyin-cloud-streak.git
   cd douyin-cloud-streak
   ```
3. **创建 Python 虚拟环境并安装依赖**：
   ```bash
   python -m venv .venv
   # Windows 激活
   .venv\Scripts\activate
   # Linux/macOS 激活
   source .venv/bin/activate

   pip install -r requirements.txt
   playwright install chromium
   ```

---

## 📝 代码提交规范 (Pull Request)

1. **创建功能分支**：
   ```bash
   git checkout -b feature/your-feature-name
   # 或者修复分支
   git checkout -b fix/your-bug-fix
   ```
2. **保持代码风格统一**：
   - Python 代码遵循 PEP 8 规范；
   - 保持路径的相对自感知设计（避免硬编码任何绝对路径）；
   - 前端采用标准的现代 CSS Glassmorphism 规范与 Vue 3 Composition API。
3. **本地测试验证**：
   - 运行 `python run_cli.py --dry-run` 确保 CLI 运行正常；
   - 运行 `python app.py` 确保 Web 控制台能正常打开与交互。
4. **提交 PR**：
   - 提交前请清晰描述您所做的更改及解决的问题。
