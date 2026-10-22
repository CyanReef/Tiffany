# 本地插件示例

将整个 `echo/` 目录复制到 `<home>/plugins/echo/`，使用原来的启动脚本启动。首次安装依赖需要联网；已有环境重复启动不运行 pip。发送 `/echo hello` 验证回复。

可在 `<home>/config/echo.toml` 设置 `prefix = "/echo "`。开发独立仓库时，在 `<home>/config/plugins.toml` 的 `paths` 中填写项目路径，无需复制源码。声明、命令与失败恢复见 [插件指南](../../docs/guides/plugins.md)。
