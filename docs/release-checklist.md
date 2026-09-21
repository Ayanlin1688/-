# 发布检查单（Release Gate）

> 版本：3.3.0 起适用；每次对外发布前逐项过卡，全部 ☑ 方可分发。

## A. 法务与合规
- [ ] 开源许可方案落地（GPL：购买商业许可 / 迁移方案执行；见 `docs/compliance-gpl-options.md`）
- [ ] 用户协议与隐私政策律师审定（`legal/` 草稿）
- [ ] AIGC 标识口径确认（旁车标注已内置；可见标识按要求补齐）
- [ ] 商标 / 品牌素材版权确认（名称、图标、字体）

## B. 安全与信任
- [ ] 代码签名证书就绪（`YANLIN_SIGN_COMMAND` 演练通过，exe/zip/安装器均签名）
- [ ] 许可私钥保管与发码流程演练（`docs/licensing-upgrade.md`）
- [ ] 依赖锁定文件复核（`requirements.txt`），危险依赖变更审查
- [ ] 更新清单与实际产物一致性校验（版本 / 大小 / sha256）

## C. 质量与交付
- [ ] 全量回归通过（`python -X utf8 -m unittest discover -s tests`）
- [ ] 启动检查通过（`scripts/check_startup.py`）
- [ ] 负载冒烟通过（`scripts/load_smoke.py 300`；发布前建议追加真机千任务一次）
- [ ] 重打包 + 打包版 EXE 冒烟（`packaging/build_release.py`）
- [ ] 干净机演练：全新安装 → 启动 → 升级上一版 → 卸载 → 数据保留校验
- [ ] 便携包解压即用演练（含中文路径 / 空格路径）

## D. 业务验收
- [ ] 真实付费并发验收（建议最高档 5 条；确认费用后执行）
- [ ] 服务商契约澄清（如 H3 高分辨率档位与计费口径）
- [ ] 试点客户清单与反馈通道就绪

## E. 运营与支持
- [ ] 诊断包导出流程可用（设置 → 网络与数据 → 诊断包）
- [ ] 崩溃日志收集口径与反馈邮箱 / 群公告就绪
- [ ] 版本发布列车：tag → GitHub Release（附 zip + update-manifest.json）
- [ ] 回滚预案：上一版本安装包与数据兼容性说明

## 附：常用命令
```powershell
# 回归
python -X utf8 -m unittest discover -s tests
# 启动检查
python -X utf8 scripts/check_startup.py
# 负载冒烟（零费用）
python -X utf8 scripts/load_smoke.py 300
# 打包（可选签名）
python -X utf8 packaging\build_release.py
# 发码
python -X utf8 scripts\make_license.py --customer 客户名 --private-key <私钥.pem>
```
