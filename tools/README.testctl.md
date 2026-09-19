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

`full_regression.py` 是独立于 PR 稳定门禁的全量 Python 诊断入口，夜间或手动触发。默认每片约 250 例、2 个 worker、单例 30 秒/分片 1,800 秒上限；显式保留 `--asyncio-mode=auto`，因为清除 pytest 默认 `addopts` 时该模式也会被清除。归档位于 `artifacts/test-runs/full-python-<UTC>/`，保存提交和锁文件指纹、全仓及分片收集日志、JUnit、闭合摘要与逐例 `failure_inventory.csv`。Desktop `webview` 来自 `desktop` extra；未安装时应报告 `not_run`，不能把模块级 collection skip 当作逐例跳过。当前全量基线仍含失败，夜间任务会如实标红并上传证据。

全量入口要求 `summary.closed=true` 且 `not_run=0`；即使 pytest 本身退出 0，收集差异、缺失用例或归因清单生成失败也使任务失败。动态参数 ID 不自动按函数名合并；marketplace ZIP 用例使用稳定参数 ID。
