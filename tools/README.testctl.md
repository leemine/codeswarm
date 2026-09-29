# testctl MVP

`testctl.py` 是仓库内、零第三方运行时依赖的测试编排入口。它不会替代 pytest、Vitest 或 Node test，只负责环境预检、发现、执行、JUnit 归一化和本地归档。

```bash
# 查看依赖安装计划；增加 --execute 才会真正安装
python3 tools/testctl.py bootstrap

# 环境、工具链和 suite 入口预检
python3 tools/testctl.py doctor

# 发现测试并输出 inventory
python3 tools/testctl.py discover --profile mvp --output /tmp/codeswarm-inventory.json

# 查看执行决策
python3 tools/testctl.py plan --profile mvp

# 执行并归档
python3 tools/testctl.py run --profile mvp
TESTCTL_NETWORK_MODE=strict python3 tools/testctl.py run --profile pr-stable
python3 tools/testctl.py run --profile web-diagnostic
python3 tools/testctl.py run --profile tui-diagnostic

# 重新生成人类可读摘要
python3 tools/testctl.py summarize artifacts/test-runs/<run-id>

# 全量 pytest collect 的文件级分片计划
python3 tools/shardplan.py /tmp/pytest-collect.log --target-cases 250 --output /tmp/shards.json

# 完整 Python 诊断回归：全仓收集、文件级分片、逐例超时、闭合核对和失败清单
uv sync --locked --group test --extra desktop --python 3.13
.venv/bin/python tools/full_regression.py --workers 2

# 列出归档、对比基线、预览到期清理
python3 tools/archivectl.py list
python3 tools/archivectl.py compare artifacts/test-runs/<base> artifacts/test-runs/<candidate>
python3 tools/archivectl.py prune

# 外部中断后从已有 JUnit 恢复 NOT_RUN 摘要（默认仅预览）
python3 tools/archivectl.py recover artifacts/test-runs/<interrupted-run>
```

默认归档位于 `artifacts/test-runs/<run-id>/`，包含 manifest 快照、环境指纹、plan、JUnit、日志、`summary.json` 和 `summary.md`。该目录被 Git 忽略，CI 应将其上传到持久化制品存储。

重跑时使用 `--parent-run-id <run-id>` 建立关联。`TESTCTL_PYTHON=/absolute/path/to/python` 可以显式选择受控 Python；工具保留符号链接入口，避免丢失虚拟环境语义。

`TESTCTL_NETWORK_MODE=strict` 使用 Bubblewrap 网络 namespace，能力不足会将必需套件标为 `BLOCKED`。未设置时为 `audit`，不具备强隔离保证。pytest 使用 signal 单例超时；本地 HTTP 服务可由 suite 的 `services` 声明，动态端口由服务绑定端口 0 并通过 `TESTCTL_PORT=<port>` 报告。

在允许无密码 `sudo` 的专用 CI runner 上，可显式设置 `TESTCTL_BWRAP_SUDO=1`，使 strict 模式以 `sudo -n -E bwrap` 建立网络 namespace。普通本地运行保持非特权路径；若特权 probe 不可用，仍明确标为 `BLOCKED`，不会悄悄降级到 audit。

`pr-stable` 仅覆盖首批稳定套件，不代表全量回归。Web 106 脚本和 TUI 完整入口已进入独立诊断 profile；Web 已知 6 个脚本失败，暂不进入 PR 绿色门禁。GitHub Actions 已提供 PR、主干及夜间稳定回归入口，结果上传为制品；远程执行状态与发布级不可变归档仍需在平台上验证。`archivectl.py prune --execute` 会删除已到期的本地运行目录，默认仅预览。

