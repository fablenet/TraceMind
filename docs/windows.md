# Windows 本地开发与测试

## TraceMind

### 1. 虚拟环境 (venv)

在项目根目录下创建并启用 venv：

```powershell
cd E:\src\fable-net\TraceMind
python -m venv venv
.\venv\Scripts\Activate.ps1
```

若执行策略禁止脚本，可先执行：`Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

### 2. 安装依赖

```powershell
pip install -r requirements.txt
pip install -e ".[yaml,prom,mcp,retrospect,cron,dev]"
```

### 3. 跑测试

- **PowerShell**（推荐）：`.\scripts\run_tests.ps1` 或：
  ```powershell
  .\venv\Scripts\Activate.ps1
  python -m pytest tests
  ```
- 可选：只跑部分用例加快反馈，例如 `python -m pytest tests/unit -q`

### 4. CLI

激活 venv 后可直接用 `tm` 命令（由 pyproject.toml 的 `[project.scripts]` 安装）。

---

## 总结

| 项目 | Windows 注意点 |
|------|----------------|
| TraceMind | 用 `venv\Scripts\Activate.ps1` 激活；测试用 `python -m pytest` 或 `run_tests.ps1` |
