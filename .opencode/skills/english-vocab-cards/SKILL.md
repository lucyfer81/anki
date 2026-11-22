---
name: 英语单词卡导入
description: Use when 需要把沪教版小学英语词汇表 md（一年级上/下、二年级上/下、三年级上）制作成单词闪卡，或导入 web-study 学习服务（XCT/LCL 账号）时。触发场景：解析核心词汇表、生成 Anki .apkg、生成/导入 inbox CSV、给 xct/lcl 加单词、更新词库、词义合并。关键词：单词卡、闪卡、词汇表、web-study、catalog、import-file。
---

# 小学英语词表 → 闪卡/web-study 导入

## 数据流与关键路径

```
词汇表 md（vault）──build_apkg.py──▶ 桌面 Anki .apkg（双向卡，6 个文件）
        └──make_webstudy_csv.py──▶ inbox/小学英语词汇表.csv ──import-file──▶ web-study 服务 ──▶ xct + lcl
```

| 东西 | 路径 |
|---|---|
| 源词表 md | `/home/ubuntu/working-vault/30-Resources/小学资源/英语单词/`（仅 5 个 `*核心词汇表.md`；`词汇与句型*.md` 不导入） |
| 生成脚本+测试 | 同目录 `anki/`：`build_apkg.py`、`make_webstudy_csv.py`、`test_build_apkg.py`、`test_make_webstudy_csv.py` |
| 服务仓库 | `/home/ubuntu/code-universe/anki`（分支 feat/web-study），扩展在 `extensions/web-study/` |
| 服务数据 | `extensions/web-study/data/`（catalog.sqlite3 共享词库；`users/{xct,lcl}/collection.anki2` 各自进度） |
| 导入通道 | `extensions/web-study/inbox/*.csv` → `webstudy.cli import-file`（对运行中的服务走回环 HTTP） |

期望基线：897 源行 → 学期内去重 869 → 跨学期合并 **702 词条**（核心 **271**）；catalog 版本 2、xct/lcl 已投递。源表列为 `序号|单元|单词|读音|词义`，加粗=核心词，单元取值 `Starter`/`Unit 1..10`。

## 标准流程：词表 md → 两个账号

```sh
# 1. 跑测试（解析/合并逻辑有回归闸门：精确计数断言）
cd /home/ubuntu/code-universe/anki && .venv/bin/python "/home/ubuntu/working-vault/30-Resources/小学资源/英语单词/anki/test_make_webstudy_csv.py"
# 2. 生成 CSV（写入 extensions/web-study/inbox/小学英语词汇表.csv）
.venv/bin/python "/home/ubuntu/working-vault/30-Resources/小学资源/英语单词/anki/make_webstudy_csv.py"
# 3. 导入（一次导入对 xct+lcl 同时生效；不要跑两次）
cd extensions/web-study && printf 'y\n' | ../../.venv/bin/python -m webstudy.cli import-file inbox/小学英语词汇表.csv --user xct
# 4. 验证（见下）
```

## 硬性规则（违反 = 伤害真实用户）

- **服务运行中绝不写 `data/`**：不直接改 catalog.sqlite3 / users/*/collection.anki2，不删 serve.lock。改词只有 import-file 一条路。
- **CSV 每行必须带稳定 `id`**（格式 `y1a|y1b|y2a|y2b|y3a-u{00..10}-{slug}`，Starter=u00）。id 决定 upsert 身份：重导=原位更新；**换 id = 新增重复词条且删不掉**（catalog 无删除）。无 id 且同名的行走逐行交互选择，非交互 shell 直接中止。
- **`printf 'y\n'` 只在全部行带 id 时安全**；有歧义行时 EOF 中止是保护，禁止 `yes |` 盲喂。
- 词义合并语义固定：学期内 dedupe → 跨学期 merge_global；义项按「；」分节去重合并；任一单元/学期核心即核心 → 词义前缀 `★`。**修改解析/合并逻辑必须先写失败测试**（曾靠此抓到 water/player 重复义项 bug）。
- 同一文件字节级重提是指纹 no-op（提示「不会重放修改」），**不是失败**，不要反复重试。

## 常见坑

- `uv`/`just` 不在非交互 PATH：直接用 `/home/ubuntu/code-universe/anki/.venv/bin/python`（服务同一 venv），且 cwd 必须在 `extensions/web-study/`（否则 `webstudy` 包不可导入）。缺 `--user` 会交互问账号。
- `import-file` 是 HTTP 客户端：服务没跑（`pgrep -af 'webstudy.cli serve'`，端口 8765）就什么都导不了。启动（照抄现役进程方式）：
  `cd extensions/web-study && setsid nohup ../../.venv/bin/python -m webstudy.cli serve --host 0.0.0.0 --port 8765 >> data/serve.log 2>&1 < /dev/null &`
- preview 与 confirm 之间词库版本变化 → StalePreview：重新 preview 再确认即可。
- 确认输出「仍待投递的账号」= 投递失败：下次取题或重启自动重试，无需回滚。

## 验证（导入后必做）

```sh
.venv/bin/python - <<'EOF'
import shutil, sqlite3, tempfile
from pathlib import Path
cat = sqlite3.connect("file:extensions/web-study/data/catalog.sqlite3?mode=ro", uri=True)
print("词条:", cat.execute("select count(*) from words").fetchone()[0],
      "版本:", cat.execute("select version from catalog_state").fetchone()[0],
      "投递:", cat.execute("select * from delivered").fetchall())
tmp = Path(tempfile.mkdtemp(dir="/tmp/opencode"))   # 集合文件被服务锁住：快照副本只读验证
for user in ("xct", "lcl"):
    for suffix in ("", "-wal", "-shm"):
        src = Path(f"extensions/web-study/data/users/{user}/collection.anki2{suffix}")
        if src.exists(): shutil.copy(src, tmp / f"{user}.anki2{suffix}")
    db = sqlite3.connect(str(tmp / f"{user}.anki2"))
    print(user, "notes:", db.execute("select count(*) from notes").fetchone()[0])
    db.close()
EOF
```

期望：词条 702、两账号 delivered 版本一致、每账号 notes=702、卡片在 `Web Study` 牌组、271 条含 ★。kids 端验收：刷新 `https://anki.aizhi.app/xct`、`/lcl` 看到新词。

## 桌面 Anki .apkg 线（偶发需求）

`.venv/bin/python <vault>/anki/build_apkg.py`：生成 `英语单词/anki/` 下 6 个 apkg（全部+每学期；双向卡；确定性 guid）。产物自验证 notes=869×2 卡。这套卡与 web 服务互不相通，桌面版导入 apkg 时才用。

## 新增年级（如四年级）

1. vault 放入 `四年级上-核心词汇表.md`（同表结构）；2. `build_apkg.py` 的 `SEMESTERS`、`make_webstudy_csv.py` 的 `SEMESTER_CODE` 各加一项（代码 `y4a`/`y4b`）；3. 两个测试文件的期望计数按新真实值更新；4. 跑测试→生成→导入→验证。词库是两账号共享的扁平列表，无年级字段——年级顺序只靠学期代码排序（新卡按导入顺序出题）。
