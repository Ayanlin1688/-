# 许可发码与密钥保管（YL2 非对称签名）

> 2026-09-21 起，激活码默认使用 **YL2 非对称签名**：厂商侧私钥签发，应用内嵌公钥验签；第三方无法伪造激活码。

## 一、密钥对（2026-10 生成）

| 材料 | 位置 | 说明 |
| --- | --- | --- |
| 私钥（PKCS#8 PEM，3072-bit RSA） | `.cluster\licensing\yanlin-license-private-2026-10.pem`（本地保管） | **绝不可提交 Git / 上传云盘 / 发送他人**；`.cluster\licensing\` 目录不入库；建议移入密码管理器 / 加密移动盘，并做离线备份 |
| 公钥 | 已内嵌于 `core/licensing.py`（`_VERIFY_PUBLIC_PEM`） | 随代码分发，无保密要求；SPKI DER SHA256：`579259ae20ed1c376d17705d3fb72849ed10e86bc3db55d1e4c6f494320f8427` |

## 二、发码

```powershell
# 正式发码（YL2）：
D:\fxaitool\conda\app\python.exe -X utf8 scripts\make_license.py `
  --customer "客户名称" --expires 2027-12-31 --private-key .cluster\licensing\yanlin-license-private-2026-10.pem

# 演示 / 兼容（YL1，不推荐）：
python -X utf8 scripts\make_license.py --customer demo --legacy
```

- `--expires` 留空 = 永久；`--edition` 默认 `pro`。
- 激活：软件 → 设置 → 账号与许可 → 输入激活码。
- 验签失败 / 已过期均会给出明确错误；试用期与闸门逻辑不变（闸门默认演示模式，发售前沿用一个开关启用拦截）。

## 三、对外发码工具与台账

正式对外发码使用 `scripts/issue_license.py`。工具会校验私钥与应用内嵌公钥匹配，输出激活码，并将签发记录追加到 `.cluster\licensing\license-ledger.csv`。私钥和台账均被 `.gitignore` 保护。

```powershell
# 永久码：
python -X utf8 scripts/issue_license.py --customer "客户名称"

# 一年期码：
python -X utf8 scripts/issue_license.py --customer "客户名称" `
  --expires (Get-Date).AddYears(1).ToString('yyyy-MM-dd')

# PowerShell 中文客户名推荐使用 UTF-8 文件，避免控制台编码影响：
[IO.File]::WriteAllText('.cluster\licensing\customer.txt', '中文客户名称', [Text.UTF8Encoding]::new($false))
python -X utf8 scripts/issue_license.py --customer-file .cluster\licensing\customer.txt

# 离线验证（不需要联网）：
python -X utf8 scripts/issue_license.py --verify "YL2..."
```

- 私钥默认使用环境变量 `YANLIN_LICENSE_KEY`，未设置时使用 `.cluster\licensing\yanlin-license-private-2026-10.pem`。
- `--expires` 留空表示永久；限期客户续费时重新签发新码并更新客户侧激活码。离线许可没有在线吊销能力，旧码需通过换发或升级策略处理。
- 台账包含签发时间、客户、版本、到期日、激活码、操作员和公钥 SHA256 指纹；不得提交 Git、上传云盘或发送给客户。

## 四、2026-10 密钥轮换（已落地）

1. 2026-10 新密钥对已生成，新公钥已替换 `core/licensing.py` 中的 `_VERIFY_PUBLIC_PEM` 并随新包发版。
2. 新私钥按 `.cluster\licensing\` 约定保管，该目录不入库。
3. 2026-09 旧公钥已作废；新客户一律使用 2026-10 私钥签发新码。
4. 使用旧安装包的客户必须先升级到新包，再换发新激活码；旧包无法验证新公钥签名。
5. 后续建议轮换周期 ≥ 24 个月，或私钥疑泄露时立即执行。

## 五、安全注意

- YL1 为共享密钥格式（演示用），正式对外不再发放；日后可择机移除兼容逻辑。
- 私钥使用时不落盘、不进日志；`scripts/make_license.py` 不会打印私钥内容。
- 发码记录建议台账化（客户 / 到期 / 经手人），便于吊销协商与续费管理（在线激活与吊销为后续批次规划项）。
