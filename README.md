# ReqDoc · 项目、文档与任务工作台

Windows 本地文档与任务管理工具。

## 更新系统状态

本仓库正在把应用内更新从 GitHub Releases 迁移到同仓库的 \`updates\` 分支。

- 源码：\`main\`
- 自动构建：GitHub Actions / Windows / Python 3.12
- 更新清单：\`updates/latest.json\`
- 更新包：\`updates/reqdoc-update-vMAJOR.MINOR.PATCH.zip\`

每次更新器源码版本变更后，Actions 会运行语法检查、回归测试、更新包构建和 ZIP 安全校验；全部通过后自动发布到 \`updates\` 分支。不再要求手动创建 GitHub Release 或上传 Release 附件。

## 首次迁移边界

当前 V7.5.2 安装包中的更新器仍访问旧的 GitHub Releases 通道，因此它不能自行发现新的 \`updates\` 通道。首次迁移需要一次性安装更新器迁移包（V7.5.3）。这次只替换 \`_internal/reqdoc_updater.py\`、\`updater.ps1\` 和版本清单，不替换主程序 EXE、Python 运行库或用户数据。

首次迁移完成后，后续更新器版本可由应用内更新按钮自动检查、下载和安装。

## 数据保护

- \`reqdoc-data\`、用户项目、文档、数据库和原始资料不进入更新包。
- 当前迁移包只允许更新器模块、独立 PowerShell 更新器和版本清单。
- 下载后执行 SHA-256、ZIP 路径、文件清单、CRC 和版本一致性检查。
- 安装前备份被覆盖的更新器文件；失败时回滚。
- 更新器不会删除、覆盖或搬动 \`reqdoc-data\`。

## 源码与构建边界

旧版 \`reqdoc\` 基础引擎的完整构建源码仍未全部进入仓库。当前自动流水线只构建更新器迁移补丁，不重建 EXE，也不宣称已完整开源或可独立重建整个应用。

主程序及相关模块的源码与当前已安装 V7.5.2 包仍需完成逐文件一致性核验，之后才能把普通应用功能文件纳入自动增量更新白名单。
