# 示例导航

在项目根目录运行示例。配置文件用于参考，实际服务器配置由启动器保存在 `data/config/Tiffany.toml`；部署命令见 [部署指南](../docs/guides/deployment.md)。

| 示例 | 用途 | 运行或使用方式 |
| --- | --- | --- |
| [Tiffany.onebot.toml](Tiffany.onebot.toml) | OneBot/NapCat 反向 WebSocket、调度、监控与可选扩展配置 | 启动向导选择 OneBot，按需修改运行配置 |
| [Tiffany.qqofficial.toml](Tiffany.qqofficial.toml) | QQ 账号、Intents、HTTP 额度与可选扩展配置 | 启动向导选择 QQ，凭据单独保存 |
| [qqofficial.py](qqofficial.py) | 使用公开 API 组装 QQ Adapter、旧 Ping Hook 和运行时 | 安装 `qqofficial` 可选依赖，配置 AppID/环境变量后运行 `python -B -m examples.qqofficial` |
| [extensions.py](extensions.py) | 无账号演示命令、Scope 和提交后确认的 SQLite 笔记 | 安装项目基础依赖后运行 `python -B -m examples.extensions`；数据使用临时目录 |
| [plugins/](plugins/README.md) | 标准 pyproject 本地源码插件模板 | 将 echo 项目复制到 home/plugins，使用正常启动脚本 |
| [legacy/](legacy/README.md) | 旧配置格式的迁移参考与回归夹具 | 用 `configure --import-config` 导入，之后执行 `check-config` |

核心独立 Hook 示例见 [项目 README](../README.md#写第一个-hook)，配置化扩展的权限、限流和持久数据边界见 [业务扩展指南](../docs/guides/extensions.md)。
