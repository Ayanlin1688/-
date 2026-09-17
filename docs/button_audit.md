# 业务按钮功能审计

审计日期：2026-09-17。范围：`ui/pages/settings_page.py`、`ui/pages/history_page.py`、`ui/main_window.py`，包含主窗口跨页连接和动态生成的站点、模型、历史操作。

按钮数量不能固定按“26 个”计算：站点数量、模型池行数、历史状态会改变运行时按钮数；同一个构造位置也可生成多个按钮。以下按**业务按钮模板**列出所有入口，分类导航逐项列出。复选框、开关、下拉框、数字步进器、文本清空图标属于输入控件，使用 `checkedChanged`、`stateChanged`、`valueChanged`、`currentIndexChanged` 等信号，不应错误要求全部具有 `clicked.connect`。

## 验证方式

- **点击验证**：新建隔离配置的 `MainWindow`，对实际实例执行 `click()` 或 `QTest.mouseClick()`，断言页面、持久配置或业务调用发生变化。不是直接调用槽函数冒充点击。
- **边界替身点击验证**：按钮与业务槽真实运行，仅替换 API、Git 或系统打开文件边界；验证被调用和 UI 恢复。此验证不代表真实网络、Git 推送或系统播放器已经成功。
- **静态审查**：核对构造、连接、信号转发和最终函数；独立静态证据不能证明用户操作已经成功。
- 新测试 `tests/test_button_audit.py` 全程拦截 `requests.Session.request`，使用临时配置；不访问外网、不调用视频创建、不运行 Git，不读取真实密钥。

## 设置页

| 按钮/位置 | 连接函数 | 行为 | 证据 |
| --- | --- | --- | --- |
| 许可与激活导航 | `_jump_to_group('license')` | 切换许可页 | 点击验证 |
| 外观与语言导航 | `_jump_to_group('appearance')` | 切换外观页 | 点击验证 |
| 模型池导航 | `_jump_to_group('pool')` | 切换模型池页 | 点击验证 |
| 默认参数导航 | `_jump_to_group('defaults')` | 切换默认参数页 | 点击验证 |
| 中转站管理导航 | `_jump_to_group('stations')` | 切换站点总览 | 点击验证 |
| 当前线路导航 | `_jump_to_group('api')` | 切换实际 API 配置页 | 点击验证 |
| GitHub 同步导航 | `_jump_to_group('sync')` | 切换代码同步页 | 点击验证 |
| 任务策略导航 | `_jump_to_group('task')` | 切换并发/重试/未匹配策略页 | 点击验证 |
| 定时执行导航 | `_jump_to_group('schedule')` | 切换定时页 | 点击验证 |
| 左侧“+ 新增中转站” | `_open_station_editor(None)` | 清空编辑器并切到新增页 | 点击验证 |
| 左侧动态站点名称 | `_focus_station(station_id)` | 切到对应站点，更新导航高亮 | 点击验证 |
| 总览“新增中转站” | `_open_station_editor(None)` | 打开新增站点编辑器 | 点击验证 |
| 把当前 API 配置保存为中转站 | `_capture_current_station()` | 复制当前线路配置到本地站点列表 | 点击验证并重新读取配置 |
| 新增页“保存” | `_save_station_editor()` | 校验名称、保存站点并返回总览；空名称记录警告 | 点击验证有效输入；空名称分支静态审查 |
| 新增页“取消” | `_close_station_editor()` | 放弃未保存编辑，返回总览 | 点击验证 |
| 站点总览每行“编辑” | `_open_station_editor(station_id)` | 打开该站点独立编辑页 | 点击验证 |
| 站点总览每行“设为当前” | `_set_current_station(station_id)` | 原子更新当前 API 配置并通知模型目录切换账号 | 点击验证配置与运行时目录账号 |
| 站点总览每行删除图标 | `_delete_station(station_id)` | 从本地列表删除该站点；删除当前站点时清除当前标识 | 点击验证 |
| 独立站点页“保存修改” | `_save_station_page(...)` | 保存该站点；当前站点修改同时更新实际 API 连接 | 点击验证；当前站点配置同步另有回归测试 |
| 独立站点页“设为当前” | `_set_current_station(station_id)` | 使用此站点作为实际线路 | 点击验证 |
| 独立站点页“删除该中转站” | `_delete_station(station_id)` | 删除该站点并回总览 | 点击验证 |
| API Key 眼睛图标 | `_toggle_secret(api_key, button)` | 在密码/明文显示间切换，不修改值 | 点击验证两次往返 |
| 上传 Token 眼睛图标 | `_toggle_secret(upload_key, button)` | 在密码/明文显示间切换，不修改值 | 点击验证两次往返 |
| 测试连接 | `test_connection()` | 后台检查视频接口与微型图片上传，分别展示结果；结束恢复按钮 | 边界替身点击验证；底层 HTTP 另有本地测试 |
| 同步上游模型 | **MainWindow 中连接** `model_catalog.refresh()` | 拉取模型列表、更新缓存及模型选项 | 边界替身点击验证，断言目录变为 upstream |
| 添加模型 | `_add_model_row('video-v3', True, '健康')` | 新增模型池行并持久化 | 点击验证 |
| 模型池每行删除图标 | `_remove_model_row(row)` | 先移除 Python 行引用再销毁控件、更新配置 | 点击验证 |
| 许可页“激活” | `_activate_license()` | 打开输入对话框、校验激活码并保存状态 | 点击取消与本地测试码激活均验证 |
| 激活对话框“激活/取消” | QFluentWidgets 的 `accept()/reject()` | 返回输入结果或取消，调用方决定是否写配置 | 接受按钮真实点击；取消返回路径验证 |
| 同步到 GitHub | `sync_github()` → `RepositorySync.sync()` | 后台运行带凭据保护的同步流程，成功/失败均恢复按钮 | Git 边界替身点击验证；未实际执行 Git |

