# 许可发码与密钥保管（YL2 非对称签名）

> 2026-09-21 起，激活码默认使用 **YL2 非对称签名**：厂商侧私钥签发，应用内嵌公钥验签；第三方无法伪造激活码。

## 一、密钥对（2026-09 生成）

| 材料 | 位置 | 说明 |
| --- | --- | --- |
| 私钥（PKCS#8 PEM，3072-bit RSA） | `.cluster\r16\licensing\yanlin-license-private-2026.pem`（工作区） | **绝不可提交 Git / 上传云盘 / 发送他人**；建议尽快移入密码管理器 / 加密移动盘，并做离线备份（丢失后所有未到期客户均无法补发同签） |
| 公钥 | 已内嵌于 `core/licensing.py`（`_VERIFY_PUBLIC_PEM`） | 随代码分发，无保密要求 |

## 二、发码

```powershell
# 正式发码（YL2）：
D:\fxaitool\conda\app\python.exe -X utf8 scripts\make_license.py `
  --customer "客户名称" --expires 2027-12-31 --private-key .cluster\r16\licensing\yanlin-license-private-2026.pem

# 演示 / 兼容（YL1，不推荐）：
python -X utf8 scripts\make_license.py --customer demo --legacy
```

- `--expires` 留空 = 永久；`--edition` 默认 `pro`。
- 激活：软件 → 设置 → 账号与许可 → 输入激活码。
- 验签失败 / 已过期均会给出明确错误；试用期与闸门逻辑不变（闸门默认演示模式，发售前沿用一个开关启用拦截）。

## 三、密钥轮换（预演）

1. 生成新密钥对（可用同一工具流程，见 `.cluster\r16\gen_keys.py` 逻辑或 RSA 标准流程）。
2. 新公钥替换 `core/licensing.py` 中 `_VERIFY_PUBLIC_PEM`，应用发版。
3. 新客户一律用新私钥发码；**旧版应用**（只含旧公钥）需补发旧签名码，或在升级后换发新码。
4. 建议轮换周期 ≥ 24 个月，或私钥疑泄露时立即执行。

## 四、安全注意

- YL1 为共享密钥格式（演示用），正式对外不再发放；日后可择机移除兼容逻辑。
- 私钥使用时不落盘、不进日志；`scripts/make_license.py` 不会打印私钥内容。
- 发码记录建议台账化（客户 / 到期 / 经手人），便于吊销协商与续费管理（在线激活与吊销为后续批次规划项）。
