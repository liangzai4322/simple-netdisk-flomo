# Google Drive 大文件接入交接

更新：2026-11-01（Asia/Tokyo）

## 目标与当前状态

- 用户已授权实施并部署：新文件 >30,000,000 字节走 Drive，≤阈值仍在原服务器。旧文件不迁移。
- 先写的方案为 `docs/drive-integration.md`。研究目录另有原始方案和研究 HTML。
- 已实现：两种存储列表/备注/下载/删除，原分片续传接后台 gdirect 转存；前端处理后台转存轮询。
- 指定账号已在 Chrome 的“同学”资料中核对；另一个 Chrome 扩展连接的资料是其他账号，禁止在其 Drive 操作。
- 已创建 Cloud 专用项目 `simple-netdisk-drive`（ID `planar-catbird-513410-q0`）；OAuth 应用表单准备到最终“创建”，尚未提交。
- Google OAuth 客户端、Drive 数据权限与生产发布设置尚未建立。浏览器工具要求在创建长期访问前单独确认，已向用户发起 `drive.file` 范围确认，等待答复。
- 服务器原后端保持运行；新代码放在 `/opt/simple-netdisk/staging-drive/`，尚未启用。

## 代码与部署

- `backend/simple_netdisk_app.py`：来自当前线上原版，移除密码硬编码回退，接入 Drive 适配层。
- `backend/drive_storage.py`：gdirect 上传、SQLite 文件映射、Google API 流式下载及回收站操作。
- `scripts/authorize_drive.py`：loopback + PKCE + offline，只申请 `drive.file`，凭据 0600。
- gdirect v0.9.0：服务器 `/opt/simple-netdisk/bin/gdirect`；Mac 本体位于外置盘 Applications/gdirect-0.9.0。
- `SIMPLE_NETDISK_GOOGLE_ACCOUNT` 由服务器 systemd drop-in 配置。
- 凭据 `/srv/simple-netdisk/gdirect/state.sqlite3`，文件夹 ID 同目录 `folder-id`，目录 0700、文件 0600、owner filebrowser。
- SSH 凭据只来自本次用户请求，不保存；不写入项目或提交。`.private/`、`vendor/` 已忽略。
- 原始后端备份 `/opt/simple-netdisk/simple_netdisk_app.py.bak-20261101-drive`；SHA256 `0360f5265eb08122da9e1941a3a03609526ab835fc7b1179bc4bc9b52e841ec8`。

## 已验证

- 阈值三个边界正确；SQLite 文件映射读写正确。
- 后端 Python 和前端 JavaScript 语法通过。
- 服务器隔离目录和独立端口完成小文件上传、完成、列表、下载内容比较、删除。测试临时目录和进程已清理。
- Linux gdirect --version 为 0.9.0。下载包对应哈希与上游 SHA256SUMS 一致。
- Drive 真实文件传输、生产分流、GitHub Pages 新版部署还未验收。

## 待执行

1. 用户确认后，完成 OAuth 应用、启用 Drive API、创建 Desktop 客户端；仅 `drive.file`。长期使用需处理 Testing 的 7 天刷新令牌限制。
2. 用指定 Chrome 账号打开授权脚本产生的 URL，核对权限并完成授权；将 authorized_user 凭据导入服务器的独立 gdirect DB，核对 accounts 为指定账号。
3. 通过 Google API 创建私有 `simple-netdisk` 文件夹，保存 folder-id；不用共享链接或公开文件权限。
4. 重新打包最新 backend（特别是环境账号配置），复制到 staging-drive，配置服务 drop-in，切换并重启本服务。
5. 发布前端与代码，确认 Pages build；只做一份真实小文件和一份 >30 MB 文件的上传、列表、下载/hash，清理本次测试文件。
6. 更新当前状态与真实证据。Drive 文件已存在后不能直接恢复不识别 Drive 的旧代码。

## 本次踩坑

- `gdirect` v0.9.0 默认尝试第三方中继，必须显式使用 `--google-only`。
- 标准 `login --client-secret` 固定请求全盘 scope；本接入用自有 `drive.file` 授权后 `login --credentials` 导入。
- Chrome 扩展当前连接的个人资料不等于用户常用资料；先在页面核对 Google 邮箱，再进行配置。
- `exec_command` 返回 session_id 表示命令尚未结束；依赖的复制/备份必须等其结束，不能仅按调用顺序假定远端完成。
- 上传部署文件应先写 staging 路径；本次曾先复制到源码路径，已在服务重启前恢复原始源码，并核对原始哈希。后续仅在正式切换时覆盖。
- 不扫描整个 nginx 历史备份目录；本服务只看 sites-enabled 指向的生效配置。
