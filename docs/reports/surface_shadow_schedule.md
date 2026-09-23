# UI-P2-2 · SurfaceShadow 性能重构（排期说明）

> 状态：**已排期，未实施**（波次三仅完成 NT-P2-1 门控与 PK-P2-2 单实例；本项为审计指定的"单独排期"大工程）。
> 审计依据：`ui_render.md` — 同卡多 effect 叠加渲染代价 2.8×、全仓 53 处挂载点。

## 问题回顾

- `SurfaceShadow(QGraphicsEffect)` 每张卡片一个 effect；`WidgetMotion` 在 card/primary 场景挂载，
  并已有"祖先已有 effect 则不重复挂载"的规避；但实测同卡场景（嵌套卡片 + 悬停动画）仍有 ~2.8× 渲染代价。
- QGraphicsEffect 会强制离屏渲染与合成，代价随卡片数量线性增长（历史页/设置页大列表场景最明显）。

## 基线测量（实施前先做）

复用离屏渲染工具链（参考 `tests/test_visual_motion.py`、`.cluster/r5-redesign/capture_appearance.py`）：

1. 帧时探针：offscreen 下对目标页面连续 `render()` N 次取平均耗时；对比"挂 effect / 去 effect"两档。
2. 滚动手感：100 行任务表 + 滚动 5 屏，测 paint 事件总耗时（`QElapsedTimer` 包 `paintEvent`）。
3. 挂载点盘点：`rg 'SurfaceShadow|setGraphicsEffect' ui/` 输出 53 处清单，逐处标注"可去/需保留/需迁移"。

## 重构方案（建议分两步）

### 第一步：减挂载（低风险）
- 设置页卡片统一去 effect（设置页多数卡有页面底色衬托，阴影仅为装饰）。
- 悬停动画仅保留 wash 层，去掉 hover 时 effect 的二次触发（`self.shadow.hover = value; self.shadow.update()` 的每帧调用）。
- 预期收益：设置页与历史页大幅减少 effect 合成。

### 第二步：paintEvent 直绘（目标态）
- `draw_shadow()` 已是缓存 tile 九宫格（无实时 blur），可直接从 `QGraphicsEffect.draw` 迁移到
  卡片 `paintEvent`：卡片外扩 16/14/20 px 绘制阴影、内部正常绘制内容。
- 需要：卡片布局预留阴影边距（`make_card` 统一加 margin）或改由父级背景层绘制；主题切换时 tile 缓存键需含主题。
- 风险：视觉回归面广（全部卡片）、与 QFluentWidgets 原生样式叠加。**必须独立分支 + 视觉回归对比后合并。**

## 回归清单（实施 PR 必须全过）

- [ ] 暗/亮主题截图对比（capture_screenshots.py 全页）
- [ ] 悬停/按压动画（test_visual_motion.py）
- [ ] 主题切换即时生效（含缓存失效）
- [ ] 100 行表格滚动帧时对比（基线与改动后）
- [ ] load_smoke 吞吐不低于基线
- [ ] 全量回归 467+ 项

## 排期建议

放在"波次一/二修复全部上线并稳定运行一周"之后，作为独立重构专项（预估 1-2 个工作日 + 视觉回归半天）。