## 历史页

| 按钮 | 连接函数/转发链 | 行为 | 证据 |
| --- | --- | --- | --- |
| 打开输出文件夹 | `open_output()` → `open_local(..., folder=True)` | 优先打开当前选中任务输出目录，否则配置目录 | 点击验证到系统边界；未启动文件管理器 |
| 空态“去工作台创建任务” | `create_requested` → MainWindow `switchTo(workspace_page)` | 返回工作台 | 点击验证当前页面 |
| 每行打开文件 | `open_local(task.result_path)` | 打开对应输出；没有路径时禁用 | 点击验证到系统边界，并验证禁用状态 |
| 每行重新查询并下载 | `redownload_requested` → `workspace.redownload(task)` | 沿用现有 task_id 查询与下载；没有 task_id 时禁用 | 点击验证跨页业务参数；完整下载另有本地 HTTP 测试 |
| 待确认记录“处理待确认提交” | `resolve_requested` → `workspace.resolve_submission(task)` | 打开提交结果核对流程 | 点击验证跨页业务参数；确认规则另有专门测试 |
| 完成/重复记录“重新生成” | `regenerate_requested` → `workspace.regenerate(task)` | 进入需要确认的新任务生成流程 | 点击验证跨页业务参数；测试不创建付费任务 |

## 主窗口

| 按钮 | 连接函数 | 行为 | 证据 |
| --- | --- | --- | --- |
| 工作台/历史/设置三导航 | `addSubInterface` 注册的导航回调 | 切换对应页面 | 三入口鼠标点击验证 |
| 左上展开/收起按钮 | `_toggle_navigation()` | 展开或折叠，保存 `appearance.nav_expanded` | 点击往返验证配置 |
| 顶部品牌标志 | `insertWidget(..., onClick=...)` → `switchTo(workspace_page)` | 回工作台 | 鼠标点击验证 |
| 底部用户头像 | `addWidget(..., onClick=show_about)` | 打开版本/关于信息 | 鼠标点击验证，对话框执行边界替身，并断言品牌图片真实加入布局 |
| 关于导航兼容入口 | `addItem(..., onClick=show_about)` | 与头像共用关于功能；此入口主动隐藏 | 静态审查，不作为可见空按钮统计 |
| 最大化/还原 | `_studio_toggle_maximized()` | Qt 状态切换及延迟校验，避免快速连点竞态 | 点击往返验证；快速双击逻辑另有回归 |
| 最小化 | QFluentWidgets 标题栏连接 → `showMinimized()` | 最小化窗口 | 点击验证 |
| 关闭 | QFluentWidgets 标题栏连接 → `close()` → `closeEvent()` | 安全停止后台后关闭 | 空闲状态点击验证；不声称实测所有后台退出情形 |
| 框架返回按钮 | 未作为业务入口使用；主动 `hide()` | 当前产品用固定三页导航，无独立返回操作 | 静态审查，隐藏入口不算空按钮 |

## 发现与修复

发现“激活”虽然有 `clicked.connect`，槽函数仍会崩溃：当前 QFluentWidgets `Dialog` 没有 `viewLayout`，点击后产生 `AttributeError`，被全局异常处理拦截，表现为无反应。已改用该组件实际提供的 `textLayout`。新增测试先确认失败，再验证取消不改配置、真实对话框输入本地测试码并点击激活后持久化成功。

关于对话框也误用 `viewLayout`；异常被局部捕获导致品牌图标丢失。已同样改为 `textLayout`，头像点击测试断言布局第一项为有效的 56 像素品牌图片。

“同步上游模型”在 `settings_page.py` 本身没有连接，但在 `MainWindow.__init__` 完成跨页绑定；本次点击测试已验证，不能根据单文件搜索把它判断为空按钮。站点与模型行使用 lambda 捕获本行 ID/控件；本次点击不同动态行并校验实际修改对象，避免只检查“有连接”却漏掉错行操作。

未发现其他缺少功能连接的可见业务按钮。此结论限定在上述代码范围与测试输入；不等同于真实服务商、GitHub 授权和各 Windows 桌面环境全部成功。

## 执行与已有证据

```text
python -X utf8 -m unittest discover -s tests -p test_button_audit.py -v
```

新增 11 项测试覆盖上述点击与边界。完整回归还应包含：

- `test_review_stations.py`：活动站点保存后真实运行时配置及模型目录账号更新（直接业务方法测试）。
- `test_ui_interactions.py`：页面点击、输入持久化、匹配增图/调序、日志过滤导出（混合点击与直接方法测试）。
- `test_product_batches.py`：历史重新下载沿用原 task_id、本地 HTTP 产品输出验证（直接业务方法测试）。
- `test_window_controls.py`：最大化、还原、快速切换竞态（直接方法测试）。
- `test_submission_recovery.py`：提交恢复对话框默认不授权重提、确认输入验证。
- `test_licensing.py`：测试码格式、篡改、过期、试用与执行限制验证。

这些既有测试不能替代手动真实服务验收；最终数量和通过/失败以本轮完整测试报告为准。
