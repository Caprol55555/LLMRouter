# LLMRouter Control Center 最佳实践开发计划

> 状态：规划完成，尚未开始实现  
> 基线分支：`codex/session-aware-routing`  
> 规划基线提交：`c3ecfb8bd8bd2a11b5267de9c7d8e2a50244a415`  
> 更新日期：2026-08-20

## 1. 背景与目标

当前生产版 LLMRouter 已具备：

- OpenAI-compatible `/v1/*` API；
- `glm`、`deepseek`、`qwen` 候选模型族；
- DeepSeek 裁判模型；
- `auto`、`auto:once`、`auto:N` 会话路由；
- TTL、有界 LRU、single-flight、后端错误失效；
- 入站 Bearer 鉴权、递归保护、隐私日志；
- 轻量 Docker 生产镜像和 GHCR CI。

当前调参依赖手工修改 YAML、重启容器和查看文本日志。随着候选模型、裁判提示词、重判策略和 9router Combo 持续变化，这种方式缺少：

- 准确区分外层用户请求、内部裁判请求和最终后端请求的统计；
- 配置草稿、验证、版本、发布、审计和回滚；
- 新旧配置的可重复对比测试；
- 模型选择比例、缓存命中率、fallback 和延迟的可视化；
- 并发发布与服务运行之间的安全边界。

本计划建设一个轻量的 **LLMRouter Control Center**，用于管理和观察生产智能路由。它不是通用聊天网站，也不是训练平台。

## 2. 成功标准

完成后，管理员应能：

1. 通过 SSH 隧道打开 `/dashboard`；
2. 准确看到外层请求、裁判调用、缓存命中、模型选择、fallback、错误和延迟；
3. 创建配置草稿并执行结构、语义、连通性和递归保护检查；
4. 对草稿运行临时路由测试，默认不保存测试文本；
5. 原子发布配置，无需重启容器；
6. 发布失败自动保留旧运行配置；
7. 一键回滚到历史版本；
8. 查看配置和管理操作审计记录；
9. 在配置切换时正确清理或迁移会话路由缓存；
10. 保持现有 OpenAI API、流式输出、tool calls 和低资源占用兼容。

## 3. 明确不做

首个正式版本不包含：

- ComfyUI、Gradio、Torch、Transformers、训练或 Benchmark；
- 公网管理域名；
- 在网页中查看、录入或导出 API Key；
- 保存普通生产请求的提示词、消息正文或工具输出；
- 直接挂载 Docker Socket，或从容器内重启 Docker；
- 修改 9router 配置、Provider、Combo 成员或 API Key；
- 多租户、组织权限、SSO；
- 将 SQLite 替换为 MySQL/PostgreSQL；
- 自动用未经审批的配置覆盖生产配置。

## 4. 架构决策

### 4.1 部署形态

Control Center 与现有 FastAPI 服务共用代码库和生产容器：

```text
127.0.0.1:8000
├─ /v1/*                模型调用接口，沿用入站 API Key
├─ /health              基础健康检查
├─ /dashboard           静态管理前端
└─ /admin/api/*         管理 API，使用独立管理员会话
```

管理入口仍只映射到宿主机回环地址，通过 SSH 隧道访问，不增加公网反向代理。即使同一 Docker 私网内的服务能访问端口，管理 API 也必须独立鉴权。

### 4.2 前端

- React + TypeScript + Vite；
- 前端在 GitHub Actions / Docker builder 阶段编译为静态文件；
- 生产镜像不包含 Node.js 运行时；
- 首版不引入重型组件库，优先使用少量、可审计依赖；
- 图表库必须支持按需引入，避免大体积 bundle；
- 页面必须支持窄屏，但不以移动端复杂编辑为首要目标。

### 4.3 数据存储

使用独立 SQLite：

```text
/data/control-center.db
```

生产挂载目录必须可写，应用根文件系统继续只读。数据库启用 WAL、busy timeout、外键和版本化迁移。

建议表：

- `schema_migrations`：数据库版本；
- `routing_events`：有限保留期的结构化路由事件；
- `routing_aggregates_hourly`：小时聚合；
- `config_versions`：不可变配置快照；
- `config_drafts`：未发布草稿；
- `config_activations`：发布与回滚历史；
- `admin_audit_log`：管理操作审计；
- `admin_sessions`：仅保存管理员会话摘要或哈希。

