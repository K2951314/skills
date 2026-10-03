---
name: zk-ai-gateway
description: 调用本机 ZK-AI 网关上的模型（OpenAI 兼容接口 /v1/chat/completions）。当用户说「调 ZK-AI」「用网关跑一下」「zk-auto 帮我做」「问一下本地大模型」「用 K3 / kimi-k3 / step-5-preview 处理」「走 ZK-AI 这个别名的模型」「用网关的能力链做长上下文 / 看图」时使用。网关在本机是 127.0.0.1:8317，在服务器（ubuntu@120.53.28.29）上是 127.0.0.1:8318 并有公网入口 https://120.53.28.29/zkai/v1（OpenAI 兼容）。默认一律用 zk-auto（网关会按请求内容自动选模型、选 Key、自动重试与故障转移），只在明确要旗舰质量 / 长上下文批量 / 看图时显式指定别名。不用于直接调用云端 API、不用于修改网关配置与别名、不用于在 skill 内部做多模型流水线编排。
agent_created: true
---

# ZK-AI 网关调用

网关是把「选哪个模型、用哪把 Key、失败后切换」全包掉的那一层。本技能只做一件事：**把请求正确发到网关，并把结果里"实际走了哪条链"讲清楚。**

## 第零步：确认用哪个入口（本机 or 服务器）

`$base` 的取值按下面顺序决定，**不要在技能里写死任何一个地址或端口**：

```powershell
$base  = if ($env:ZKAI_BASE_URL) { $env:ZKAI_BASE_URL } else { "http://127.0.0.1:8317" }
```

| 场景 | `ZKAI_BASE_URL` | 说明 |
|---|---|---|
| 本机 Windows | `http://127.0.0.1:8317` | 默认。`.env` 在本机，密钥也读本机环境变量 |
| 服务器本机（SSH 进去） | `http://127.0.0.1:8318` | 服务器网关只听回环 |
| 任何地方（走公网） | `https://120.53.28.29/zkai` | 经 Caddy 反代；**必须带 Bearer**，且只放行 `/v1/*` 与 `/health` |

- 环境变量 `ZKAI_BASE_URL` 已设置就以它为准；否则退回本机 `127.0.0.1:8317`。
- 公网入口的 token 与服务器 `/etc/zkai.env` 的 `ZKAI_API_TOKEN` 同一份值。
- 服务器上 `sq` / `tandian` 等应用走的是**回环** `127.0.0.1:8318`，不经过 Caddy，不消耗公网带宽。
- 从外网调时注意上传带宽（服务器实测约 150 KB/s），但请求体通常只有几 KB，可忽略；
  真正的瓶颈是模型生成时间，`-TimeoutSec 420` 别省。

## 第一步：判定要不要显式选别名

| 用户意图 | model 值 |
|---|---|
| 默认、大多数情况 | `zk-auto` |
| 明确要旗舰质量，不怕慢 | `zk-k3` |
| 长上下文 / 批量 / 读多写少 | `zk-long` |
| 消息里带图片，或要 OCR | `zk-vision` |

拿不准就 `zk-auto`。网关的 agent-auto 会按请求**形状**自动改写：带图→`zk-vision`，长上下文或工具轮→`zk-long`，其余按能力分在链内排序。

**不要自己再套一层"先判断任务类型再选模型"。** 网关 `agent_auto` + capability 打分已经做了按形状分类，skill 层再加一次 LLM 分类只会多烧一次调用、又把按内容分派绕回去。

### 关于第一步表格里的名字

上面四个别名是《网关当前配置里有、且很可能继续有》的稳定入口；但**别名会随配置增减**。表格里某个名字在第三步查不到时，一律退回 `zk-auto`。同理，用户在请求里直接点了某模型/别名的名字（例如触发了本技能的那几个说法），也先回第三步核对：存在才用，不存在就用 `zk-auto` 顶替并说明。

## 第二步：确认网关可用

```powershell
$r = Invoke-RestMethod "$base/health"
$r.status          # healthy / degraded / starting
```

连不上（`Connection refused`）说明网关没启动：先启动本机 ZK-AI 网关，把本端口交给正常启动流程，再回来继续。不要反复重试。

>`degraded` = 所有 provider 都不可用，此时发请求大概率失败，先告诉用户。

## 第三步：取实时别名清单（抗漂移）

```powershell
$aliases = (Invoke-RestMethod "$base/v1/models").data.id
```

