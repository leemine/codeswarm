# CodeSwarm 工程规约

修改代码前读取相关模块已有 AGENTS.md，优先核对源码和附近测试；不因新增规约开展全仓整改。

## core 依赖迭代：两条规则

1. core 改动先合入远端，再升级 swarm 依赖。固定已合入的完整提交 SHA，同时更新 pyproject.toml 中所有 openjiuwen 引用及 uv.lock；使用 uv lock 生成锁文件，不只替换锁文件字符串。
2. swarm 合入前，CI 在干净环境执行 uv sync --locked --group test --python 3.13，确认实际安装的 openjiuwen 来自锁定 Git 提交，再运行相关测试。不得用本地 core 路径、editable override 或 PYTHONPATH 替代锁定依赖验收。

本地联合开发可以引用 core 源码，但须区分本地源码测试与锁定依赖验证。core 无相关变更时无需追随每个新提交。CI 未通过或必需验证未执行时，不宣称升级配对验证成功。

## 统一执行边界

- 普通 Single/Team 使用 core/harness；Projects 只提供配置候选与授权上下文，不拥有 Provider 状态机。
- 保留原 Runtime generation、控制投递、生产者生命周期和 Team 调度。新增绑定层不复制状态机，不拥有原会话持久化。
- 执行 Provider 与模型服务商分开；主体/会话/Workspace/配置必须隔离。已绑定执行不受新默认配置影响。
- 事件单路投影到原历史/UI，审批通过原交互通道；流结束不等于执行结束。前端遵守现有模块规约。

## Python CI 执行策略（2026-09-29）

- 每次提交/PR 和合入后的自动检查使用 `pr-stable`，本地按影响面运行确定性测试及所需真实验收；不逐提交、逐候选或逐合入 SHA 触发全量 Python CI。
- `full-python` 只由每日 `schedule` 在默认分支执行。`workflow_dispatch` 只跑 stable；不要为每个提交额外手动补跑全量。
- 全量是每日回归巡检，不是每个 PR 的必经门禁。保留完整归档、准确 SHA、失败/跳过/未运行与分片闭合审计；已知相关失败须调查修复，不能因降低频率忽略失败。
- 合入后核验 stable、依赖来源和受影响范围；不得把旧 SHA 的每日全量结果记作新 SHA 已通过全量。
