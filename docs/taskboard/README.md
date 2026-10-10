# Taskboard 最小版 Demo

本分支在 Swarm 正式 `3bbcacdae3c44aba74263040550c770019795c63` 上开发，固定 Core `91943c6b6d2aa518eb8c5cd03a382c5afdc38e06`，Core/uv.lock 未修改。内置应用插件＋bundled UI，业务数据仅保存在 AgentServer 实例目录。

## 安装和启动

在本分支 worktree 根目录运行（Python 3.13、Node 22、npm）：

```bash
uv sync --locked --group test --python 3.13
cd jiuwenswarm/channels/web/frontend
npm ci
npm run build
cd ../../../..
.venv/bin/python scripts/taskboard_demo.py start
.venv/bin/python scripts/taskboard_demo.py status
```

访问 <http://127.0.0.1:19240/taskboard>。四个独立端口：Web19240、Gateway Web19241、AgentServer19242、Gateway辅助19243。可传 `--base-port`、`--data-dir`，同一套 start/stop 使用相同参数。不会占用原19100部署；数据默认 `.taskboard-demo/`，配置、日志、PID 清单均独立。

停止和恢复：

```bash
.venv/bin/python scripts/taskboard_demo.py stop
.venv/bin/python scripts/taskboard_demo.py start
```

停止脚本只处理清单里且命令行与本 worktree 一致的进程。启动前检查端口，首次进程初始化约20～45秒；`start` 返回代表已发起启动，使用 `status` 和日志检查就绪。数据不会随停止删除。进程工作目录也固定到数据目录，运行缓存不写源码根。任务库在 `.taskboard-demo/agent/taskboard.sqlite3`；仅修改主题或语言不改变数据。

首次生成 Demo 配置默认不配置模型，关闭自动心跳、定时任务和多模态模型探测；Taskboard CRUD 不依赖 Provider。如需真实执行，通过现有设置页配置模型/执行 Provider，然后使用原会话入口发消息。配置文件含凭据时不纳入 Git。不要把无模型会话关联测试描述成真实模型执行成功。

## 使用闭环

1. Code 侧栏进入任务看板，新建标题/描述/项目/优先级；任务默认待处理。
2. 点卡片编辑，拖到进行中或在详情切换状态。
3. 先通过原新建会话入口建立 Code 会话；详情“关联已有会话”选择当前实例现有会话。关联不启动执行。
4. 打开会话，沿用原历史恢复/执行/权限；顶部“返回任务详情”回到任务。
5. 写结果记录，保存；人工“标记完成”，需要时“重新打开”。刷新和重启后保留记录。
6. 使用标题/编号搜索、项目筛选。冲突保留当前输入，复制草稿后加载最新版本；不会静默覆盖。

## 验证

后端受影响范围：

```bash
JIUWENSWARM_DATA_DIR=/tmp/taskboard-tests timeout 180 .venv/bin/python -m pytest tests/unit_tests/test_application_plugins.py tests/unit_tests/server/taskboard tests/unit_tests/governance/test_application_boundary.py tests/unit_tests/gateway/test_e2a_proxy.py tests/unit_tests/server/test_project_governance_boundary.py tests/unit_tests/server/test_project_access.py -q --no-cov --timeout=30
cd jiuwenswarm/channels/web/frontend
npm run test:taskboard
npm run build
```

真实 Chrome 验收：安装 Playwright 或通过 `PLAYWRIGHT_MODULE` 指定既有模块；`CHROME_PATH` 可指定浏览器。可先在隔离 Demo 内用 `.venv/bin/python scripts/taskboard_seed_demo.py` 通过真实RPC创建或复用名为 `Taskboard Demo` 的Code项目及已有Web Code会话。脚本通过实际 RPC/UI 创建样本，不拦截或模拟响应。建议使用空任务库，重复运行会新增样本。

```bash
PLAYWRIGHT_MODULE=/path/to/playwright node scripts/taskboard_browser_demo.cjs
```

补验已有样本的重启、真实双写冲突、返回及英文/640px窄屏：

```bash
PLAYWRIGHT_MODULE=/path/to/playwright node scripts/taskboard_browser_followup.cjs
```

可选 `REAL_PROVIDER=1` 需要按现有模型/资源/权限机制提供可用配置。首次交付的无显式profile Native请求被181006拒绝，失败保留。随后用户指定火山后，本机隔离Demo已配置火山glm-5.2、显式Native profile及精确项目模型资源，真实Native回复通过；TB-002已关联成功会话。参见验证报告末节。新建安装不会带有本机密钥或资源登记。

截图和机器结果位于 [evidence](evidence/)。准确结果与未验证范围见 [验证报告](VERIFICATION.md)。容器验证只使用任务专属容器和挂载目录，未覆盖默认运行时镜像，也未接入或修改原元戎在线部署。
