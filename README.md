# ReqDoc · 项目、文档与任务工作台

Windows 本地文档与任务管理工具。**源码和安装包均使用这一个公开仓库**，不再使用 `reqdoc-studio-updates`。

## 功能
- 项目 → 文档 → 任务的层级管理
- TXT / Markdown / DOCX 导入与原始资料阅读
- 表格整理、任务编辑、完成进度、回收站
- Windows 便携版与应用内 GitHub 更新（发布通道准备中）

## 下载与更新
正式发布的 ZIP 将存放在本仓库的 [Releases](https://github.com/a874911029-debug/reqdoc-studio/releases)；应用内的「检查 GitHub 更新」也只查询本仓库。

**当前状态：尚未发布第一个正式 GitHub Release。** 现有旧版程序可能仍指向旧的更新仓库，不能把旧版中的更新按钮视为已可用。请勿在未备份数据时覆盖程序目录。

## 数据保护
用户项目、文档、原始文件与数据库 **不得提交到 Git**。程序升级必须排除 `reqdoc-data/`，覆盖前备份，失败时回滚程序文件。

## 源码状态
已公开 `src/reqdoc_studio.py`、`src/reqdoc_updater.py`、资料编辑、表格解析、文件拖入和表格复制模块。

**尚未完全开源的部分：** 旧版 Windows EXE 中的 `reqdoc` 基础引擎仍只有打包运行文件，当前仓库尚不能从源码完整重新构建相同 EXE。我们会在拿到并核验基础引擎源码后补齐，而不是假称已完整开源。

本项目的安装包和源码分开管理：源码放在 `src/`，约 52 MB 的便携安装包只放在本仓库 Releases 附件，不提交 Git 历史。

## 当前可下载状态
V7.5.0 单仓库引导版已完成离线打包与代码检查。**首次 Release 附件仍待上传，当前应用内下载未正式上线。**

## 安全
请勿提交 API Token、密钥、真实用户数据库、私人原始资料、个人日志、备份及临时安装文件。


## V7.5.1 task action hotfix
- Single-task actions now target the focused row, not a stale checkbox batch selection.
- Changing the selected task preserves the previous task draft before loading the new task.
- Add image opens the file picker before creating or changing an editor; cancelling has no side effects.
- Paste image checks the clipboard first and does not create an empty task when no image exists.
- Toolbar image insertion appends to the focused task; Ctrl+V in the editor preserves the caret.
- The hotfix is version 7.5.1. No GitHub Release has been published yet; the update button will not find it until a Release asset is published.
