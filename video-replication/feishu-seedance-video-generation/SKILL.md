---
name: feishu-seedance-video-generation
description: "读取飞书多维表格（Base）中的分镜脚本、时长和参考截图，调用火山方舟 Doubao Seedance 2.5 异步生成逐镜头视频，下载 MP4，并把任务状态、错误、临时 URL 和视频附件回写原记录。用户提到从飞书 Base/分镜表批量生成视频、Seedance 2.5 视频生成、Base 到方舟视频工作流时使用。"
---

# 飞书分镜生成 Seedance 2.5 视频

读取 Base 的分镜记录，逐行生成视频并把结果持久化回原记录。使用 `scripts/generate_from_base.py` 执行确定性流程，不手工拼接 API 请求或附件 token。

## 依赖

- 先读取本机 `lark-shared` 和 `lark-base` Skill，沿用它们的身份、权限、NDJSON 和附件规则。
- 要求 `lark-cli` 已登录并可读取目标 Base。
- 从环境变量 `ARK_API_KEY` 读取方舟密钥；不得在命令、日志、Base 或最终回复中输出密钥。
- 修改模型参数或媒体输入前读取 `references/seedance-2.5-api.md`。
- 适配非默认字段时读取 `references/base-contract.md`。

## 执行工作流

1. 解析用户给出的 Base URL，优先使用 `--as user`；只有用户明确要求应用身份时才用 `--as bot`。
2. 先运行只读预检：

   ```bash
   python3 scripts/generate_from_base.py \
     --base-url '<base-url>' \
     --dry-run
   ```

3. 检查计划中的记录数、提示词、参考图数量和标准化时长。Seedance 2.5 输出时长为 4-30 秒；默认 `clamp` 会把分镜中的更短时长提升至 4 秒并把非整数秒向上取整。脚本会同步补充有效提示词：短时间轴完成后保持结尾停帧，超长时间轴等比压缩。用户要求严格保持时长时使用 `--duration-policy reject` 并报告不兼容记录。
4. 明确告诉用户生成会产生方舟费用、会在 Base 新建 5 个输出字段（若缺失）并回写记录。获得明确同意后才执行付费生成：

   ```bash
   python3 scripts/generate_from_base.py \
     --base-url '<base-url>' \
     --ensure-output-fields \
     --yes
   ```

   执行前让调用环境通过密钥管理或已有会话预先设置 `ARK_API_KEY`，不要把真实值放进聊天、命令行或 shell 历史。
5. 小批量验证时加 `--max-records 1`；指定记录时重复使用 `--record-id rec_xxx`。确认首条生成质量后再处理全部记录。
6. 汇报读取数、提交数、成功数、失败数、输出目录和 Base 链接。失败时保留任务 ID 和错误，不因单行失败中断其他分镜。

## 默认行为

- 模型：`doubao-seedance-2-5-260628`
- 比例：`adaptive`
- 分辨率：`720p`
- 音频：关闭；用户明确需要声音时加 `--generate-audio`
- 水印：关闭；用户明确要求水印时加 `--watermark`
- 轮询：每 10 秒，最长 30 分钟
- 输出：`seedance_output/` 下的 Base NDJSON、输入截图和 MP4
- 参考图：使用同一记录 `镜头截图` 中的全部附件

## 安全与恢复

- 未带 `--yes` 时脚本拒绝调用付费 API 和写 Base；预检只能使用 `--dry-run`。
- 不自动切换飞书身份。权限失败时按 `lark-shared` 以原身份恢复授权。
- 方舟返回 `429` 或 `5xx` 时指数退避重试；单个记录最终失败时回写 `Seedance状态=failed` 和错误摘要。
- Base 中已有 `queued` / `running` 状态和任务 ID 时续接轮询，不重复创建任务或重复计费。
- 批量任务可先用 `--submit-only` 提交并保存任务 ID，再续接完成；已有同名输出附件时不重复上传。
- 成功后立即下载视频并上传到 `Seedance生成视频`，因为方舟 `content.video_url` 仅保留 24 小时。
- Base 附件只用 `+record-download-attachment` / `+record-upload-attachment`，不得直接写附件 CellValue。
