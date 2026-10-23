# AI PM Studio · 最小验证

输入一个个人产品想法，让批判分析与方案草拟两个子代理独立并行工作，
再由校验子代理检查两份结果与原始证据，输出一个决策报告。
另外提供单 Agent 对照模式。没有可视化界面，不改 Hermes 内核。

**当前状态：执行链路经过本地模拟模型接口测试；真实模型效果尚未验证。**
本示例针对分叉 `dd0923bb89ed2dd56f82cb63656a1323f6f42e6f` 开发。
上游 2026-10-23 的委派接口已有变化，尚未做上游运行兼容性测试。

## 快速运行

从本仓库根目录运行，Python 3.11+。下列安装命令沿用本分叉的依赖锁定；
若已有 Hermes 虚拟环境，复用该环境即可。

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'

# 只接受尚不存在的目录，不覆盖现有 Hermes 配置。
.venv/bin/python -m examples.ai_pm_studio init --home /tmp/ai-pm-studio

# 在终端安全读取你自己的兼容接口 API Key；不要把 Key 发到 PR 或提交到仓库。
read -r -s -p 'API key: ' AI_PM_STUDIO_API_KEY
export AI_PM_STUDIO_API_KEY

# 将两个占位符替换为你的模型 ID 和 OpenAI Chat Completions 兼容接口地址。
.venv/bin/python -m examples.ai_pm_studio run \
  --home /tmp/ai-pm-studio \
  --brief examples/ai_pm_studio/brief.example.json \
  --model YOUR_MODEL_ID \
  --base-url https://YOUR_ENDPOINT/v1
```

终端输出本次 `report.md` 的路径。正式自用时，将 `/tmp/ai-pm-studio`
换成自己选择的持久目录。Windows 使用 `.venv\Scripts\python.exe`，
通过 PowerShell 设置同名环境变量；本次只在 Linux/Python 3.12 上测试。

对同一输入跑单 Agent 基线：在上述 `run` 命令后加 `--mode single`。
两种模式默认使用同一个模型、同样的输入、无外部工具，分别产生三次和一次
角色执行；Hermes 内部重试可能增加 API 请求数，因此不能仅按角色数估算成本。
`max_tokens=2500` 是每次请求的输出上限，两种模式的总预算不相等。

## 输入和输出

输入格式见 [brief.example.json](brief.example.json)：

- `idea`：要验证的想法。
- `constraints`：真实限制，不限于开发条件。
- `evidence`：带唯一 ID 的已知材料，允许空数组。它们是用户提供的材料，
  不是框架认证的事实。示例内容明确为演示假设。

每次输出位于 `<home>/studio/runs/<unique-id>/`：

| 文件 | 用途 |
|---|---|
| `brief.json` | 原始输入 |
| `batch-*-input.json` | 实际角色提示词、证据和交接内容 |
| `batch-*-result.json` | Hermes 的原始状态、摘要、调用与 token 统计 |
| `critic/designer/verifier.json` 或 `baseline.json` | 经过结构校验的角色结果 |
| `run.json` | 流程状态、模型、代码版本、每阶段 token/耗时 |
| `report.md` | 完成后生成的可读报告，包含主张的证据标记与逐条校验 |

失败时返回非零退出码，保留已有材料，不生成最终报告。
不自动修复格式、不自动重跑整批，防止隐藏失败和追加不可见成本。
新的一次运行使用新目录；**尚不支持断点续跑**。

## 复用与边界

- `HermesBackend` 调用原生 `delegate_task(tasks=..., parent_agent=...)`，
  Hermes 创建真实的独立子代理、运行线程、处理模型协议和保存会话。
- 原生 `skill_view` 从隔离 home 加载四个角色 Skill；Python 负责固定的阶段顺序、
  交接校验和报告落盘。这里不是由提示词决定是否执行下一个角色。
- 父代理仅充当运行上下文，不额外调用模型；所有子代理都没有外部工具。
  因此校验仅针对输入材料和内部一致性，**不等于联网查证**。
- 禁用永久记忆写入及个人记忆加载。阶段产物属于项目记录，不能自动当作
  长期事实；后续只应把人工确认的稳定偏好或决策写入 Hermes 记忆。
- 使用独立 `HERMES_HOME`、SQLite 会话库、空插件允许列表；不启动 Gateway、
  Cron 或 Kanban。独立上下文不是操作系统沙箱。
- 配置限制并发 2、深度 1、每子代理最多 3 次迭代、120 秒超时。
  旧 Hermes 以线程中断处理超时，不能强杀阻塞线程；它不是整个进程的硬时限，
  也不是费用上限。需要后台可靠运行时再接 Kanban/Cron。
- 程序校验 JSON、证据 ID、角色状态和主张覆盖；引用真实证据 ID 仍可能
  曲解证据，最终判断需人工复核。

## 测试

```bash
scripts/run_tests.sh tests/test_ai_pm_studio.py -q --tb=short
```

本地 HTTP 测试使用真实 `AIAgent`、`delegate_task`、`skill_view`、`SessionDB`，
只替换模型服务返回内容，覆盖并发实际发生、顺序交接、空工具列表、单 Agent
基线、无效 JSON 拦截及文件输出。其他测试覆盖超时/中断/迭代耗尽状态、
伪造证据 ID、遗漏主张、重复结果、输入错误和目录保护。

完整评估、版本差异和建议见 [ASSESSMENT.md](ASSESSMENT.md)。
