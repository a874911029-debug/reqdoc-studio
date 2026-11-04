# ReqDoc 自动更新器迁移与发布说明

## 当前范围

本阶段只迁移应用内更新通道，不发布新的任务编辑器功能。自动构建包仅包含：

- \`_internal/reqdoc_updater.py\`
- \`updater.ps1\`
- \`version.json\`

不包含 EXE、Python 运行库、业务模块或 \`reqdoc-data\`。

## 更新通道

- 源码：\`main\`
- 持久更新分支：\`updates\`
- 清单：\`updates/latest.json\`
- 更新包：\`updates/reqdoc-update-vMAJOR.MINOR.PATCH.zip\`

应用从清单读取版本、包体积和 SHA-256，下载后校验并调用独立 PowerShell 更新器。

## 版本要求

当前迁移版本为 \`7.5.3\`，最低支持直接迁移版本为 \`7.5.2\`。后续更新器版本必须递增 \`src/reqdoc_updater.py\` 中的 \`APP_VERSION\`。

## 首次迁移

V7.5.2 旧更新器只访问 GitHub Releases，不能自行发现 \`updates\` 分支。首次迁移需要一次性安装 V7.5.3 更新器迁移包。完成迁移后，后续更新器版本可通过应用内更新流程安装。

## 数据与回滚

应用在启动外部更新器前保存草稿并备份数据。更新器只写入白名单中的三个更新器文件，备份将被覆盖的文件，失败时回滚；\`reqdoc-data\` 不在更新包白名单内。

## 尚未完成的事项

1. 逐文件核对 GitHub \`src/\` 与当前 V7.5.2 安装包中的模块，消除源码版本漂移。
2. Windows 实机验证首次迁移、更新后启动、失败回滚和用户数据完整性。
3. 完成源码核验后，才把主程序模块加入增量更新白名单。
