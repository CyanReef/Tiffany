# 旧配置参考

[Tiffany.toml](Tiffany.toml) 从原项目根目录移入，保留原字节。它用于显式配置导入，普通启动读取 `data/config/Tiffany.toml`。

```powershell
.\start.bat configure --import-config examples/legacy/Tiffany.toml
```

Linux 使用 `bash start.sh configure --import-config examples/legacy/Tiffany.toml`。导入自己的旧版本配置时，将参数替换为对应文件路径；当前接入默认值和完整示例分别见 [OneBot](../Tiffany.onebot.toml)、[QQ 官 Bot](../Tiffany.qqofficial.toml)。
