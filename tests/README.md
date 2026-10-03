# 测试目录导航

测试使用标准库 `unittest`。按测试边界分为 `unit` 和 `integration`，再按 `core`、`onebot`、`qqofficial`、`hooks`、`deployment` 分类；共用的假对象和辅助函数放在 `support`。

结合源码学习时，可以对照 [源码地图](../docs/CODE_MAP.md) 和 [学习与开发路线](../docs/LEARNING_GUIDE.md) 找到每项行为的实现。

| 目录 | 覆盖内容 | 测试数 |
| --- | --- | ---: |
| `unit/core` | 字段懒计算、Provider/Service 注册表、隔离依赖的指标导出、核心/启动器分层 | 24 |
| `unit/onebot` | 协议字段、客户端调用、使用运行时假对象的适配器接入 | 19 |
| `unit/hooks` | 命令解析 | 1 |
| `unit/qqofficial` | 官 Bot 配置、懒字段、接纳与去重、Token 和回复调用 | 27 |
| `integration/core` | Bot 分发、Hook、追踪、生命周期、调度和 Scope 卸载、服务器边界回归 | 49 |
| `integration/onebot` | 帧处理与 Hook 的协作、适配器安装和本机服务器生命周期 | 4 |
| `integration/hooks` | Ping 回复、事件过滤和字段懒计算的完整流程 | 3 |
| `integration/qqofficial` | 本机 HTTP/WebSocket、现有 Ping 复用、恢复、过载、心跳与资源清理 | 16 |
| `unit/deployment` | 配置保护、备份、脱敏、扫码模拟、环境复用、跨平台隔离、失败恢复 | 24 |
| `integration/deployment` | 真实 HTTP/Token/健康、更新包、覆盖回滚、锁、子进程、Windows 入口和限次重启 | 17 |
| **合计** | Windows 跳过 4 项 POSIX 信号测试；Linux 跳过 5 项 Windows 入口测试 | **184** |

单元测试隔离外部服务；分层测试使用新 Python 子进程阻止相邻层和可选依赖导入，路径用临时目录。集成测试验证多个真实组件的协作，其中适配器测试会启动本机 HTTP/WebSocket 监听。两类测试均不需要外部 NapCat 或腾讯账号。完整测试先执行 `python -m pip install --require-hashes --only-binary=:all: -r requirements-server.lock`。[CI](../.github/workflows/server.yml) 在 Linux Python 3.11～3.14、Windows Python 3.11/3.14 上运行，并检查发布物不含 data。账号与 24 小时持续运行验收见[部署说明](../docs/DEPLOYMENT.md)。

## 文件布局

```text
tests/
├── unit/
│   ├── core/
│   │   ├── test_context.py                 # 字段解析与缓存
│   │   ├── test_provider_registry.py       # Provider 注册、快照和撤销
│   │   ├── test_service_registry.py        # Service 身份、依赖和撤销
│   │   ├── test_layering.py                # 核心独立使用与标准库启动器
│   │   └── test_openmetrics_exporter.py    # 指标导出与可选依赖
│   ├── onebot/
│   │   ├── test_fields.py                  # 原始协议数据转换
│   │   ├── test_client.py                  # 响应关联、超时和 pending 清理
│   │   └── test_adapter.py                 # 构造配置、接入、连接代际
│   ├── hooks/
│   │   └── test_command.py                 # 文本派生命令
│   ├── qqofficial/
│   │   ├── test_fields.py                  # 原始 Payload 的懒字段与身份边界
│   │   ├── test_config.py                  # 配置、可选依赖与工厂
│   │   ├── test_client.py                  # Token、回复序号、超时和取消
│   │   └── test_adapter.py                 # 接纳、去重、恢复检查点
│   └── deployment/
│       ├── test_storage.py                 # 路径、配置、备份、凭据与脱敏
│       ├── test_environment.py             # 环境复用、平台变化与安装失败
│       └── test_onboarding.py              # 扫码、过期、取消及提交回滚
├── integration/
│   ├── core/
│   │   ├── test_dispatcher.py              # 路由、优先级和错误策略
│   │   ├── test_hook_handles.py            # 句柄变更与在途快照
│   │   ├── test_hook_execution.py          # Hook 超时与取消
│   │   ├── test_trace.py                   # 追踪、采样和隐私
│   │   ├── test_lifecycle.py               # 启动、回滚和关闭
│   │   ├── test_scheduler.py               # 容量、并发与会话公平性
│   │   ├── test_scope.py                   # 依赖阻止与资源卸载
│   │   └── test_server_regressions.py      # 真实结束、abort、服务归属和指标边界
│   ├── onebot/
│   │   ├── test_adapter_frames.py          # 坏帧和 Hook 故障隔离
│   │   └── test_adapter_lifecycle.py       # 安装、监听和关闭
│   ├── hooks/
│   │   └── test_ping.py                    # 消息到回复的完整流程
│   ├── qqofficial/
│   │   ├── test_client_http.py             # 本机 REST 认证、错误和容量
│   │   └── test_gateway.py                 # 本机网关与 Runtime 的协作
│   └── deployment/
│       ├── test_health.py                  # 实际指标、健康变化与入站 Token
│       ├── test_processes.py               # 锁、信号、停止上限与故障重启
│       ├── test_release.py                 # 发布排除 data、覆盖更新与回滚
│       └── test_windows_entrypoints.py     # Windows 脚本、Python 探测与参数传递
└── support/
    ├── async_helpers.py                   # 同步测试的异步运行辅助
    ├── onebot.py                          # 内存 WebSocket 与运行时假对象
    └── qqofficial.py                      # 本机腾讯模拟服务与原始消息
```

## 运行方式

在项目根目录、安装项目依赖后执行。`-t .` 将项目根目录设为导入起点，使分组发现测试时的包名保持一致。

```powershell
# 全部测试
python -m unittest discover -s tests -t . -q

# 单元测试 / 集成测试
python -m unittest discover -s tests/unit -t . -q
python -m unittest discover -s tests/integration -t . -q

# 单个文件 / 单个用例
python -m unittest tests.unit.onebot.test_client -v
python -m unittest tests.integration.core.test_scope.ScopeTests.test_unload_does_not_wait_for_unrelated_event_route -v

# 分层与部署行为
python -m unittest tests.unit.core.test_layering -v
python -m unittest discover -s tests/integration/deployment -t . -q
```

原有的 `python -m unittest discover -s tests -q` 也能发现全部测试。官 Bot 可以单独运行：

```powershell
python -B -m unittest discover -s tests/unit/qqofficial -t . -v
python -B -m unittest discover -s tests/integration/qqofficial -t . -v
```

## 后续放置规则

- 直接验证对象契约、使用假对象隔离 I/O 的测试放在 `unit`；需要启动 Bot、Runtime 或本机监听的协作测试放在 `integration`。
- 一个文件聚焦一个模块或行为，用 `test_<行为或模块>.py` 命名；目录表达分类，文件名无需增加阶段编号。
- 多个文件共用的假对象放在 `support`；只供一个文件使用的辅助对象保留在该文件。
- 异步测试使用 `unittest.IsolatedAsyncioTestCase`，并在用例结束时释放任务和监听资源。
