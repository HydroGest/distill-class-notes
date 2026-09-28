# Obsidian 配置清单(蒸馏 · 手机/PC 端)

> 目标:打开 `data/笔记/` 即用;只看、只搜、偶尔补记;**不用双链、不用图谱**。

## 1. 安装

- PC:https://obsidian.md 下载 Windows 版
- 手机(安卓):应用商店搜 Obsidian(或 GitHub release 的 APK)

## 2. 打开笔记库(Vault)

1. 打开 Obsidian → "Open folder as vault" → 选择服务器的 `data/笔记` 文件夹
   - **PC 访问方式**:如果 PC 上通过挂载访问服务器目录(如 SMB/NFS/SSHFS),直接选那个挂载路径;
   - 或者 PC 本地放一份,同步交给 Remotely Save(见第 4 步);
   - 手机:先走 Remotely Save 拉取,或只读时用浏览器看(见第 5 步)。

## 3. 必做设置(让它"不好用"的部分消失)

- 设置 → 核心插件:**关闭**「关系图谱」「图谱视图」「反向链接」「标签面板」(用不到的全部关)
- 设置 → 文件与链接 → 附件默认存放路径:**当前文件的文件夹**下的 `附件/`
- 设置 → 编辑器 → 显示行号:随意

## 4. 可选:多端同步(Remotely Save + 我们的 WebDAV)

PC 和手机都装插件 Remotely Save(设置 → 第三方插件 → 浏览 → 搜索 Remotely Save):

| 项 | 值 |
|---|---|
| 同步方式 | WebDAV |
| 服务器地址 | `http://<服务器IP>:8082/笔记` |
| 用户名 | `distill` |
| 密码 | config.yaml 里的 `webdav.password` |
| 加密 | 开启端到端加密,设一个自己的密码(别忘) |

- Remotely Save 是**打开 App 时手动/自动同步**,不是后台服务——符合"安卓零后台"原则;
- 手机端同样打开 `data/笔记` 库,同步后即可看全部笔记;
- 注意:草稿(未分类/)和定稿一起同步;校对在 PC 上做。

## 5. 手机只读方案(不想装 Obsidian 也可以)

服务器上的 `data/笔记` 已通过 WebDAV 暴露,手机装任意 WebDAV 客户端(如 Solid Explorer / CX 文件管理器)即可浏览 md 文件;或以后给笔记加一个只读网页预览(M2 可选)。

## 6. 日常使用

- 草稿在 `笔记/未分类/<日期>_<n>/`,校对时:确认课程、改头部课程字段、补易错点 → 把整个文件夹移进 `笔记/<课程>/`
- 搜索:Obsidian 全局搜索(Ctrl/Cmd+Shift+F),可搜转写稿、照片 OCR 要点
