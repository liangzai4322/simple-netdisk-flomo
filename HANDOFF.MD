# Google Drive 大文件接入交接

更新：2026-11-01（Asia/Tokyo）

## 目标与当前状态

- 用户已授权实施并部署：新文件 >30,000,000 字节走 Drive，≤阈值仍在原服务器。旧文件不迁移。
- 先写的方案为 `docs/drive-integration.md`。研究目录另有原始方案和研究 HTML。
- 已部署：两种存储列表/备注/下载/删除，原分片续传接后台 gdirect 转存；前端处理后台转存轮询。
- 指定账号已在 Chrome 的“同学”资料中核对；另一个 Chrome 扩展连接的资料是其他账号，禁止在其 Drive 操作。
- 已创建 Cloud 专用项目 `simple-netdisk-drive`（ID `planar-catbird-513410-q0`）、Desktop 客户端，启用 Drive API，应用名“无痕同步 Drive”。
- 用户已单独确认长期访问范围；Desktop OAuth 客户端和 `drive.file` 授权已完成。Google Cloud 受众页面刷新后明确显示“正式版”。
- 后端已切换并重启，`/health` 返回 `drive_ready=true` 和阈值 30,000,000。真实公网两种存储验收已通过，测试文件已清理，原列表前后均为 118 项。
- 前端功能提交 `845c889` 已推送；最新隐私说明提交 `c3714a9`。`privacy.html` 已部署并填写进 OAuth 应用信息。

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
- gdirect 已成功导入凭据，accounts 精确匹配指定账号。私有 `simple-netdisk` 文件夹已创建；没有新增公开共享权限。
- 刷新令牌仅保存在服务器受限 SQLite，Mac 授权输出和服务器导入 JSON 已删除；Mac 仅保留 Desktop 客户端配置。
- 切换后服务 active、Drive ready；公网小文件上传、列表、下载 SHA-256 一致已通过，Drive 大文件上传、列表、下载 SHA-256 一致已通过，测试文件和分片记录已清理。

## 已完成与后续入口

- 线上接入已完成，使用既有页面即可自动按大小分流。
- 本轮实际验收记录：`docs/drive-acceptance.json`。
- 本地结果页：`/Volumes/Samsung SSD/page/202610/outputs/20261101-netdisk-drive-integration/index.html`。
- 小文件 4,096 字节走 server；大文件 30,000,001 字节走 drive；下载 SHA-256 均一致，测试数据均已清理。
- 大文件本次分片上传 138.095 s，Drive 完成/轮询 89.640 s，下载 93.532 s；属于单次网络观测。
- 每秒采样的服务进程组内存最高 108,027,904 字节（约 103.0 MiB）；Python RSS 最高约 27.3 MiB、gdirect RSS 最高约 70.7 MiB。结束后服务内存约 15.5 MiB、重启计数 0。
- 旧原始后端仍可做代码参考；存在 Drive 文件后，回滚必须保留 Drive 读取兼容，禁止直接换回不识别 Drive 的旧版。

## 本次踩坑

- `gdirect` v0.9.0 默认尝试第三方中继，必须显式使用 `--google-only`。
- 标准 `login --client-secret` 固定请求全盘 scope；本接入用自有 `drive.file` 授权后 `login --credentials` 导入。
- Chrome 扩展当前连接的个人资料不等于用户常用资料；先在页面核对 Google 邮箱，再进行配置。
- `exec_command` 返回 session_id 表示命令尚未结束；依赖的复制/备份必须等其结束，不能仅按调用顺序假定远端完成。
- 上传部署文件应先写 staging 路径；本次曾先复制到源码路径，已在服务重启前恢复原始源码，并核对原始哈希。后续仅在正式切换时覆盖。
- 不扫描整个 nginx 历史备份目录；本服务只看 sites-enabled 指向的生效配置。
- Python 默认 User-Agent 被 Cloudflare 返回 403/1010；同一路由使用普通浏览器 User-Agent 返回 200。不是后端或 Drive 授权失败。公网验收脚本显式设置 User-Agent。
- Google Cloud 旧标签可能保留“测试”缓存，刷新后显示“正式版”；以刷新后的状态为准。
