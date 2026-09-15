# 打包与发布（packaging）

一键构建免安装版、便携 zip 与安装器脚本。用户端 **零 Python、零 Git**。

## 构建（开发机）

```powershell
# 在仓库根目录，使用项目运行环境
D:\fxaitool\conda\app\python.exe -X utf8 packaging\build_release.py            # 打包 + 便携 zip + 版本清单
D:\fxaitool\conda\app\python.exe -X utf8 packaging\build_release.py --installer # 额外构建 Inno Setup 安装器
```

产物（`packaging\dist\`）：

| 产物 | 说明 |
|---|---|
| `YanlinMatrix\` | PyInstaller onedir 免安装目录（双击 `YanlinMatrix.exe` 即用） |
| `YanlinMatrix-v<版本>-win64-portable.zip` | 便携包（解压即用） |
| `update-manifest.json` | 版本清单（版本 / 大小 / SHA-256），供后续发布与更新检查使用 |
| `YanlinMatrix-Setup-<版本>.exe` | Inno Setup 安装器（需本机安装 Inno Setup 6 后构建） |

## 安装器（Inno Setup）

- 脚本：`installer.iss`（中文 + 英文双语言、桌面图标可选、安装后可直接启动）。
- 本机未安装 Inno Setup 时构建脚本会跳过安装器并给出提示；安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php) 后重跑即可。
- 未签名的安装器在部分电脑上会提示「未知发布者」；正式分发前需要 **代码签名证书**（阶段 C · 签名项）。

## 数据目录

- 用户数据（配置 / 日志 / 提交账本 / 历史库 / 模型缓存）位于 `%APPDATA%\Yanlin\`；首次启动会自动从旧位置搬迁。
- 便携模式：设置环境变量 `YANLIN_CONFIG_DIR=<目录>` 可把配置与数据固定到任意自定义目录。

## 图标

`packaging\app.ico` 放置产品图标后，PyInstaller 与安装器会自动使用（当前未提供,欢迎后续补充品牌图标）。
