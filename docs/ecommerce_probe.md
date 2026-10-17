# 电商图功能 · kkone 中转站真实报文契约（POC 实测）

> 实测时间：2026-10-17。网关：`https://api.kkone.vip/v1`（New API / One-API 系，响应头 `X-New-Api-Version`）。
> 本文使用一把“测试密钥”实测；**任何密钥不得写入代码或提交**，运行时一律从 `config.json` 读取。
> 所有结论均为真实请求验证（非推断），已通过返回码、`usage.input_tokens_details.image_tokens` 与输出像素三重确认。

## 1. 模型清单（测试密钥可见 68 个，分三类）

- 文本/对话（约 40 个，可用于 AI 分析）：
  - Gemini：`gemini-3.8-flash`、`gemini-3.6-flash-high`、`gemini-3.7-flash-high`、`gemini-3.1-pro-preview`、`gemini-3.1-pro-high`、`gemini-4-argon`（多数带 `-pack` 计费变体）
  - Claude：`claude-haiku-4-5`、`claude-sonnet-4-5/4-6/5/5.5`、`claude-sonnet-4-6-thinking`、`claude-opus-4.5~5.5`、`claude-fable-5`
  - GPT：`gpt-5.6-sol`、`gpt-6-astra/sol/6.1-sol`；Grok：`grok-4.5/4.6/4.7`
- 图片（12 个）：`gpt-image-2`、`gpt-image-2-cf`、`gpt-image-2.5-flare`、`gpt-image-2.5-flare-cf`、`gpt-image-2.5-sunburst`、`gpt-image-2.5-sunburst-cf`、`grok-imagine-image`、`grok-imagine-image-edit`、`Nano Banana 2`、`Nano Banana 2.1`、`Nano Banana Pro`、`wan-3.0`
- 视频（12 个）：`MiniMax-H3`、`video-v2 / v2-fast / v2-特价版 / v2-480P / v2-720P`、`video-v3` 等

> 重要：旧（软件当前配置）那把密钥 `/models` 只返回 24 个，**全是视频/图片，没有任何文本模型**；本测试密钥是 68 个的超集（视频+图片+文本都有）。
> 电商图功能要可用，**软件里配置的 API Key 必须是含文本模型的这一把（或等效含 chat + image 的分组）**。

## 2. DeepSeek 官方/中转文本模型：当前不可用

- 实测 `POST /v1/chat/completions`，模型 `deepseek-chat`、`deepseek-reasoner` 均返回：
  `HTTP 503 No available channel for model deepseek-... under group CCMax稳定满血,…,Claude官转（仅支持sonnet、haiku）,…,Gemini Pro-官Key,…,Grok,图片模型,视频模型`
- 即**当前令牌所在分组没有任何 DeepSeek 渠道**（`/models` 里也无 deepseek 条目）。
- 若要用 DeepSeek：需让 kkone 后台在该令牌分组里开通 DeepSeek 渠道；开通前用 Gemini/Claude/GPT 替代。

## 3. AI 分析（文本/JSON）——已验证

- 接口：`POST {base_url}/chat/completions`，OpenAI 兼容。
- 流式：`stream=true`，`Accept: text/event-stream`，逐行 `data: {...}`，结束 `data: [DONE]`。
- 增量：正文 `choices[i].delta.content`；思考链（开启思考类模型时）`choices[i].delta.reasoning_content`。
- 注意：流末尾会出现 `choices=[]` 的 usage 事件，解析时需先判空。
- 推荐默认分析模型：`gemini-3.8-flash`（快、便宜、多模态、严格 JSON 实测通过）。
  - 实测：要求“恰好 2 条（轮播 1、详情 1）”，流式返回纯 JSON 数组，可直接解析；flash 无 `reasoning_content`。
- 需要“深度思考”时，前端让用户选思考类模型（如 `gemini-3.1-pro-preview` / `gemini-3.6-flash-high` / `claude-sonnet-4-6-thinking`），适配层读取 `delta.reasoning_content`（有则展示，无则留空）。
- 非流式兜底（`stream=false`）同样可用，`gpt-5.6-sol`、`gemini-3.8-flash` 实测返回 `choices[0].message.content`。
- `claude-haiku-4-5` 偶发首包超时（>30s），设置读取超时放宽到 60s+。

## 4. 图片生成（以 gpt-image 系列为默认，全部同步）

默认图片模型：`gpt-image-2.5-sunburst`（图生图/参考图已完整验证）；`gpt-image-2` 同构。

### 4.1 文生图（无参考图）

- 接口：`POST {base_url}/images/generations`，`Content-Type: application/json`。
- 请求体：`{"model","prompt","n":1,"size":"WxH","quality":"auto|high"}`
- 响应：`200` **同步**，`data[0].b64_json`（PNG，无 task_id、无需轮询），另有 `revised_prompt`、`output_format:"png"`、`usage`。

