# StoryboardVideoStudio · 阶段2A

保留阶段1的 Fluent 界面，现已实现：扫描提示词和图片 → 匹配和手动绑定 → 上传图片 → 所选单模型提交 → 后台轮询 → 下载视频 → 保存历史。

## 启动

在 `D:\bendihua` 执行：

```bat
python -m pip install -r requirements.txt --upgrade
python main.py
```

当前沿用阶段1的 PyQt5 + PyQt-Fluent-Widgets 1.11.3，所有主要交互控件使用 QFluentWidgets。没有混用 PySide6，也没有原生控件降级分支。默认窗口为 1400×900，三面板比例为 18:58:14；数据源与生成参数左右并排、各占一半，当前任务在下方占满宽度。导航默认折叠为图标。

## 使用

1. 在设置中填写 API Base URL、API Key，以及服务商提供的图床上传凭据，点击“测试连接”。视频 API 使用模型列表请求检查，上传 API 则实际上传一张微型 PNG；两项结果分别显示，不创建视频任务。密钥只在本地输入。视频接口通过不代表上传权限、余额和指定模型调用均已通过。
2. 选择提示词目录、图片目录和视频保存目录。扫描在后台进行；提示词使用 UTF-8 文本，支持中文路径、`.txt`、`.jpg/.jpeg/.png/.webp`。无图片且采用文生视频策略时可留空图片目录。
3. 软件按“完全匹配 → 前缀匹配 → 序号匹配”收集命中层级的全部图片，并按文件名自然排序。例如 `玫瑰毯子1.txt` 自动匹配 `1(1).jpg`、`1(2).jpg`、`1(3).jpg`。检查“匹配详情”中的数量和 Picture 编号；拖动图片行改变顺序，支持添加、移除和保存，手动清空绑定也会持久化。
4. 在工作台选择模型、比例、分辨率、时长，点击“开始生成”。队列按顺序执行一个模型；图片上传失败会等待 2 秒后重试一次，仍失败则不提交该视频任务，继续下一条。视频创建本身不自动重试。
5. 在任务历史中打开文件或输出目录。同步图标表示“重新查询并下载”，沿用已有 task_id，不会创建新任务。若已更改 API 地址，先切回记录对应接口及密钥。

暂停会等待当前任务完成，再停在下一条前；继续恢复队列。取消和跳过会停止本地轮询及后续处理，不代表服务端取消。正在进行的 HTTP 请求会等返回或超时后退出；关闭窗口也会等待后台线程安全结束。

未匹配策略支持“跳过并警告”“仍提交文生视频”“暂停任务”。选“暂停任务”时，日志会提示：点击继续将把该条作为文生视频提交，也可跳过或取消。

## 模型接口

| 模型 | 提交方式 | 参数约束 |
| --- | --- | --- |
| video-v1 | JSON `/video/generations` | duration 仅 5/10/15；5 种 aspect_ratio；不发送分辨率/音频字段 |
| video-v2 / video-v2-fast | JSON `/videos` | duration 仅 5/10/15；6 种 aspect_ratio；480p/720p/1080p；音频开关实际生效 |
| video-v3 / seedance-2.5 / seedance2.5 / sd-2.5 / sd2.5 | JSON `/videos` | duration 为 4–30 整数；720p；ratio 为 16:9/1:1/9:16；支持音频开关及可选整数 seed |
| MiniMax-H3 | JSON `/videos` | seconds 为 4–15 整数；8 种比例对应精确像素 size，9:16+1080p 为 1088x1920；2K/4K 才附带 aspect_ratio |
| grok-imagine-1.5-video | multipart `/videos` | seconds 为 1–15；7 种比例；重复 input_reference 上传真实文件；多图只支持 480p/720p |

`<Picture N>`、`<PictureN>`、`<图片N>`、`@ImageN`、`@图片N` 统一转换为 `@参考图N`；v2/v2-fast 提交时进一步转换为文档规定的 `@ImageN`。