不得存储解析后的真实密钥。配置快照只能保存环境变量引用，例如 `${NINE_ROUTER_INTERNAL_API_KEY}`。

### 4.4 指标写入

推理主链路不得同步依赖指标落库成功：

- 通过有界内存队列提交结构化事件；
- 单写入 worker 批量落 SQLite；
- 队列满时丢弃指标而不是阻塞或弄失败用户请求；
- 记录 `telemetry_dropped_events`；
- 服务优雅停止时尝试限时 flush；
- 指标组件失败只影响可观测性，不影响 `/v1/*`。

每个外层请求生成不含敏感信息的 `request_id`，内部记录：

- 流量类别：`production`、`admin_test`、`deployment_smoke`；
- 请求模型：显式模型、`auto`、`auto:once`、`auto:N`；
- 是否调用裁判；
- 裁判成功、超时、解析失败、越界或默认回退；
- 缓存命中或重判原因；
- 选择的模型族；
- 最终状态、fallback、首字节与总延迟；
- token usage（仅在上游可靠返回时）；
- 配置版本 ID。

不得记录：提示词、消息正文、tool 参数、Authorization、API Key、Cookie、完整会话 ID。会话关联最多保留不可逆哈希的短前缀，且不作为长期用户标识。

### 4.5 管理鉴权

- 新增独立环境变量 `LLMROUTER_ADMIN_TOKEN`；
- 登录时使用常量时间比较；
- 浏览器成功登录后使用随机、短期、可撤销的 HttpOnly 会话 Cookie；
- Cookie 使用 `SameSite=Strict`；SSH 隧道下为 HTTP localhost，不能强制依赖 `Secure`；
- 所有写 API 同时验证 Origin 和 CSRF Token；
- 登录端点限速并统一失败提示；
- `/admin/api/*` 默认拒绝，不能复用普通 `/v1` Key；
- 不启用宽泛 CORS；
- 设置 CSP、`X-Content-Type-Options`、`Referrer-Policy`、frame 限制和 `Cache-Control: no-store`；
- 日志不得打印管理员 Token、Cookie 或请求正文。

### 4.6 配置模型

YAML 继续作为首次启动和灾难恢复基线。数据库没有活动版本时，从 YAML 导入版本 1；存在活动版本后，以数据库的活动快照为准。

配置分为：

- 可在线调整：裁判模型、提示词、允许模型族、默认模型、描述、超时、token 预算、TTL、重判周期和部分会话策略；
- 只读展示或需运维修改：监听地址、端口、密钥引用、数据目录和安全根配置；
- 禁止从网页修改：任何密钥值、Docker 网络、文件路径和宿主机命令。

### 4.7 原子发布

引入不可变 `RuntimeSnapshot` 和受锁保护的运行态引用。每个请求在开始时捕获一个快照，整个请求使用同一版本。

发布顺序：

```text
读取草稿
→ schema 校验
→ 语义与递归校验
→ 构建候选 RuntimeSnapshot
→ 可选连通性检查
→ 原子切换内存引用
→ 使受影响的会话缓存失效
→ 写入活动版本和审计记录
```

在内存切换或数据库提交前失败，不改变生产状态。若数据库活动指针提交失败，必须恢复旧内存快照。进程重启只加载最后成功提交的活动版本。

禁止通过挂载 Docker Socket实现“保存后重启”。

### 4.8 会话缓存策略

配置版本必须参与缓存有效性判断。至少以下变化清空全部路由缓存：

- 候选模型集合；
- 裁判模型、裁判提示词、默认模型；
- 模型描述；
- TTL、重判周期、模态变化策略；
- 递归保护规则。

纯展示字段变化不得清缓存。发布响应应明确返回清除了多少缓存项及原因。

### 4.9 9router 集成边界

- 使用现有独立 9router Key，只读调用 `/v1/models` 和必要的模型探测接口；
- Control Center 不接触 9router Dashboard 密码或 CLI Token；
- 本地禁止选择 `auto`、`lr/*` 以及安全配置中的 forbidden models/prefixes；
- `/v1/models` 无法证明 9router Combo 的内部成员，因此完整 Combo 递归检查仍由 cloud 仓库的部署脚本负责；
- 页面必须明确标识“模型 ID 存在”与“Combo 内部无递归”是两种不同保证。

## 5. 页面与交互范围

### 5.1 Overview

