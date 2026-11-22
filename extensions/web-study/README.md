# Anki 双用户 Web 单词卡

XCT、LCL 用浏览器学习同一份词表。两个账号的 Anki 集合独立，评分、到期日和复习历史不共享。这个服务不与桌面 Anki 同步，也不会打开桌面用户的集合。

身份认证完全由 Cloudflare Access 负责（由部署者自行配置和管控）。服务本身没有账号密码：每人打开自己的入口链接，通过 Access 认证后直接进入学习页。入口链接（按 `webstudy/__init__.py` 的 `USERS` 生成）：

- `https://anki.aizhi.app/xct`
- `https://anki.aizhi.app/lcl`

## 安装和启动

需要 Linux 或 macOS、[`just`](https://just.systems/)、[`uv`](https://docs.astral.sh/uv/) 和本仓库要求的 Rust 工具链。先在仓库根目录运行 `just wheels`，再切换到扩展目录，使用 `out/wheels/` 里的本仓库 `anki-*.whl`（**不是** `aqt-*.whl` 或 PyPI 上的 Anki）：

```sh
cd extensions/web-study
just setup '../../out/wheels/anki-<实际文件名>.whl'
just serve 0.0.0.0 8765
```

`serve` 使用单 worker 并独占锁定 `extensions/web-study/data/`，与启动命令的工作目录和地址无关。

## 谁能进入

入口链接接受两类请求，其余一律返回 401：

1. **经 Cloudflare Tunnel 进来的请求**：携带 Access 签发的 JWT（`Cf-Access-Jwt-Assertion` 头），并用 `data/access.json` 里配置的公钥完成验签（签名、受众 AUD、签发方、有效期）。只有经本机回环进来的代理头会被信任，因此 `https` 即代表来自隧道；局域网直连无法伪装。
2. **本机发起的命令**：`import-file` 等本机运维操作直接信任回环来源，供词表导入使用。

`data/access.json` 示例（`aud` 是 Access 应用概览页的 AUD Tag）：

```json
{ "team": "vincentgroup", "aud": "<AUD Tag>", "domain": "anki.aizhi.app" }
```

注意：能通过 Access 认证的人都可以打开任一入口链接，Access 策略请限制到家庭成员。打开另一人的入口链接会切换到对方的账号。

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

`--user` 选择词义选择和确认操作归属的账号。若服务只监听指定局域网地址或端口不是 `8765`，追加 `--url http://<服务机局域网IP>:<端口>`。如果确认后某个账号投递失败，页面会标出待重试账号；它下次取题或服务重启时会重试，也可以重新确认。其他账号的复习记录不回滚。

## 验收和备份

1. 打开 `https://anki.aizhi.app/xct`，通过 Access 认证后应直接看到学习页和账号 XCT。
2. 上传一条带 ID 的词。XCT 翻卡并评「简单」，LCL 应仍能看到这张新卡。通过 `inbox/` 修改同一 ID 的释义，LCL 应能看到新释义；XCT 不应因此立即重学，等卡片重新到期时会用新释义。
3. 直接访问 `http://<服务机局域网IP>:8765`（未走隧道）应被拒绝；在本机运行 `just import-file` 应正常工作。
4. 停服重启，确认账号隔离、词条和进度仍在。停服备份整个 `extensions/web-study/data/`。

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

扩展测试验证真实 Anki 取题、评分、词条更新不改卡片身份、投递中断重试、入口链接的信任边界，以及网页与命令行走同一导入流程。测试只用临时数据，不接触 `data/`。