工作台和默认参数选项随模型限制比例、分辨率和时长；H3 只提供 480p/768p/1080p/2K/4K，并显示最终像素尺寸。切换模型时会调整不兼容的选择并提示。v2/v3 音频开关生效，v3/Seedance 支持 0–4294967295 的可选 seed；其他模型的这两个控件禁用且不发送。参数不合法或提示词为空时，在上传前记录错误，不调用上传或视频 API。完整参数表见 [模型参数修复记录](docs/model-parameters-validation.md)。

模型池、故障转移、视频自动重试和失败阈值尚不参与调度；相关设置仅保留供阶段2B使用。默认参数与外观设置保留持久化行为。

## 参考图调试

在“设置 → API 配置 → 调试模式”开启诊断（默认关闭），工作台日志可选“调试”级别。开启后记录提示词文件、绑定来源/数量/顺序、每张原图的路径/尺寸/格式/字节数、上传 URL、替换前后及最终提示词、实际请求体。创建成功后，当前任务显示“已提交X张参考图”。请求体在 JSON 编码前遮蔽视频和上传凭据，不影响真正发送的内容。

调试模式会额外下载每张上传图片，比较尺寸、格式、字节数和 SHA256；最多检查 32MB，连接/读取超时为 15/60 秒。检查独立进行，不使用上传会话、API Key、netrc 或 Cookie；诊断失败仅警告，不改变上传或中断生成。关闭后不新增 DEBUG 或额外图片检查，界面和导出隐藏原有 DEBUG。提示词引用超过绑定数量时，无论调试开关是否打开，都会警告。

自动匹配先找完整文件名（不含扩展名），没有命中再找提示词名前缀，仍未命中则按提示词结尾/开头的数字匹配图片开头的系列数字；没有数字的提示词使用自然排序后的序号。每级收集全部命中项，不再取图片列表第 N 个文件兜底。`1(2)` 排在 `1(10)` 前，编号1不匹配编号10。忽略空白、大小写及全角括号差异。完全匹配命中后不合并下一层的前缀图片。

手动保存的绑定及顺序优先；已有手动绑定可在“匹配详情”点击“重新自动匹配 → 保存调整”更新。Picture 1 对应 `images[0]`，拖拽后编号随顺序更新。文件夹中同系列的 JPG/PNG 等不同文件全部保留，不按扩展名静默丢弃；如果只需 JPG，可使用仅放 JPG 的素材目录。支持 `<Picture 1 >` 等带空格标记；按完整数字一次替换，避免 Picture 1/10 互相干扰。数量不足示例：`提示词引用了Picture 5，但只绑定了3张图，可能影响生成质量`。

真实玫瑰毯子三图诊断记录见 [参考图排查报告](docs/reference-debug-investigation.md)。`artifacts/reference-debug/request.json` 可用于与手动提交逐字段对比；`execution.log` 保存全部日志。`scripts/run_reference_debug.py --submit` 已执行一次，检测到本次 task_id 后拒绝重复提交。下面两条只读取已有证据，不创建视频任务：

```bat
python -X utf8 scripts\verify_reference_evidence.py
python -X utf8 scripts\capture_reference_debug.py
```

自动匹配修复后的真实验证见 [同系列多图验收](docs/series-matching-validation.md)。新证据位于 `artifacts/reference-automatch`，使用用户确认的三张原始 JPG 的独立副本，`match_overrides` 为空，完全走正常扫描/匹配/上传/提交链路。该模式不会改用户日常配置，也不会混入源目录中的两张 PNG。

```bat
python -X utf8 scripts\run_reference_debug.py --auto-match
python -X utf8 scripts\verify_reference_evidence.py --auto-match
python -X utf8 scripts\capture_reference_debug.py --auto-match
```

以上命令不提交视频。真实调用命令 `python -X utf8 scripts\run_reference_debug.py --auto-match --submit` 已执行一次；存在该次 task_id 时拒绝重复付费提交。

## 配置和文件

项目根目录 `config.json` 自动保存配置和历史，使用临时文件加原子替换。API Key 保存在该本地文件中，截图和测试使用独立配置。

