# 文件服务接入 Google Drive / gdirect

日期：2026-11-01（Asia/Tokyo）

## 目标与本次授权

为现有 `simple-netdisk-flomo` 接入服务器配置的指定 Google 账号，保留现有服务器后端与 GitHub Pages 前端。用户已明确要求实施、接入与部署；先写本文档再实施。服务器登录凭据仅在连接时使用，不写入项目、文档、前端或提交记录。

## 分流约定

- 新上传文件 **大于 30 MB** 使用 Google Drive 存储，并接入 gdirect 可用的传输能力。
- 新上传文件 **小于或等于 30 MB** 继续使用现有服务器存储。
- 采用十进制：30 MB = 30,000,000 字节。恰好等于阈值归现有服务器。
- 已有文件保留原存储位置；本次不批量迁移、不替换现有资料。
- 后端以实际接收字节数判定；前端大小提示仅改善反馈。
- Drive 凭据由后端保管；前端不能收到刷新令牌、客户端密钥或服务器凭据。

## 拟实施结构

1. 读取实际前后端源码及项目交接，定位上传、文件元数据、下载、删除与部署方式。
2. 为文件元数据添加兼容旧记录的存储标记及 Drive 文件 ID；统一现有列表和文件操作入口。
3. 大文件流式接收或临时落盘后上传 Drive，完成后仅保留必要元数据；避免将整文件读入内存和在服务器永久保留第二份副本。
4. 服务器文件沿用原逻辑；Drive 文件经服务端鉴权下载或合适的受控链接，沿用原访问边界。浏览器不能原生执行 gdirect，具体路径按现有后端与 gdirect 最新能力落实。
5. 使用 Chrome 的指定 Google 账号完成必要配置。若需要新增 OAuth 客户端或授权持久访问，按工具要求在最终确认动作前展示具体账户、权限和用途。
6. 保留可回滚的代码与部署备份，只更新本项目服务。

## 必需验证

- 阈值判定：29,999,999 / 30,000,000 / 30,000,001 字节。
- 一份服务器小文件和一份 Drive 大文件：上传 → 列表 → 下载 → 内容校验。
- 现有文件列表与服务健康正常；前端部署可访问。
- 记录真实结果与未完成项，不把配置完成等同于传输成功。

## 实施状态

当前：代码已实现并推送，前端提交 `845c889` 已由 Pages 构建成功，线上 HTML 已包含转存逻辑。后端部署包已暂存在服务器，线上后端尚未切换，等待 Google OAuth 长期访问确认。

## 已落实的实现

- 后端：Python 标准库 HTTP 服务；大文件仍走原有 4 MiB 分片续传，完成后后台调用固定版本 gdirect v0.9.0 上传 Drive。
- gdirect 使用 `--google-only`，明确排除新版默认尝试的第三方中继；服务器按 Google DNS 解析结果连接 Google。
- 大文件元数据保存于服务数据目录的 `drive-files.sqlite3`；Drive 原文件保持私有，应用的既有公开读取入口继续由后端流式代理。
- `drive.file` 权限通过专用授权脚本获得，再导入 gdirect；不直接运行请求全盘权限的 `gdirect login --client-secret`。
- 转存并发上限 2；下载以 1 MiB 块流式转发，不把完整大文件读入内存或为下载创建持久缓存。
- 转存失败保留原分片文件和进度，不回落为服务器永久存储。
- 前端轮询转存状态，避免长请求超时；原列表、备注、下载、删除入口兼容两种存储。

## 已执行验证

- Python/前端 JavaScript 语法通过。
- 29,999,999 / 30,000,000 / 30,000,001 字节分流判定通过。
- 服务器隔离临时数据：小文件分片上传、完成、列表、下载内容比对、删除通过。
- gdirect v0.9.0 Linux 二进制已安装并能运行；发布包哈希与上游 SHA256SUMS 对应条目一致。
- Drive 真实上传与下载尚未进行，等待指定账号 OAuth 授权。

## 运行配置

`SIMPLE_NETDISK_GOOGLE_ACCOUNT` 在服务器 systemd drop-in 中配置；不在前端设置。
凭据：`/srv/simple-netdisk/gdirect/state.sqlite3`，仅服务用户可读写；文件夹 ID：同目录 `folder-id`。
后端：`/opt/simple-netdisk/`；前端仍由本仓库 GitHub Pages 部署。

## 授权与回滚

OAuth 应用若保持 Testing 状态，Drive 刷新令牌会受 Google 的测试期限限制；正式长期使用须完成对应发布设置，并确认只申请 `drive.file`。
Google 账号、客户端 JSON、授权码和刷新令牌不得提交仓库。
切换前的服务器源码已备份。尚无 Drive 文件时可以恢复原源码；已有 Drive 文件后必须保留 Drive 读取兼容层，不能直接回退旧版隐藏这些文件。

来源：[gdirect v0.9.0](https://github.com/mizorewww/gdirect/releases/tag/v0.9.0)、[Google Drive 权限说明](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)、[OAuth 令牌有效期](https://developers.google.com/identity/protocols/oauth2#expiration)。
