# StoryboardVideoStudio · 阶段3

Windows 桌面批量视频生成工具，使用 Python 3.12、PyQt5 和 QFluentWidgets。支持多参考图自动匹配、单模型提交、轮询下载、产品子文件夹批处理、定时执行和私人 GitHub 仓库同步。

## 安装与启动

安装 Python 3.12 和 Git for Windows。私人仓库需要先通过 Git Credential Manager 或 `gh auth login` 登录有访问权限的 GitHub 账号。

```bat
git clone https://github.com/admin11044/StoryboardVideoStudio.git
cd StoryboardVideoStudio
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python main.py
```

首次启动使用默认配置，无需额外模板或素材。设置和任务历史保存在自动创建的 `config.json`，该文件不随仓库分发。项目保持 PyQt5 + PyQt-Fluent-Widgets，不要混装 PySide 版本的 Fluent 库。

## 使用流程

1. 设置页填写视频 API 地址、API Key、图床上传 URL 和上传 Token。“测试连接”分别检查视频 API 和实际上传一张微型测试图，不创建视频任务。主 API Key 并非所有图床都接受的上传凭据。
2. 选择提示词根目录、参考图根目录和视频保存目录。提示词为 UTF-8 `.txt`，图片支持 JPG/JPEG/PNG/WebP 和中文路径。
3. 查看“匹配详情”中的全部图片，可拖拽调序、添加、移除并保存；“重新自动匹配”可恢复自动结果。
4. 选择模型、比例、分辨率和时长，再点击“开始生成”，或设置定时执行。
5. 任务历史支持状态筛选、产品列、打开目录和重新查询下载。重新下载沿用原 task_id，不创建新任务。

暂停会在当前任务结束后停到下一条之前；取消或跳过停止本地后续处理，不代表服务端取消。关闭窗口时等待后台请求返回或超时，避免销毁仍在运行的线程。

### 产品子文件夹

提示词和参考图根目录下的同名子文件夹对应一个产品：

```text
提示词/
  玫瑰毯子/
    01_展示.txt
    02_细节.txt
  车载风扇/
    01_展示.txt
参考图/
  玫瑰毯子/
    1(1).jpg
    1(2).jpg
    1(3).jpg
    2(1).jpg
  车载风扇/
    1(1).jpg
视频保存/
  玫瑰毯子/
    001_01_展示.mp4
    002_02_细节.mp4
  车载风扇/
    001_01_展示.mp4
```

产品按文件夹名自然排序，产品内部按提示词相对路径自然排序；当前产品全部结束后自动处理下一个。自然排序让“产品2”排在“产品10”之前。递归扫描仅在各产品内部进行，参考图不会跨产品绑定。同名参考图子文件夹缺失时，日志警告并跳过整个产品，即使选择了文生视频策略也不会提交该产品。

提示词根目录直接包含 TXT 时仍按旧版流程处理（保留递归设置和手动图片选择）；根目录 TXT 和产品子文件夹混用时，根目录任务显示为“未分组”，只匹配参考图根目录的直接图片。队列支持折叠产品组，工作台顶部显示当前产品和总进度。“已完成产品”表示该产品的任务均已结束，包括失败、跳过和取消；成功/失败数量另行统计。

### 多图匹配与参数

每个提示词依次按“完全匹配 → 前缀匹配 → 序号匹配”取第一个命中层级的全部图片，再按文件名自然排序。例如 `玫瑰毯子1.txt` 或 `01_展示.txt` 自动绑定 `1(1).jpg`、`1(2).jpg`、`1(3).jpg`。编号 1 不匹配编号 10；同系列 JPG/PNG 都会保留。Picture 1 对应 `images[0]`，保存的手动顺序优先。

`<Picture 1>`、`<Picture1>`、`<Picture 1 >`、`@Image1` 等转换为模型要求的参考图标记。引用编号大于图片数量时，日志警告可能影响生成质量。未匹配提示词策略支持跳过、文生视频或暂停；暂停后点击继续会按文生视频提交该条，也可跳过。

| 接口模型名 | 主要参数约束 |
| --- | --- |
| video-v1 | `/video/generations`，duration 为 5/10/15，不发送分辨率和音频 |
| video-v2 / video-v2-fast | `/videos`，duration 为 5/10/15，480p/720p/1080p，支持音频 |
| video-v3 / seedance-2.5 / seedance2.5 / sd-2.5 / sd2.5 | `/videos`，4–30 秒，720p，16:9/1:1/9:16，支持音频与可选 seed |
| MiniMax-H3 | `/videos`，4–15 秒；480p/768p/1080p/2K/4K；9:16 + 1080p 提交 `size=1088x1920` |
| grok-imagine-1.5-video | multipart `/videos`，1–15 秒，重复 `input_reference` 文件字段，多图仅 480p/720p |

选项随模型约束更新，非法参数在上传和提交前拦截。每次使用工作台选中的一个模型；模型池轮询、自动故障转移、视频自动重试和失败阈值尚不参与调度，相关设置暂作保留。上传失败自动重试一次，视频创建不自动重试。已有相同输入、参数和 task_id 的任务会被保留，避免重复扣费。

## 定时执行

“设置 → 定时执行”中选择时间、仅一次/每天重复、队列结束后保持运行/自动关闭，然后启用开关。工作台顶部显示下次执行时间，关闭开关立即取消。