`full_regression.py` 是独立于 PR 稳定门禁的全量 Python 诊断入口，仅由每日定时 CI 自动触发；提交/PR/合入及手动 workflow_dispatch 只运行 stable。默认每片约 250 例、2 个 worker、单例 30 秒/分片 1,800 秒上限；显式保留 `--asyncio-mode=auto`，因为清除 pytest 默认 `addopts` 时该模式也会被清除。归档位于 `artifacts/test-runs/full-python-<UTC>/`，保存提交和锁文件指纹、全仓及分片收集日志、JUnit、闭合摘要与逐例 `failure_inventory.csv`。Desktop `webview` 来自 `desktop` extra；未安装时应报告 `not_run`，不能把模块级 collection skip 当作逐例跳过。全量中的任何失败都会如实标红并上传证据。

每日全量使用 4 个 worker；本地诊断仍可用 `full_regression.py --workers 2` 降低并发。全量结果按实际 SHA 留档，不作为每次提交/合入的必经门禁；不因调整频率放宽分片大小、超时、失败归因或闭合判定。

完整 Python 套件中有调用 Web 前端 `jsdom`/`vite` 的跨语言用例，因此夜间 job 同时对 Web、Browser、TUI 前端执行 `npm ci`；这不等于已经运行 Web 的 106 个独立脚本。

全量入口要求 `summary.closed=true` 且 `not_run=0`；即使 pytest 本身退出 0，收集差异、缺失用例或归因清单生成失败也使任务失败。动态参数 ID 不自动按函数名合并；marketplace ZIP 用例使用稳定参数 ID。

## Goal / Heartbeat 持续回归与真实验收

`pr-stable` 增加 Goal 控制、历史、Heartbeat 调度与 Runtime 生命周期的 619 项确定性回归，结合已有 harness 套件，在 PR / develop push 自动运行。`tools.tests.test_provider_acceptance` 检查相关 Goal/Heartbeat 模块在发现和执行列表中均有覆盖。全量 Python 仍从整个 `tests/` 自动收集：新增 Native 4 项、Codex 用户输入优先 1 项、原 Web UI 的 Native/Codex 2 项均在仓库内；正常全量中这 7 项按用例明确跳过。

独立的 **Real Provider and browser acceptance** workflow 仅允许手动触发，在 `provider-acceptance` GitHub environment 中运行。`local-cli` 不调用外部模型；其余选项必须显式配置该 environment 的 `HEARTBEAT_REMOTE_API_BASE`、`HEARTBEAT_REMOTE_API_KEY`、`HEARTBEAT_REMOTE_MODEL` 三个 secrets，不读取个人配置。远端验收会把合成目标、系统提示和工具结果发送给配置的模型端点，并产生 API 用量；该授权由有权手动运行工作流的人作出。缺配置、依赖、浏览器或前端构建均失败，不能变成绿色 skip。

```bash
uv sync --locked --group test --extra codex --python 3.13
# UI / all 额外需要原前端构建和 Chromium
npm ci --prefix jiuwenswarm/channels/web/frontend
npm run build --prefix jiuwenswarm/channels/web/frontend
.venv/bin/python -m playwright install chromium
# 在环境中提供上述三个变量后，选择一个明确的验收范围
.venv/bin/python tools/run_provider_acceptance.py --suite all --output artifacts/test-runs/provider-acceptance
```

范围与严格预期计数：`local-cli` 2、`goal` 12（渠道7 + Native4 + 用户输入优先1）、`heartbeat` 4、`ui` 2、`all` 18（goal + heartbeat + ui）。本地未提交开发可显式加 `--allow-dirty`，CI 要求干净 checkout。runner 核对 core 声明/锁/实际非 editable 导入、测试前后源码与前端指纹、收集/JUnit逐例身份、零 skip、进程清理和归档脱敏。只上传清理后的摘要和有限证据，不上传配置、Provider home 或原始聊天日志。必要救援清理也令任务失败。

普通 full-python 的 opt-in skip 仅说明用例已纳入收集；真实验收通过必须另有本工作流或同入口本地执行的结果证据。新的 workflow 文件入库并不等于远端模型 secrets 已配置或真实 CI 已通过。
