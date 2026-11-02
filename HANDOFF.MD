# Google Drive 大文件接入交接

更新：2026-11-02（Asia/Tokyo）

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

## 2026-11-02 服务器 / Drive 单次测速

- 通过线上 HTTPS 接口，各上传并下载一次约 30 MB 不可压缩随机文件；服务器样本 30,000,000 字节，Drive 样本 30,000,001 字节。
- 当前 Mac 使用已配置系统代理网络路径；不是服务器裸带宽测量。两次下载 CF-Cache-Status 均为 MISS，SHA-256 均一致。
- 服务器：上传至可用 114.291 秒，0.2625 MB/s；下载 88.021 秒，0.3408 MB/s。
- Drive：上传至可用 204.832 秒，0.1465 MB/s；其中电脑至服务器 112.957 秒、0.2656 MB/s，转存及完成确认 91.875 秒、0.3265 MB/s；下载 94.598 秒、0.3171 MB/s。
- 本次下载响应头等待：服务器 1.383 秒，Drive 6.448 秒；正文传输速率分别 0.3463 / 0.3403 MB/s。
- MB 为十进制。用户上传平均速度必须计入转存完成确认，不能将仅上传到服务器的速率当作 Drive 完整上传速率。
- 两份测试文件已从网盘移除（Drive 文件移入回收站），分片记录与本地数据已清理；前后列表条目均为 127。没有修改生产配置。
- HTML 与原始 JSON：`/Volumes/Samsung SSD/page/202610/outputs/20261102-netdisk-speed/`。每条路径只有一次样本，属于当时网络观测。

## 2026-11-02 原文加速链路核对与本机补测

- 用户质疑文章宣称加速而网页测速接近。已核对：当前生产下载 `serve_drive` 通过普通 `DRIVE.request(alt=media)` 流式转发，没有调用 gdirect 下载；上传强制使用 DNS 解析 IP（`--ip`），跳过自动选点。因此此前接入完成的是存储分流，网页测量没有覆盖作者的本机直连下载路径。
- 补测同一个 30,000,001 字节合成文件：Mac 本机 gdirect 自动选点、`--google-only`、8 连接，16.334 秒，1.8367 MB/s，SHA-256 一致；约为网页 Drive 下载 0.3171 MB/s 的 5.79 倍。本次不同客户端与连接数同时变化，不可将全部差异仅归给一个组件。
- 选中端点 74.125.143.100，SNI g.cn，证书校验开启。gdirect 扫描提示 2,048 地址中 2,040 个响应 TCP，疑似 TUN/VPN 接管；对选中 IP 的系统路由确认为 utun7。所以这次仍是保留系统 TUN 的本机下载，未验证真正直连或作者家宽宣传速率。没有修改系统代理/TUN设置。
- 凭据限制保持：仅临时通过 SSH 获取短期访问令牌到进程内存，刷新令牌没有复制到本机；本机测试 SQLite 的已保存账号数为 0。合成文件临时从回收站恢复，补测后已重新移入回收站，本地下载已清理。
- 新证据：`/Volumes/Samsung SSD/page/202610/outputs/20261102-netdisk-speed/native-gdirect.json` 与 `.log`；同目录 HTML 已补充覆盖范围与本机对比。
- 后续不得把“使用 Drive 存储 / 后台 gdirect 上传”等同于“网页已实现客户端直连加速”。实现原文的本机路径需要处理客户端组件、网络分流与访问权限边界；不要向网页暴露可写 Drive 令牌。

## 2026-11-02 物理直连与加速组件