- `match_overrides`：按提示词绝对路径保存手动绑定和顺序。
- `history`：保存远端 task_id、接口地址、任务状态、原序号、输出路径和错误；下载失败仍保留 task_id。
- `scan_settings.recursive`：默认 true；改为 false 则只扫描目录顶层。
- `download_settings.overwrite_existing`：默认 false，同名文件跳过；设为 true 可覆盖。失败下载不替换已有文件。
- `download_settings.naming_rule`：默认 `{序号}_{提示词名}.mp4`；与设置页中原有 `task_strategy.naming_rule` 同步。
- `workspace.poll_interval`：轮询间隔，单位秒；`workspace.poll_timeout` 可选，默认最长等待 3600 秒。

命名规则支持 `{序号}`（如 003）、`{提示词名}`、`{模型}`、`{日期}`（YYYYMMDD）、`{时间}`（HHMMSS）、`{task_id}`。自动移除 Windows 非法文件名字符，禁止越出输出目录。下载使用同目录临时文件，收到完整内容后再保存最终文件。

重复点击开始时，已存在 task_id 的相同输入与生成参数会被保留，避免重复付费提交；修改轮询间隔、输出目录或未启用的设置不会触发新提交。提示词或图片内容/文件信息、有效生成参数发生变化后会被视为新任务。无 task_id 的失败记录可在下一次手动开始时重新尝试，阶段2A不会自动重试。

图床默认 `https://video.kkone.vip/api/uploads`，请求地址从配置读取；使用 `multipart/form-data` 的 `files` 字段，Content-Type 边界由 requests 自动生成。优先使用“图床 API Key（上传 Token）”；默认图床同时发送 `Authorization: Bearer <上传凭据>` 和服务端声明的 `X-Upload-Token`。该字段留空时会尝试主 API Key，是否接受由服务商权限决定。自定义图床只发送明确填写的图床 Key，留空则匿名上传，不向其他站点发送主 API Key。上传响应兼容 `files[0].url`、`data.files[0].url`、`url`、`data.url`。

上传请求使用连接 15 秒、读取 60 秒超时；失败后等待 2 秒重试一次，重试可被取消。其他 HTTP 请求继续使用连接 10 秒、读取 30 秒超时。下载地址不会携带主 API Key。

## 验证与截图

```bat
python -X utf8 -m unittest discover -s tests -v
python -m compileall -q core ui scripts tests main.py
python scripts\check_startup.py
python -X utf8 scripts\capture_screenshots.py
```

测试使用本机 HTTP 服务，实际执行 multipart/JSON 请求、状态轮询、文件下载和界面交互；覆盖各模型、失败继续、下载恢复、暂停/继续/取消/跳过、配置重开回填及安全退出。这里的下载内容是测试字节，不能作为真实视频生成成功的证据。

截图脚本覆盖 `screenshots` 中原有六个同名 PNG，同时更新 `screenshots.zip`。截图使用明确标注的本地示例任务和真实 PNG 素材，不写入用户配置、不展示密钥，不代表服务商生成结果。主窗口 1600×1100，匹配对话框 900×650。离屏运行时显式注册系统微软雅黑，避免字体空白。

2026-09-11 后续真实平台测试已通过图片上传，此前的上传 401 不是当前状态。H3 尺寸修复后，`size=1088x1920` 已创建任务 `task_zCyQJjDT8ljn3DrbMWBrYqCxQyjnDMvN` 并进入轮询；最终结果见 `live-smoke-result.json` 和 [本次验收记录](docs/model-parameters-validation.md)。此前九种上传鉴权失败的历史证据仍保留在 `upload-auth-diagnostics.json` 和 `docs/upload-auth-investigation.md`，本次未修改上传实现。

使用本地配置运行：

```bat
python -X utf8 scripts\run_live_smoke.py
python -X utf8 scripts\run_live_smoke.py --submit
```

第一条仅检查配置；第二条最多处理首个提示词并可能产生平台费用，成功后保存 `live-smoke-result.json` 和历史记录。已有相同远端任务会被保留，不重复创建；没有素材或鉴权失败时不能视为完成真实平台验收。

可单独运行 `python -X utf8 scripts\check_connections.py` 查看视频/上传两边的连接结果；此命令只上传测试图，不创建视频任务。