- 最近 1 小时、24 小时、7 天外层请求数；
- 裁判调用数与每外层请求放大系数；
- 缓存命中率；
- 模型选择分布；
- 成功率、fallback、超时和错误分类；
- p50/p95 总延迟和裁判延迟；
- 当前配置版本、发布时间、运行提交 SHA；
- 指标丢弃数和 SQLite 状态。

所有时间显示同时支持浏览器本地时区和 UTC 提示，避免再次混淆服务器时间与数据库 UTC。

### 5.2 Requests

显示不含正文的结构化请求记录：

- 时间、request ID；
- 流量类别；
- 路由策略；
- cache hit / miss；
- 重判原因；
- 裁判结果和最终模型；
- 状态、延迟、token usage；
- 配置版本。

支持按时间、流量类别、模型、状态和配置版本过滤。禁止提供“查看原始消息”功能。

### 5.3 Configuration

- 当前活动版本只读摘要；
- 创建草稿；
- 结构化表单编辑；
- YAML 高级视图仅展示可管理字段；
- diff；
- 校验；
- 发布说明；
- 版本历史与回滚。

### 5.4 Route Lab

- 临时输入任务文本；
- 选择活动版本或草稿；
- 仅裁判模式，默认不调用最终回答模型；
- 显示选择模型、是否使用默认值、耗时和错误类别；
- A/B 比较两个配置版本；
- 默认不保存输入；只有管理员明确选择“保存到测试集”才持久化；
- 明确提示真实裁判会消耗上游额度。

### 5.5 Audit

记录登录成功/失败摘要、草稿创建、校验、发布、回滚、测试集变更和清理操作。审计内容不得包含 Secret 或测试文本正文。

## 6. 分阶段实施

每个阶段必须独立通过开发、审查和决策门，不能把未通过问题带到下一阶段。

### 阶段 0：基线锁定与控制面骨架

目标：建立不会改变现有推理行为的控制面边界。

交付：

- 架构说明和数据库/运行态 ADR；
- `control_center` 配置段，默认关闭；
- 控制面模块目录与依赖边界；
- SQLite migration runner 和空数据库初始化；
- `/admin/api/status` 的关闭态/健康态契约；
- 测试 fixture、临时数据库工具；
- CI 路径覆盖新目录。

验收：

- 默认配置下现有 API 行为和镜像依赖不变；
- 现有测试全部通过；
- migration 可重复执行；
- 只读根文件系统下，仅指定数据目录可写；
- 没有 UI、配置写入或热更新的半成品入口。

### 阶段 1：隐私安全的路由遥测

目标：建立准确、不会影响推理的事件和聚合链路。

交付：

- 结构化路由事件模型和 request correlation；
- 有界异步队列、单 writer、批量 SQLite 写入；
- retention 和小时聚合；
- 外层请求、裁判、缓存、最终后端与错误埋点；
- `production/admin_test/deployment_smoke` 分类；
- 指标丢弃和数据库异常自监控；
- 查询服务，但暂不开放管理 HTTP API。

验收：

- 一次 `auto` 能准确对应一个外层事件、零或一个裁判事件及一个最终结果；
- cache hit 不增加裁判调用数；
- 5 并发及 single-flight 统计准确；
- 数据库不可写、锁冲突、队列满时 `/v1/*` 仍正常；
- 自动化测试证明不存储提示词、Headers、Key、Cookie 或 tool 内容；
- 指标启用前后延迟基准无明显回归。

### 阶段 2：只读管理 API、鉴权与 Dashboard

目标：先提供可信的只读可视化，不开放配置变更。

交付：

- 管理员登录、会话、退出、限速、CSRF 和安全响应头；
- `/admin/api/overview`、`requests`、`health`、`runtime`；
- React/TypeScript Dashboard：Overview、Requests、运行状态；
- UTC/本地时区切换；
- 分页、过滤、空状态、错误状态；
- 多阶段 Docker 构建，生产镜像无 Node runtime；
- 后端、前端和浏览器级 smoke 测试。

验收：

- 未认证访问所有管理数据均被拒绝；
- 普通 LLMRouter 入站 Key 不能登录管理端；
- CSRF、暴力登录、Cookie 和安全头测试通过；
- 页面数据与 SQLite 查询一致；
- Dashboard 不显示提示词和密钥；
- `/v1/*` 回归测试、流式和 tool calls 继续通过。

### 阶段 3：配置草稿、验证与版本库