- 用户明确要求真正直连测试；足够快时考虑小于 30 MB 也使用 Drive，并接入加速。已单会话实施，没有委派。
- 用 `IP_BOUND_IF` 将测速进程 Internet socket 绑定 en1（index 17），每次校验绑定值；物理接口路由经 192.168.1.1。系统代理和 TUN 保持原状。仅 Google GTS 根证书与 Google 节点；不启用第三方中继。
- 直连 30,000,001 字节：下载 9.571 秒 / 3.1344 MB/s；上传 35.139 秒 / 0.8537 MB/s。1 MB：上传 3.815 秒，下载 4.219 秒。SHA-256/MD5 与大小均匹配，三个合成 Drive 文件已移回回收站，本地副本已清理。
- 相同 1 MB 服务器路径：上传 8.430 秒，下载 3.035 秒，内容一致并清理。因此小文件上传更快、下载稍慢，已向用户询问是否全部切 Drive；尚未收到选择，默认保持原阈值。
- 证据目录 `/Volumes/Samsung SSD/page/202610/outputs/20261102-netdisk-physical-direct/`。第一次固定端点连接失败，记录单列为 `failed-first-endpoint.json/.log`；失败样本的 MB/s 为 null。后续使用已核验端点缓存自动选择完成全部传输。117 个端点通过绑定网卡的 Google TLS + Drive API 探测，不能将这一探测等同于持续文件测速。
- 本轮代码：`scripts/native_accelerator.py` 复用原上传分片/续传，使用本机 gdirect 直传与 8 连接下载；`scripts/bind_physical.c` 仅绑定子进程。后端增加配对 broker，网页收不到 Google 写令牌，刷新令牌仍只在服务器。
- 原生组件 `/Volumes/Samsung SSD/Applications/simple-netdisk-accelerator/`；本机配置 `~/Library/Application Support/simple-netdisk-accelerator/config.json` 0600；大文件分片在 SSD Inbox/.simple-netdisk-accelerator，下载在 Inbox/无痕同步下载。配置含配对密钥与本网盘密码，不输出、不提交。
- macOS LaunchAgent 读取外置盘脚本被 TCC 拒绝；已移除失败的 LaunchAgent，由当前已授权用户进程启动，并提供 `启动加速.command`。没有设置成功的开机自启；重启 Mac 后需用户启动该脚本。
- 公网 nginx 白名单不会自动放行新 API。已为 `/pan-api/accelerator/` 与 8090 的 `/api/accelerator/` 增加独立代理 location，并通过 nginx -t/reload；后端 `helper-key` 位于原 gdirect 私有目录，0600、filebrowser 所有。源文件及 nginx 备份后缀 `bak-20261102-accelerator`。
- 集成验收样本 `codex-accelerator-acceptance-a7b3235abc.bin`（30,000,001 字节，upload_id `04a39cc43d037708b339621051c5dc1fd51e47e36921823b8bb444ff608c4752`）已通过本机组件直传并进入正式 Drive 文件列表；修复 nginx 后复用原本机分片，完成阶段 23.503 秒，不将这段时间称为包含首次本机接收的完整上传耗时。已发布网页实际点击“加速下载”，下载的 30,000,001 字节 SHA-256 与上传内容一致；本机分片、完成记录、下载副本及正式列表测试文件全部清理，前后均为 127 项。删除后本机完成记录失效也验证通过。功能提交 `1e52ac2` 已上线，页头长路径布局修复提交 `b11e2b8` 已推送。
- SSH 原直连复用连接曾中途失效；现使用已配置 127.0.0.1:10808 SOCKS5 代理 + ServerAlive，ControlPath `/tmp/codex-netdisk-physical-20261102.sock`。macOS nc 的 HTTP CONNECT 对该代理 200 响应不兼容，SOCKS5 正常。结束后关闭该连接。

- 配对 broker 验证：缺少 helper key 时 403，携带浏览器 Origin 即使带正确 helper key 也返回 403；Google token 不允许浏览器获取。运行中的本机组件 PID 32383，仅监听 127.0.0.1:18743。后端 active、NRestarts=0，空闲 cgroup 内存 14,946,304 字节。

- 最终快照：后端 active、NRestarts=0、服务内存 15,818,752 字节（约 15.1 MiB）；本机组件 RSS 17,936 KiB、CPU 0.0%。Google 全局路由仍为 utun7，绑定物理网卡后的作用域路由为 en1，证据 runtime-state.json。原生路径的 gdirect 账号数为 0。
- 网页下载成功后发现长本机路径会挤压品牌标题，已将状态区设为可收缩 flex，保留完整路径 title，并让加速按钮独占工具栏第二行，避免窄屏溢出。
- 最终 HTML 报告：`/Volumes/Samsung SSD/page/202610/outputs/20261102-netdisk-physical-direct/index.html`。原始数据与集成验收记录位于同目录。

- 初版 `.private/deploy_accelerator.py` 含已否定的 LaunchAgent 实验，不作为更新入口；更新组件文件后使用安装目录的 `start_accelerator.py` / `启动加速.command`。当前启动器会先探测组件，已运行时不会重复启动。
