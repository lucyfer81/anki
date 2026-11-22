# Anki 双用户 Web 单词卡

XCT、LCL 用局域网浏览器学习同一份词表。两个账号的 Anki 集合独立，评分、到期日和复习历史不共享。这个服务不与桌面 Anki 同步，也不会打开桌面用户的集合。预设账号在 `webstudy/__init__.py` 的 `USERS` 中定义（当前为 `xct`、`lcl`，登录时大小写均可）。

## 安装和启动

需要 Linux 或 macOS、[`just`](https://just.systems/)、[`uv`](https://docs.astral.sh/uv/) 和本仓库要求的 Rust 工具链。先在仓库根目录运行：

```sh
just wheels
```

切到扩展目录，使用 `out/wheels/` 里的本仓库 `anki-*.whl`（**不是** `aqt-*.whl` 或 PyPI 上的 Anki），把文件名替换为实际产物：

```sh
cd extensions/web-study
just setup '../../out/wheels/anki-<实际文件名>.whl'
just init-users
just serve 0.0.0.0 8765
```

`setup` 使用仓库根目录的 `.venv`。`init-users` 交互设置各账号（XCT、LCL）的密码，只存带盐哈希；停服后可重跑以同时更换两个密码并注销旧会话。`serve` 使用单 worker 并独占锁定 `extensions/web-study/data/`，与启动命令的工作目录和地址无关。

### 通过域名和 HTTPS 访问（可选）

如已配置 Cloudflare Tunnel + Access（例如 `https://anki.aizhi.app`），在该隧道的 Public Hostname 里把 Service 指向 `http://localhost:8765` 即可。`serve` 已启用代理头解析：仅信任回环来源的 `X-Forwarded-Proto`，因此经隧道访问时 Cookie 会带 `Secure`，局域网直连 HTTP 不受影响。Access 在 Cloudflare 边缘完成认证后再回源，比裸露的端口转发安全得多，但仍应把 Access 策略限制到具体用户或邮箱域。

**仅用于可信局域网。** HTTP 不加密密码、会话或学习内容，不要做公网端口转发。`0.0.0.0` 会监听所有 IPv4 接口；也可绑定指定的局域网 IP，或用 `127.0.0.1` 限制为本机。防火墙只放行需要访问的局域网来源。

## 导入词表

在学习页上传 UTF-8 CSV 或 TSV，先预览差异，再确认。文件的必需列是 `word,meaning`，可选列是 `id,example`。示例：

```csv
id,word,meaning,example
w001,book,书,"Read a book, please."
w002,book,预订,Book a room.
```

已有 `id` 的行会更新对应词条（可以改拼写）；没有 ID 的行若与已有词条同名，必须选择更新哪条、新增词义或跳过。省略 `example` 列会保留旧例句；要清除例句，请提供该列并将单元格留空。缺失的词条不会被删除。修订时建议先下载带 ID 的 CSV，再修改后提交。重复提交同一文件不会重新套用旧值；若别人已确认了新版本，需要重新上传以更新预览。

单个文件最大 2 MiB，最多可包含 2,000 条需要选择词义的行。超过上限时，请拆分文件或补全 ID。

目录入口运行在同一服务上，不会另开集合。在扩展目录内，把文件放到 `inbox/`：

```sh
mkdir -p inbox
just import-file inbox/words.csv --user lcl
```

输入 LCL 的密码、核对差异并确认。若服务只监听指定局域网地址或端口不是 `8765`，追加 `--url http://<服务机局域网IP>:<端口>`。如果确认后某个账号投递失败，页面会标出待重试账号；它下次取题或服务重启时会重试，也可以重新确认。其他账号的复习记录不回滚。

## 验收和备份

1. 从第二台机器打开 `http://<服务机局域网IP>:8765`。用两个独立浏览器会话分别登录 XCT、LCL。
2. 上传一条带 ID 的词。XCT 翻卡并评「简单」，LCL 应仍能看到这张新卡。通过 `inbox/` 修改同一 ID 的释义，LCL 应能看到新释义；XCT 不应因此立即重学，等卡片重新到期时会用新释义。
3. 停服重启，确认账号隔离、词条和进度仍在。停服备份整个 `extensions/web-study/data/`，不要只复制其中一个数据库；备份含密码哈希和会话，应限制访问。

每次合并上游后，在仓库根目录重新构建并更新本地 wheel，再用临时数据库跑回归测试：

```sh
# 仓库根目录
RELEASE=1 just wheels
just check

# 扩展目录
cd extensions/web-study
just setup '../../out/wheels/anki-<实际文件名>.whl'
just test
```

扩展测试验证真实 Anki 取题、评分、词条更新不改卡片身份、投递中断重试，以及网页与命令行走同一导入流程。测试只用临时数据，不接触 `data/`。