- 只在 `$aliases` 里挑 model 值；挑不到就退回 `zk-auto`，**别硬拿一个不存在的名字去请求**。
- 这个网关的别名和底层模型会随配置变化。**不要把任何模型 id 或别名写进结论或文档** —— 一律以本接口返回为准。SKILL.md 里不写死模型名的根本原因就在这里：一旦别名下线，写死的名字会 404，而这份文档不会自动变。

## 第四步：发请求

```powershell
$token = $env:ZKAI_API_TOKEN
$body = @{
  model      = "zk-auto"
  messages   = @(@{role="user"; content="你的问题"})
  max_tokens = 1024
} | ConvertTo-Json -Depth 5

$resp = Invoke-WebRequest "$base/v1/chat/completions" -Method POST `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
  -UseBasicParsing `
  -TimeoutSec 420
```

要点：
- token 从 `$env:ZKAI_API_TOKEN` 读；为空时提示用户设置该环境变量，不要在本技能或仓库里放任何密钥。
- 入口变化只改 `$env:ZKAI_BASE_URL` 或第零步的兜底默认值，第二、三、四步都已统一读 `$base`。
- 非流式即可。流式加 `stream = $true`，返回 `text/event-stream`，以 `data: [DONE]` 结束。
- 消息里要带图片时改用 `zk-vision`，图片走 `content` 数组的 `image_url` 部分。
- **客户端超时必须大于网关自己的预算。** 网关单次上游超时各供应商不同（60–300 秒范围），而整个请求还有一个墙钟预算（默认 300 秒，`ZKAI_MAX_REQUEST_SECONDS` 控制）：多次失败积累到 300 秒才由网关返回 504。所以客户端至少给 `-TimeoutSec 420`，否则一次还在正常跑的慢请求会被客户端先拨断，而网关侧会留下一条永远 `pending` 的请求记录。PowerShell 的 `Invoke-WebRequest` 默认只给 100 秒，必须显式覆盖。

## 第五步：解读结果，别盲目重发

成功时响应体和响应头都带路由信息：

```text
x-zkai-model     : 实际用的模型
x-zkai-alias     : 请求的别名（可能已被改写）
x-zkai-provider  : 走的供应商
x-zkai-attempt   : 第几次尝试才成功（>1 说明前面挂过）
x-zkai-fallback  : true = 发生过跨模型兜底
```

`$json.zk_ai.resolved_model` / `zk_ai.fallback_used` 是同样信息的 body 版，`zk_ai.routing_reason` 会给出完整排序理由。

报告给用户时写清"实际走了哪条链"，照实引用响应头/`zk_ai` 里的值，例如"zk-auto → <实际模型>（<实际供应商> 渠道，第 N 次尝试，<有无兜底>）"。不要在这里编示例模型名——它等于又写死了一次。

失败时按顺序做：

1. `401` → token 为空或不匹配，检查 `$env:ZKAI_API_TOKEN`。
2. `404` → model 名字写错或已下线。错误体里会带一个相近可用名列表（前缀匹配 + 模糊匹配），先照它改；改不了就回第三步重取 `$aliases`。
3. `429` + `Retry-After` → 限流，等 header 里给的秒数再试，别立刻重发（会把冷却拖长）。
4. `5xx` / 超时 → 网关已在同一请求内沿候选链重试过，**不要立刻重发同一个请求**。先看 `x-zkai-request-id`，再查网关管理端的请求详情定位是哪一环失败的。
5. 客户端自己超时拦截（看不到任何 HTTP 状态）不等于网关失败。网关侧可能还在跑，
   记录停留在 `pending`。这时把 `-TimeoutSec` 提上去，不要当成失败重新发一遍。
6. 网关未启动时不会给你 HTTP 错误，而是连接被拒绝。看到连接级错误先启动网关，
   再回来继续，不要反复重试。

## 边界

- 不直连裸模型名（别名清单以外的那批）。实测同一个"带 5 个工具 + json_mode"的请求：走默认别名得到链上全部候选并按能力分排序；改点名单个模型后候选数骤降，别名的权重地板、硬门槛、跨模型兜底全部失效，该模型挂了就直接报错。
- 不在本技能内做多模型分工编排。要分步调就另建别的技能，网关只管单次调用的选路。
- 不新增、不修改网关侧的模型与别名。那是 `config/models.yaml` 和 ZK-AI 控制台的事。
- 严格输出长的内容时注意 `max_tokens`，网关默认 1024；返回被截断表现为 `finish_reason = "length"`。