目标：允许安全编辑，但仍不改变运行配置。

交付：

- YAML 基线导入为不可变配置版本；
- 活动版本、历史版本和草稿数据模型；
- 结构化配置 API；
- 草稿创建、编辑、删除、diff；
- schema、语义、环境变量引用、模型 allowlist 和递归校验；
- Configuration 页面；
- 管理操作审计。

验收：

- 配置版本不可修改，只能由草稿产生新版本；
- 草稿无法写入 Secret 明文或未知字段；
- `auto`、`lr/*` 和 forbidden prefix 无法成为上游候选；
- 非法草稿不能进入待发布状态；
- 编辑草稿绝不改变当前推理行为；
- diff 稳定且不因字段顺序产生噪声。

### 阶段 4：原子发布、热更新、缓存失效与回滚

目标：完成安全的在线配置生命周期。

交付：

- 不可变 RuntimeSnapshot；
- 请求级快照捕获；
- 发布锁和乐观版本检查；
- 原子切换、持久化活动指针；
- 基于变更类型的缓存失效；
- 发布失败恢复；
- 历史版本回滚；
- 发布/回滚 UI 和审计记录。

验收：

- 发布期间的已有请求完整使用旧版本，新请求完整使用新版本；
- 不出现一个请求混用两个配置；
- 两个管理员并发发布时仅一个成功，另一个得到明确冲突；
- 模拟 DB 失败、构建失败和校验失败均保持旧配置；
- 进程重启加载最后成功版本；
- 影响路由语义的变更清缓存，纯展示变更不清；
- 回滚也产生新 activation 记录而不篡改历史。

### 阶段 5：9router 模型发现与 Route Lab

目标：让持续调参可以先验证、再发布。

交付：

- 只读获取 9router `/v1/models`；
- 模型存在性、连通性和本地递归校验；
- Route Lab 临时裁判；
- 活动版本与草稿 A/B；
- 可选测试集及显式保存确认；
- 测试调用标记为 `admin_test`；
- 裁判耗时、结果、默认回退原因展示。

验收：

- 9router 不可用时不影响生产推理，只影响发现/测试并明确提示；
- 临时输入默认不落库、不进入普通请求统计；
- A/B 使用各自完整快照，不污染当前活动版本和会话缓存；
- 模型不存在、响应非 JSON、越界、超时均有可理解结果；
- UI 明确说明 `/v1/models` 不能验证 Combo 内部成员；
- 不新增 9router Dashboard 或 CLI 凭据依赖。

### 阶段 6：生产硬化与发布准备

目标：达到可以灰度部署的质量。

交付：

- 完整后端、前端、迁移、并发和端到端测试；
- 数据保留、聚合和清理任务；
- SQLite 一致性检查与备份文档；
- 管理 API OpenAPI 边界审查；
- 镜像 SBOM/依赖检查（在现有 CI 可承受范围内）；
- 资源和延迟基准；
- 运维、故障恢复、隧道和回滚文档；
- cloud 仓库部署变更清单，但不在本阶段擅自修改或部署另一仓库。

验收：

- 测试覆盖核心状态机和失败注入；
- 无 P0/P1 安全、数据一致性或兼容问题；
- 5 并发和预期生产负载下无 OOM、无持续队列丢弃；
- Control Center 关闭时保持现有轻量运行模式；
- Control Center 开启后的资源增量有实测报告；
- 生产镜像仍不包含训练依赖、Gradio、ComfyUI 或 Node runtime。

### 阶段 7：深圳机灰度与验收（由 Codex 运维，不交给开发/审查模型）

前置：阶段 0–6 全部通过并已形成可追踪提交，用户明确授权部署。

步骤：

1. 更新 cloud 仓库 Compose、数据卷、环境示例、备份和更新脚本；
2. 生成管理员 Token，只写服务器 root 600 文件；
3. 备份现有 LLMRouter 配置和镜像；
4. 固定提交标签和 digest 更新；
5. 建立桌面 SSH 隧道脚本；
6. 只读观察至少一个灰度窗口；
7. 再启用配置写入和 Route Lab；
8. 验证原子发布、回滚、流式、tool calls、并发、内存和 Swap；
9. 更新运维文档并按 cloud 仓库 Git 规则提交。

## 7. 跨阶段质量要求

### 7.1 兼容性

