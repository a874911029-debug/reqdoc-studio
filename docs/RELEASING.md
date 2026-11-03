# 在唯一公开仓库发布 ReqDoc

仓库：`https://github.com/a874911029-debug/reqdoc-studio`

## 首次发布（V7.5.0）
1. 打开 [Releases / New release](https://github.com/a874911029-debug/reqdoc-studio/releases/new)。
2. 新建版本标签 `v7.5.0`，Release 标题 `ReqDoc V7.5.0`。
3. 把经过本地测试和 ZIP 校验的**安装包**上传为 Release 附件，文件名必须严格为 `reqdoc-studio-win64-v7.5.0.zip`。
4. 发布为普通 Release，**不要**勾选「pre-release」。
5. 发布后确认 GitHub API：`https://api.github.com/repos/a874911029-debug/reqdoc-studio/releases/latest`。
6. 检查 `assets[].digest` 格式为 `sha256:<64位十六进制>`、`size` 正确，安装包点击可以下载。

**限制：** 当前连接的 GitHub 插件只能管理源码/提交，没有上传 Release 二进制附件的能力；所以首次安装包必须通过 GitHub Releases 页面上传一次，不能把“已提交源码”误认为“已发布安装包”。

## 更新流程
- V7.5 起，`src/reqdoc_updater.py` 只访问这个仓库的 `releases/latest`。
- 发布新版时使用 `vMAJOR.MINOR.PATCH` 标签，并上传 `reqdoc-studio-win64-vMAJOR.MINOR.PATCH.zip`。
- 软件从 Release 元数据取得资产 URL、大小及 GitHub SHA-256 摘要；严格校验后才下载/安装。
- 不再维护 `latest.json`，也不再建立 `reqdoc-studio-updates` 仓库。
- Windows 安装前备份项目数据，独立更新器不得覆盖 `reqdoc-data`。

## 从旧版本迁移
V7.3 / V7.4 的更新器仍访问旧地址，**不能直接通过其旧更新按钮升级**。用户需要第一次手动安装 V7.5；以后在正式 Release 通道启用后才可在软件中检查和安装新版本。

## 发布安全核对
- 不包含 `reqdoc-data`、用户项目、原始资料、数据库、日志、密钥或测试私有文件。
- 仅上传已验证 ZIP；核查 ZIP CRC、文件列表与 SHA-256。
- 源码使用正常 Git 提交，不把 52 MB 便携 ZIP 提交到 Git 历史。
- 更新流程完整测试（下载、校验、数据备份、退出旧进程、安装、回滚、重新启动）通过 Windows 实机后，才能称为“自动更新已完成验收”。

## 关于源码
目前已公开持续维护的桌面应用源码、更新器和相关资料处理模块。旧版 `reqdoc` 基础引擎的完整构建源码仍待补齐，所以现阶段不能宣称可独立复现 Windows EXE。