- 启动后通过 HTTPS 时间响应校准软件内部时钟，设置页显示当前时间、来源和偏差；不修改 Windows 系统时钟。网络不可用时警告并使用系统时间。
- 到点复用“开始生成”的完整流程；已有队列或重新下载正在运行时记录日志并跳过该次。跨过开始前五分钟时提醒一次，设置不足五分钟的定时不会给出错误提醒。
- 执行前先持久化消费本次定时，防止重启后重复提交。一次模式执行后关闭开关，每天模式安排下一天。软件关闭期间错过的定时不补交。
- 自动关闭仅作用于定时启动的非空队列；手动取消后保持运行。后台工作未结束时仍按安全退出流程等待。
- 最小化或切换其他软件不影响触发；需要软件持续运行、电脑保持唤醒。关机、退出或系统睡眠期间无法执行；从睡眠恢复后若跨过时刻，会在下次时钟检查中消费当次事件，不补跑多个历史日期。

每天定时处理当前目录中的任务；已有远端 task_id 的相同任务沿用原记录，不每天重新生成相同素材。增加提示词或修改有效输入后会创建新任务。

## GitHub 同步

目标为私人仓库 `admin11044/StoryboardVideoStudio`，分支 `main`。设置页“同步到GitHub”按钮在后台检查敏感文件、执行 add/commit/push 并核对远端提交；推送失败间隔 2 秒重试，最多 3 次，结果用 InfoBar 和日志提示。

```bat
python -X utf8 scripts\sync_github.py
```

自定义说明可写入 UTF-8 文件，然后执行：

```bat
python -X utf8 scripts\sync_github.py --message-file temp\commit-message.txt
```

说明使用中文，前缀为 `feat:`、`fix:`、`refactor:`、`docs:`。没有代码改动时也会推送已有本地提交。Git 登录由系统凭据工具处理，首次认证请在终端完成 `gh auth login` 和 `gh auth setup-git`，或通过 Git Credential Manager 登录。应用内后台同步不会弹终端索取密码。网络需要代理时配置本机 Git 代理，不把凭据写进 origin URL。

`AGENTS.md` 已记录后续每批代码修改验证完成后自动提交推送的开发约定。软件不监视磁盘并提交正在编辑的半成品；设置变更只写本地配置。

`.gitignore` 排除 `config.json`、密钥/环境文件、视频、日志、缓存、截图、临时文件和本地诊断产物；同步服务还检查实际暂存内容中的本地凭据。不要将密钥写入源代码。同步失败会保留本地提交，修复网络或登录后可重试，不会强制覆盖远端。

## 配置与诊断

配置采用临时文件加原子替换。`paths` 保存目录，`match_overrides` 保存绑定图片及顺序，`history` 保存产品、task_id、原接口、输出路径和状态，`schedule` 保存定时时间及已消费事件。

下载命名支持 `{序号}`、`{提示词名}`、`{模型}`、`{日期}`、`{时间}`、`{task_id}`；每个产品内重新计数。默认不覆盖已有文件，完整下载后才保存最终文件。非法文件名会清理，输出不能越过选择的目录。

开启“设置 → API 配置 → 调试模式”后显示 DEBUG 日志和已提交图片数：原图路径/尺寸/格式/字节数、上传 URL、提示词替换前后、实际 `images` 和脱敏请求体。额外匿名下载上传图片核对尺寸/格式/哈希，最多 32MB；诊断异常只警告，不修改图片或中断原流程。关闭后界面和导出只显示 INFO 及以上级别。

图床 URL 从配置读取，multipart 字段为 `files`，边界由 requests 生成。默认图床使用 Bearer 和 `X-Upload-Token`，优先单独上传凭据。自定义图床不会自动收到主 API Key。上传响应兼容 `files[0].url`、`data.files[0].url`、`url`、`data.url`，超时为连接 15 秒/读取 60 秒。

## 项目结构

```text
main.py                    程序入口
requirements.txt           Python 依赖
core/                      配置、匹配、任务、API、下载、定时、校时、Git同步
ui/
  main_window.py           Fluent 主窗口
  pages/                   工作台、历史、设置
  widgets/                 队列、参数、数据源、当前任务、日志等
  components/              匹配详情与共用控件
scripts/                   启动检查、截图、同步及验收工具
tests/                     单元、真实本地HTTP、Qt界面集成测试
AGENTS.md                  增量开发和自动同步约定
.gitignore                 敏感文件和生成产物排除规则
```

## 验证

```bat
python -X utf8 -m unittest discover -s tests -v
python -m compileall -q core ui scripts tests main.py
python scripts\check_startup.py
python -X utf8 scripts\verify_stage3.py --wait-minute
python -X utf8 scripts\capture_screenshots.py
```

阶段3验收使用独立配置和本机 HTTP 服务，真实等待 60–120 秒到 UI 选择的整分钟，在最小化状态自动处理“玫瑰毯子 + 车载风扇”，验证三图顺序、上传/提交/轮询、分产品下载和历史。不会调用收费平台；下载为固定测试字节，不代表服务商视频生成结果。结果和日志位于本地 `artifacts/stage3/`。去掉 `--wait-minute` 可快速触发相同本地流程。

截图脚本使用标明为示例的独立配置和本地绘制图片，更新 `screenshots/` 原六张 PNG 及 `screenshots.zip`。截图、真实素材和平台诊断产物均不上传 GitHub。

需要真实服务商冒烟测试时，先填写本地配置：

```bat
python -X utf8 scripts\run_live_smoke.py
python -X utf8 scripts\run_live_smoke.py --submit
```

第一条只检查配置，第二条可能产生平台费用，最多处理首个提示词。已有相同远端任务会保留；真实结果保存在本地 `live-smoke-result.json`，不分发到仓库。