- 不能破坏现有 `/v1/chat/completions`、`/v1/models`、WebSocket、流式 SSE 和 tool calls；
- 不能改变已有 `auto` 策略语义，除非某阶段明确迁移并提供兼容测试；
- 未启用 Control Center 时，不要求创建数据库或管理员 Token；
- 更新脚本和旧 YAML 必须有清晰迁移路径。

### 7.2 测试

每阶段至少包括：

- 单元测试；
- API 集成测试；
- 失败路径；
- 权限和隐私测试；
- 现有回归测试。

涉及 UI 的阶段增加：

- 组件测试；
- 关键流程浏览器 smoke；
- loading、empty、error、unauthorized 状态；
- 键盘可用性与基础无障碍检查。

### 7.3 性能预算

- 指标不可阻塞推理；
- Dashboard 静态资源应压缩并缓存，管理 API 数据 `no-store`；
- 列表必须分页，不允许一次读取全部事件；
- 保留期和聚合必须有上限；
- 不允许在请求热路径扫描 SQLite 历史；
- 生产额外常驻内存目标不超过 100 MiB，最终以实测为准；
- 不能安装 Torch/Transformers 来实现图表或配置管理。

### 7.4 安全与隐私

- 所有 Secret 只来自环境变量或服务器文件；
- 错误响应和日志做敏感信息清洗；
- 管理端不得存在任意文件读写、命令执行或 SSRF；
- 9router Base URL 必须受配置 allowlist/固定内部地址约束；
- Route Lab 的 URL 不能由浏览器任意指定；
- CSV/JSON 导出首版不实现，避免意外泄露；
- 数据库备份视为敏感运维数据。

## 8. Kimi 开发与 GLM 审查协作协议

### 8.1 角色

- **Codex（总控）**：读取仓库、生成每轮 Prompt、检查实际 diff/测试、解释审查报告、决定修复或进入下一阶段，并负责经授权的 Git/部署操作；
- **Claude Code / Kimi（开发）**：只实现当前开发 Prompt 的范围；
- **Claude Code / GLM（审查）**：只读审查当前轮实际 diff 和测试证据，不修改文件；
- **用户**：在三个会话之间传递 Prompt 和报告，批准提交、推送和部署。

### 8.2 顺序

每阶段严格执行：

```text
Codex 检查仓库与上轮状态
→ Codex 输出当前阶段开发 Prompt
→ 用户交给 Kimi
→ Kimi 实现并返回开发报告
→ 用户通知 Codex
→ Codex 检查实际 diff 和测试结果
→ Codex 输出针对实际 diff 的审查 Prompt
→ 用户交给 GLM
→ GLM 只读审查并返回报告
→ 用户把审查报告交给 Codex
→ Codex 独立复核并作出决策
   ├─ 修复：留在本阶段，生成修复 Prompt，之后重新审查
   └─ 通过：结束本阶段，准备下一阶段
```

审查 Prompt 不应与开发 Prompt 同时预生成。它必须包含实施后的真实 base SHA、工作树状态、diff 范围、测试证据和风险点。

### 8.3 开发模型权限

Kimi 默认：

- 可以修改当前仓库文件并运行本地测试；
- 不得提交、推送、合并、rebase、部署或修改服务器；
- 不得修改 cloud 仓库；
- 不得读取、输出或写入真实生产 Key；
- 不得顺手实现下一阶段；
- 不得为了测试降低鉴权、递归保护或隐私约束；
- 遇到范围外阻塞时停止并在报告中说明。

### 8.4 审查模型权限

GLM 默认：

- 只读查看代码、diff、测试、配置和文档；
- 可以运行不会修改仓库的检查；
- 不得编辑、格式化、提交、推送或部署；
- 必须基于实际代码给出文件和行号；
- 不得把风格偏好冒充阻塞问题；
- 必须验证开发报告中的测试声明，而不是照抄报告。

### 8.5 严重级别

- **P0**：密钥泄露、远程接管、数据破坏、鉴权绕过、生产不可用；
- **P1**：核心功能错误、原子性/回滚失效、并发竞态、提示词落库、明显兼容回归；
- **P2**：边界条件错误、可观测性失真、可恢复但影响使用的问题；
- **P3**：非阻塞可维护性、文案、轻微 UX 或优化建议。

决策门：