### 4.2 图生图（带产品图/风格参考图）——核心

- 接口：`POST {base_url}/images/edits`，**`multipart/form-data`**（不是 JSON）。
- 表单字段：
  - 文件：`image=@文件`（一个或多个，按顺序重复；产品图在前、风格图最后，顺序即参考顺序），字段名就叫 `image`；
  - 普通字段：`model`、`prompt`、`n`、`size`（可再带 `quality`）。
- 响应：`200` **同步**，`data[0].b64_json`（PNG）。
- 已三重确认参考图被使用：
  - 返回 `usage.input_tokens_details.image_tokens = 256`（文生图时为 0）；
  - 让其“原样复刻纯绿参考图”，输出多点像素 RGB≈(66,198,117)，与输入纯绿 (70,200,120) 一致。
- 结论：**gpt-image 图生图直接走本地文件 multipart 即可，不需要先上传到图床**。
- JSON 传 data URI/URL（`image` / `images` / `image_urls` / `input_reference`）在 gpt-image 上均被忽略（`image_tokens=0`），不要用。

### 4.3 尺寸 / 比例 / 分辨率（尺寸精确，比例严格）

- `size` 直接决定输出像素，实测完全精确：
  - `1024x1024` → 1:1；`1024x1536` → 9:16 实际 1024×1536；`1536x1024` → 16:9 实际 1536×1024；
  - `2048x2048` + `quality:"high"` → 实际 2048×2048（2K）。
- 分辨率换算规则（长边基准）：1K→1024、2K→2048、4K→4096；按所选比例算出宽高：
  - 1:1 (1024²)；9:16 (1024×1536)；16:9 (1536×1024)；3:4 (1024×1365)；4:3 (1365×1024)；
    4:5 (1024×1280)；5:4 (1280×1024)；2:3 (1024×1536)；3:2 (1536×1024)。
  - 3:4 与 2:3 同为 1024×1536（按 2:3 即可），4:5 与 5:4 如上。
  - 4K 若模型拒绝（尺寸上限）则回退到 2K 并在 UI 提示。
- 比例集合：`1:1,5:4,9:16,16:9,3:2,4:3,4:5,3:4,2:3`。

### 4.4 其他图片模型（次要，先不做或作为可选）

- `grok-imagine-image-edit`：图生图要求 `POST /v1/images/edits` 且 **`Content-Type: application/json`**，字段 `image`/`images` 为 **1–8 个 HTTPS 图片 URL**（拒绝 data URI/裸 base64：`URL 必须是无用户信息的 HTTPS 地址`）；需先上传图床拿公网 URL。
- `Nano Banana Pro`：在 `/images/generations` JSON 与 `/images/edits` JSON 下均未成功（`no images generated` / `unsupported MIME`），其参考图格式待后续单独验证，本期不作为默认。
- gpt-image 系列无异步任务，`GET /v1/images/generations/{id}` 实测 404（不要写轮询逻辑给 gpt-image）。

## 5. 图床（仅供 grok 等需要公网 URL 的模型）

- `https://video.kkone.vip/api/uploads`：multipart 字段 `files`，响应 `data.files[0].url`。
- 测试密钥上传返回 `401 upload token required`（它是网关 key，不是图床 token）；软件现有的 `api.upload_api_key`（X-Upload-Token）才是图床凭据。
- gpt-image 走 multipart 图生图，**不依赖图床**；只有未来接入 grok-edit 才需要图床。

## 6. 对适配层的硬要求（据此写段 2/3）

1. 分析模型走 `/chat/completions` SSE；正文拼 `content`，思考拼 `reasoning_content`，跳过 `choices=[]`；解析严格 JSON 数组（去围栏、首尾 `[ ]`、校验数量/类型/序号、carousel 与 detail 各自从 1 编号）。
2. 图片模型 gpt-image：
   - 无参考图 → `/images/generations` JSON；
   - 有参考图 → `/images/edits` multipart（`image` 多文件按顺序）；
   - 一律**同步**取 `data[0].b64_json`，解码为 PNG 落盘；`usage.image_tokens=0` 且本应带参考图时记为异常告警。
3. 尺寸按 4.3 精确计算；`quality` 默认 `auto`，2K/4K 用 `high`；4K 失败回退 2K。
4. 网络健壮性：大体积 b64 响应（1.5MB+）在 urllib 下出现过 `IncompleteRead`，实现侧需使用与现有 `http_client` 一致的、带重试的读取（或在读取层对 IncompleteRead 做有限重试）。
5. 所有测试用本地 HTTP fixture 回放，禁止在单测里真实联网/扣费。