- 任一确认的 P0/P1：必须留在当前阶段修复并重新审查；
- P2 若影响本阶段验收、数据准确性、安全边界或后续架构：本阶段修复；
- 可延期 P2/P3：记录到计划的 Deferred Items 后可进入下一阶段；
- 审查报告缺少 diff 依据、行号或复现证据：Codex 不直接采信；
- 即使 GLM 报告“通过”，Codex 仍需独立查看 diff 和关键测试。

### 8.6 每轮开发 Prompt 必备内容

Codex 为 Kimi 生成的 Prompt 必须包含：

1. 当前阶段和轮次编号，例如 `P2-DEV-R1`；
2. 仓库绝对路径、分支、base SHA、工作树状态；
3. 要先完整阅读的文件；
4. 本轮唯一目标和明确不做；
5. 文件/模块边界和架构约束；
6. 安全、隐私、兼容和性能约束；
7. 必须新增或更新的测试；
8. 可执行验收命令；
9. 禁止提交、推送和部署；
10. 固定开发报告格式。

开发报告格式：

```text
## 实现摘要
## 修改文件
## 关键设计决定
## 测试命令与结果
## 未验证项
## 已知风险或阻塞
## git status --short
```

### 8.7 每轮审查 Prompt 必备内容

Codex 为 GLM 生成的 Prompt 必须包含：

1. 当前阶段和审查轮次，例如 `P2-REVIEW-R1`；
2. 精确 base SHA 和待审查范围；
3. 阶段验收标准；
4. 开发报告，但要求独立验证；
5. 本阶段风险清单；
6. 只读约束；
7. 必跑或建议检查命令；
8. 固定审查报告格式。

审查报告格式：

```text
## 结论
PASS | PASS_WITH_NOTES | CHANGES_REQUIRED

## Findings
- [P0|P1|P2|P3] 标题
  - 证据：文件:行号
  - 影响：
  - 复现/推理：
  - 建议：

## 验收标准逐项核对
## 实际运行的命令与结果
## 测试缺口
## 范围外观察
```

若没有 Finding，必须明确写“未发现阻塞问题”，不能虚构问题来填充报告。

## 9. Git 与交付策略

- 新会话启动时从当前 `codex/session-aware-routing` 创建专用工作分支，建议 `codex/control-center`；
- Codex 在每阶段开始记录 base SHA；
- Kimi 和 GLM 不执行 Git 写操作；
- 每阶段通过决策门后形成一个边界清晰的提交；
- Codex 提交前检查完整 diff，并向用户给出拟用标题、范围、主线、正文和未验证项；
- 未经用户明确授权不推送、不开 PR、不部署；
- 不把 `.env`、数据库、测试产生物或真实凭据提交到仓库；
- 若工作树出现与当前阶段无关的修改，暂停并由 Codex 厘清归属。

建议阶段提交标题：

```text
P0 feat(control-center): establish isolated control-plane foundations
P1 feat(observability): record privacy-safe routing telemetry
P2 feat(control-center): add authenticated routing dashboard
P3 feat(configuration): add validated routing drafts and version history
P4 feat(configuration): support atomic activation and rollback
P5 feat(route-lab): add upstream discovery and configuration evaluation
P6 chore(control-center): harden production delivery and operations
```

最终标题仍须根据实际完整 diff 重生，不能机械照抄。

## 10. 最终验收清单

- [ ] 普通用户、裁判、最终调用、测试流量的统计口径明确且可验证；
- [ ] 生产请求正文和 Secret 不落库、不进日志；
- [ ] 管理鉴权、CSRF、限速、安全头通过测试；
- [ ] 配置草稿不影响运行配置；
- [ ] 发布原子、并发安全、失败保持旧配置；
- [ ] 回滚不篡改历史；
- [ ] 配置变更正确处理会话缓存；
- [ ] Route Lab 默认不保存输入；
- [ ] 9router 不可用不拖垮生产请求；
- [ ] SSE、WebSocket、tool calls、usage 兼容；
- [ ] 数据迁移、保留、清理、备份、恢复可验证；
- [ ] 生产镜像无 Node runtime、Torch、Gradio、ComfyUI；
- [ ] Control Center 关闭时保持现有轻量模式；
- [ ] 深圳机资源、Swap 和 OOM 验收通过；
- [ ] cloud 部署文档、更新与回滚脚本同步完成。

## 11. Deferred Items

本节由后续 Codex 会话维护。只有经过决策门确认可延期的事项才能加入，并记录来源阶段、严重级别、理由和计划处理阶段。

当前无。
