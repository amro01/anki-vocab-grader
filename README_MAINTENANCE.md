# Anki 词库标签与提示系统：技术总结与维护文档

**对应脚本**：`process_deck.py`
**当前配置**：`LONGMAN_CORE_TARGETS = {"W1", "S1"}`
**当前产物**：`2026-10-27_tagged.colpkg`
**基线牌组**：`2026-10-27.colpkg`（546 notes / 546 cards）

本文档记录当前实现的技术架构、关键坑位、验证方法与后续维护流程。所有统计均来自本目录当前脚本与当前牌组的实际运行结果。

---

## 0. 快速使用

```bash
cd /home/deep/enwords_anki
python3 process_deck.py
```

默认读取：

- `2026-10-27.colpkg`
- `LONGMAN3000.txt`
- `1600.txt`
- `3500.txt`

默认输出：

- `2026-10-27_tagged.colpkg`

也可显式指定：

```bash
python3 process_deck.py \
  --deck 2026-10-27.colpkg \
  --longman LONGMAN3000.txt \
  --zk16k 1600.txt \
  --gk35k 3500.txt \
  --out 2026-10-27_tagged.colpkg
```

脚本不会覆盖输入文件；输出路径与输入路径相同时会直接报错。

---

## 1. 架构与词库分工定位

### 1.1 三层词库的职责

| 层级 | 词库 | 标签 | 背面视觉提示 | 定位 |
|---|---|---|---|---|
| 应试核心 | `1600.txt` | `zk16k` | 顶部醒目红字：`中小学1600词（必须掌握听说读写）` | 中考高频，要求会拼写、会输出，是最高优先级提示 |
| 母语高频兜底 | `LONGMAN3000.txt` | `lm3k_core` | 顶部灰色小字：`Longman Core (实际命中频级)` | 母语语料中的 S1/W1 高频词；补足 1600 词库可能漏掉的词 |
| 高中认知 | `3500.txt` | `gk35k` | 无提示 | 高考认知词汇，强调“认识/阅读”，不额外抢占卡片视觉 |

设计意图：

- **红字是“必须会”**：命中中考 1600 才显示，且要求听说读写。
- **灰字是“母语高频/兜底”**：命中配置的 Longman 频级才显示，并标出实际级别，例如 `S1`、`W1`、`S1, W1`。
- **3500 只打标签**：提醒该词属于高考认知范围，但不把每张卡都变成提示墙。
- 灰字始终在红字上方；两者都命中时先灰后红，再拼接原正文。

### 1.2 实际命中示例

以下均为当前牌组真实结果：

| 单词 | Longman 频级 | 1600 | 3500 | 最终标签 | 背面提示 |
|---|---|---|---|---|---|
| `bag` | `S1, W2` | 否 | 是 | `lm3k_core gk35k` | 灰字 `Longman Core (S1)`，无红字 |
| `apple` | `S2, W3` | 是 | 是 | `zk16k gk35k` | 红字，无灰字 |
| `children` | 无精确词条；`child` 为 `S1, W1` | 否 | 否 | `lm3k_core zk16k gk35k` | 灰字 `S1, W1` + 红字（经词形还原） |
| `socks` | 无精确词条；`sock` 为 `S3` | 否 | 否 | `zk16k gk35k` | 红字（经词形还原；S3 不在默认 Core 目标） |
| `swam` | 无精确词条；`swim` 为 `S3` | 否 | 否 | `zk16k gk35k` | 红字（经词形还原；S3 不在默认 Core 目标） |
| `of course` | `S1, W1` | 否 | 否 | `lm3k_core` | 灰字 `S1, W1` |
| `ache` | 无 | 否 | 是 | `gk35k` | 无提示（仅高考认知） |
| `giraffe` | 无 | 否 | 是 | `gk35k` | 无提示（仅高考认知） |
| `peanuts` | 无；`peanut` 不在三个词表 | 否 | 否 | 无 | 无（词典本身无 `peanut`，无法还原） |

`bag` 是理解分工的关键例子：

1. 它不在 `1600.txt` 的解析结果中，因此**不会**得到中考红字。
2. 它在 `3500.txt` 中，得到 `gk35k`，属于高考认知词。
3. 它在 Longman 中为 `S1, W2`；默认目标集 `{"W1", "S1"}` 命中 `S1`，因此得到 `lm3k_core` 和灰字 `Longman Core (S1)`。

这就是“Longman 高频兜底”：即使 1600 词库漏掉了，母语高频层仍会把它标记出来。

### 1.3 输入与输出

| 文件 | 角色 | 说明 |
|---|---|---|
| `2026-10-27.colpkg` | 输入牌组 | 现代 `.colpkg`，真实数据库在 `collection.anki21b` |
| `LONGMAN3000.txt` | Longman 词库 | archive.org djvu OCR 文本，有字符误识、粘连、跨行 |
| `1600.txt` | 中考词库 | 每行 `词汇 [音标] ...`，含目录行、括号变形 |
| `3500.txt` | 高考词库 | 同上 |
| `2026-10-27_tagged.colpkg` | 输出牌组 | 已打标签并注入分层提示的新包 |
| `2026-10-27.colpkg`（原文件） | 备份 | 脚本从不原地修改输入 |

### 1.4 执行流

1. 解析命令行参数，检查输入文件与输出路径。
2. 解析 `LONGMAN3000.txt`，得到 `{headword: {S1, S2, S3, W1, W2, W3}}`。
3. 解析 `1600.txt`、`3500.txt`，得到两个小写词集合。
4. 用 `zipfile` 将 colpkg 解压到临时目录。
5. 优先打开 `collection.anki21b`：
   - zstd 解压为临时 SQLite；
   - 若不存在则回退到 `collection.anki21` / `collection.anki2`。
6. 读取 `notes` 表，对每张卡：
   - 清洗正面、精确匹配 + 词形还原；
   - 清理旧的顶部提示；
   - 按“灰字 → 红字 → 原正文”重建背面；
   - 重建受管标签，保留用户标签；
   - 仅在内容/标签变化时更新 `mod`、`usn`。
7. 提交事务，关闭数据库。
8. 将 SQLite 重新 zstd 压缩回 `collection.anki21b`，把原包所有文件重新打包为输出 colpkg。
9. 重新打开输出包，执行 SQLite 完整性检查、标签计数、提示计数与旧标记残留检查；打印统计并清理临时目录。

---

## 2. 关键底层避坑记录

### 2.1 Anki 现代存储机制坑

#### `collection.anki21b` 与 `collection.anki2` 不是一回事

当前 colpkg 解压后包含：

```text
meta
collection.anki2       # 占位库，只有 1 条“请更新至 Anki 的最新版本...”提示
collection.anki21b     # zstd 压缩的真实 SQLite，546 条笔记
media
```

如果按直觉优先读 `collection.anki2`，会以为牌组只有 1 张卡，处理结果完全错误。脚本因此在 `_open_collection()` 中优先处理 `collection.anki21b`。

`collection.anki21b` 的文件头：

```text
28 b5 2f fd
```

这是 zstd 帧魔数。处理方式：

- 用 `zstandard.ZstdDecompressor().copy_stream(...)` 解压到临时文件；
- 校验解压后前 16 字节为 `SQLite format 3\0`；
- 修改完成后用 `ZstdCompressor(level=3)` 压回；
- 只替换 `collection.anki21b`，保留原始 `meta`、`collection.anki2`、`media`。

注意：

- 不要把解压后的临时 SQLite 文件打进输出 zip；脚本把工作数据库放在独立临时目录，只打包 `pkg` 目录。
- `meta` 保持原样（版本 3），否则 Anki 可能不识别包格式。
- 不要删除 `collection.anki2` 占位库；现代 Anki 会优先读 `anki21b`，保留它可维持包结构兼容性。

#### 必须注册 `unicase` collation

Anki 的 SQLite schema 使用了自定义排序规则 `unicase`。Python 标准库 `sqlite3` 不认识它，直接执行下面这类语句会报错：

```text
sqlite3.OperationalError: no such collation sequence: unicase
```

实际踩坑点：在 `PRAGMA integrity_check` 时触发。Anki schema 中 `tags.tag`、`decks.name`、`notetypes.name`、`deck_config.name` 等列使用 `COLLATE unicase`；完整性检查会遍历这些表/索引，因此必须注册该排序规则。

解决方式：

```python
def _unicase(left: str, right: str) -> int:
    left, right = left.casefold(), right.casefold()
    return (left > right) - (left < right)

def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.create_collation("unicase", _unicase)
    return con
```

所有连接——包括更新数据库和输出校验——都必须通过 `_connect()`，不能直接 `sqlite3.connect()`。当前脚本只修改 `tags`、`flds`、`mod`、`usn`，不修改 `sfld`/`csum`，因此不会触碰排序键本身；但完整性检查仍需要该 collation。

### 2.2 djvu OCR 噪点清洗坑

`LONGMAN3000.txt` 不是结构化词表，而是档案扫描后的 djvu 文本。主要噪声：

| 类型 | 例子 | 处理 |
|---|---|---|
| 词条与词性粘连 | `minepron`、`acidnW3`、`leadvS1`、`aimvS2` | 从右往左找最长词条前缀，尾部必须是合法词性/频度序列 |
| 词性和词条被合并 | `artnS1,W1`、`warnS2`、`lookvSI.WI` | 最长前缀 + 剥离被吸收的 `n`/`v` |
| 频度标记误识 | `Wl`、`WI`、`VV1`、`SI` | 只匹配大写 `S/W`，字符类兼容 `1/2/3/l/L/I/!` 和竖线；`VV1` 先归一化为 `W1` |
| 词性 OCR 变体 | `ad]`、`ad/`、`ad}`、`moda/`、`pran`、`cony`、`deferm/ner` | 词性别名表 + 含斜杠、右方括号、右花括号、竖线的 token 直接视为词性尾巴 |
| 词条跨行 | `all determiner,` / `predeterminer, pron` / `SI, Wl` | 状态机把后续词性/频度行并入当前词条 |
| 页眉/页码 | `LONGMAN COMMUNICATION 3000`、`10`、`11` | 跳过含 `LONGMAN` 的行和纯数字行 |
| 词性与单词重名 | `article`、`number` 既是词性也是词条 | 行首遇到 `article/number` 时优先当词条；非行首才当词性 |
| 词条顺序/列错乱 | OCR 不保证严格字母序；偶发跨列插入 | 不做版面重建，只按行解析；错误词条最多导致漏匹配，不阻塞整体流程 |
| 字母丢失 | `ce cream`（ice cream）、`agenSI.WI`（age/agency） | 无法完全恢复；精确匹配 + 词形还原兜底，必要时人工修正源词表 |

状态机的核心逻辑在：

- `_is_tail_token()`：判断 token 是否属于词性/频度尾巴；
- `_attached_cut()`：切分 `acidnW3` 这类粘连 token；
- `_split_headword()`：返回 `(词条, 尾巴)`；
- `_parse_longman_entries()`：逐行状态机，遇到新词条则 flush 上一条；
- `_extract_levels()`：从尾巴中提取 S/W 频级；
- `_strip_absorbed_pos()`：处理 `earn SI` 实际为 `ear n S1` 的情况。

当前解析结果：

- 词条总数：3572；
- 去重 headword：3146；
- `S1/W1` 目标词条：1134；
- 每个频级约 990–999 个标记，符合 Longman 3000 的分频结构。

**重要**：`_extract_levels()` 使用大写 `[SW]` 标记，而不是大小写不敏感匹配。否则：

- `bowl` 末尾的 `wl` 会被误判为 `W1`；
- `determiners!` 末尾的 `s!` 会被误判为 `S1`。

### 2.3 词元化与词形还原（Lemmatization）

#### 为什么必须做

精确字符串匹配会漏掉大量变形词：

- be 动词：`am`、`is`、`are`、`was`、`were`、`been`、`being`；
- 不规则过去式：`ate → eat`、`bought → buy`、`went → go`、`wrote → write`；
- 不规则复数：`children → child`；
- 规则复数：`shoes → shoe`、`socks → sock`、`noodles → noodle`；
- 缩写：`that's → that`、`they're → they`、`won't → will`、`she's → she`。

如果直接拿 `children` 查词表，三个词库都可能查不到；还原成 `child` 后即可命中 Longman `S1/W1`、1600 和 3500。

#### 当前实现策略

没有依赖外部 NLP 模型（当前环境 NLTK 缺少 WordNet 数据、spaCy 缺少英文模型），而是实现了一个自包含、保守的英文词形还原器：

1. **不规则动词表**：`_IRREGULAR_VERBS`，base → 过去式/过去分词/第三人称/ing 形式。
2. **不规则名词表**：`_IRREGULAR_NOUNS`，处理 `children/men/women/feet/teeth/mice/leaves/lives` 等。
3. **不规则比较级**：`_IRREGULAR_ADJ`，如 `better → good/well`。
4. **缩写表**：`_CONTRACTIONS` + `_contraction_candidates()`，处理 `'s/'re/'ve/'ll/'d/'m/n't` 及 `won't/can't/cannot`。
5. **规则后缀**：`_regular_candidates()`，处理：
   - 复数/第三人称：`-s/-ies/-oes/-ses/-xes/-ches/-shes`；
   - 过去式：`-ed/-ied`，含双写辅音还原、补 `e`；
   - 现在分词：`-ing`，含 `ying → ie`、双写辅音还原、补 `e`。
6. **保守过滤**：多词短语和连字符词不做还原；`news`、`means`、`series`、`species`、`clothes` 不做复数剥离；`morning`、`evening` 不做 `-ing` 还原。

#### 匹配优先级

- **1600/3500**：先精确匹配；不中再依次尝试词元候选，命中即打标签。
- **Longman Core**：
  1. 若原形本身命中的频级与 `LONGMAN_CORE_TARGETS` 有交集，优先使用原形频级；
  2. 否则尝试词元候选，使用第一个命中目标频级的词元；
  3. 两者都没有目标频级则不显示灰字。

这个优先级避免了两个问题：

- `left` 作为方向词，原形本身就有 `S1/W1`，不会强行还原成 `leave`；
- `lost` 的 Longman 词条是 `S2/W3`，默认目标 `S1/W1` 不命中，但还原成 `lose` 后可命中 `S1/W1`，因此能正确显示灰字。

#### 当前效果

- 词形还原命中：**34 个唯一原词形 / 34 张卡**。
- 典型命中：`am/is/are`、`ate`、`bought`、`children`、`does`、`goes`、`saw`、`felt`、`lost`、`found`、`shoes`、`socks`、`swam`、`that's`、`won't` 等。
- 不会凭空创造词条：例如 `peanuts` 可还原为 `peanut`，但三个词表都没有 `peanut`，因此仍不打标签。

### 2.4 幂等性与 HTML 注入设计

#### 旧提示清理

每次更新前，`_strip_injected_hints()` 会从背面**顶部**循环剥离所有已知提示块：

- 旧版红字：`LongMan 3000 W1（必须掌握听说读写）`
- 当前灰字：`Longman Core (S1, W1)` 等
- 当前红字：`中小学1600词（必须掌握听说读写）`

`HINT_STRIP_RE` 只匹配以这些文案为内容的 `<div>`，并从字段开头连续清理。这样：

- 重复运行不会叠加提示；
- 从旧版本包升级时，旧红字会被清除并替换成新分层提示；
- 配置从 `{"W1","S1"}` 改成 `{"W2"}` 时，旧的 `S1/W1` 灰字会被清除，不会残留；
- 原正文内容不被修改（迁移测试中逐条比较，正文完全一致）。

#### 重建顺序

```text
[灰字（若核心命中）] + [红字（若1600命中）] + 原正文
```

- 只命中灰字：灰字在最顶；
- 只命中红字：红字在最顶；
- 两者都命中：灰字在上、红字紧随其后；
- 均未命中：不添加提示（同时清掉旧提示）。

#### 标签幂等

`_merge_tags()` 的规则：

1. 保留用户原有标签顺序；
2. 删除所有受管标签：`lm3kw1`、`lm3k_core`、`zk16k`、`gk35k`；
3. 按固定顺序重新追加本次命中的：`lm3k_core` → `zk16k` → `gk35k`；
4. 去重，输出 `" tag1 tag2 "` 格式。

这保证：

- 旧标签 `lm3kw1` 一定被清除；
- 标签顺序稳定，不会因为上次运行顺序不同而漂移；
- 第二次运行不会产生标签变更，`updated = 0`。

#### 写入与验证

- 只有 `tags` 或 `flds` 真正变化的笔记才更新 `mod`/`usn`；
- `mod` 为当前秒级时间戳，`usn = -1`；
- 重新打开输出包，验证：
  - SQLite `integrity_check = ok`；
  - `lm3kw1` 残留为 0；
  - 旧 `LongMan 3000 W1` 提示残留为 0；
  - 灰字/红字数量与本次插入计数一致；
  - 无重复提示；
  - 标签计数一致。

---

## 3. 日常维护与参数调优

### 3.1 调整 Longman Core 频级

修改 `process_deck.py` 第 **44** 行：

```python
LONGMAN_CORE_TARGETS = {"W1", "S1"}
```

示例：

```python
# 只保留 W1
LONGMAN_CORE_TARGETS = {"W1"}

# 默认：W1 + S1
LONGMAN_CORE_TARGETS = {"W1", "S1"}

# 加入 W2
LONGMAN_CORE_TARGETS = {"W1", "S1", "W2"}

# 加入 S2
LONGMAN_CORE_TARGETS = {"W1", "S1", "S2"}

# 只看 W2
LONGMAN_CORE_TARGETS = {"W2"}
```

规则：

- 合法值只有 `S1/S2/S3/W1/W2/W3`；
- 小写会自动转大写；
- 非法值会在启动时报错；
- 灰字会显示“实际命中的目标频级”，按 `LEVEL_ORDER` 排序：
  - 命中 `S1` 和 `W1` → `Longman Core (S1, W1)`
  - 只命中 `S1` → `Longman Core (S1)`
  - 只命中 `W1` → `Longman Core (W1)`
- 修改后直接重跑脚本即可；旧的灰字和 `lm3k_core` 会被清理并按新配置重建。

实测参考：

- `{"W1","S1"}`：Core 词条 1134，灰字 326 张；
- `{"W2"}`：Core 词条 961，灰字 113 张；
- `{"W2"}` 运行在旧输出上时，旧 `S1/W1` 灰字全部被清除，只保留 `W2`。

如果未来增加新的频级（例如 Longman 的其它标记），需要同步更新：

- `LEVEL_ORDER`（第 **52** 行），用于灰字显示顺序；
- `_extract_levels()` 的字符类与归一化规则；
- `main()` 中对 `LONGMAN_CORE_TARGETS` 的合法性校验集合。

### 3.2 修改红字/灰字样式

位置：

- 灰字模板：`GRAY_HINT_TEMPLATE`，第 **53–56** 行；
- 红字模板：`RED_HINT`，第 **57–60** 行；
- 旧提示标记：`OLD_HINT_MARK`，第 **61** 行。

修改样式时，**必须同步检查三个正则**：

| 常量 | 位置 | 作用 |
|---|---|---|
| `HINT_STRIP_RE` | 第 **738** 行附近 | 清理顶部旧提示；若提示文案变了，必须同步更新 |
| `GRAY_HINT_RE` | 第 **746** 行附近 | 输出校验时识别顶部灰字；样式/文案变了必须同步更新 |
| `RED_HINT_RE` | 第 **750** 行附近 | 由 `RED_HINT` 自动 `re.escape` 生成，通常只需改 `RED_HINT` |

最容易踩的坑：**只改了 HTML 模板，没改清洗/校验正则**。后果：

- 旧提示清不掉，出现重复；
- 或者输出明明有提示，校验却认为没有，脚本报“输出校验失败”。

修改检查清单：

1. 改 `GRAY_HINT_TEMPLATE` / `RED_HINT`；
2. 若文案或结构变化，改 `HINT_STRIP_RE`；
3. 若灰字样式变化，改 `GRAY_HINT_RE`；
4. 若旧版红字也要兼容清理，保留 `LongMan 3000 W1（必须掌握听说读写）` 分支至少一个版本周期；
5. 重跑脚本，确认：
   - 旧标记残留 0；
   - 无重复提示；
   - 灰/红计数与标签计数一致；
   - 二次运行 `updated = 0`。

若新增第三种提示（例如“高考扩展”），需要同时修改：

- `update_notes()` 中的 `hint_prefix` 拼接顺序；
- `HINT_STRIP_RE`；
- `_hint_flags()` / `_verify_package()`；
- 控制台统计项。

### 3.3 后续追加新卡片 / 更新词库的 SOP

#### 标准流程

1. **备份 Anki 集合**
   - Anki 菜单 `File → Export` 导出当前集合为 `.colpkg`；
   - 同时备份 Anki 配置目录，便于回滚；
   - 脚本输入必须是导出的 `.colpkg`（或包含 `collection.anki21b/anki21/anki2` 的压缩包），不是裸 collection 文件。

2. **把新包放到项目目录**
   - 例如 `2026-11-01.colpkg`；
   - 不要覆盖原始输入包，除非你确认不再需要旧版本。

3. **确认词库文件**
   - `LONGMAN3000.txt`、`1600.txt`、`3500.txt` 如无更新则无需改动；
   - 若更新了词表，脚本会自动重新计算标签与提示。

4. **运行脚本**

   ```bash
   python3 process_deck.py \
     --deck 2026-11-01.colpkg \
     --out 2026-11-01_tagged.colpkg
   ```

   或使用默认文件名：

   ```bash
   python3 process_deck.py
   ```

5. **检查控制台**
   - `输出包校验通过`；
   - `旧 lm3kw1 标签残留: 0`；
   - `旧 LongMan 3000 W1 提示残留: 0`；
   - 灰/红/标签计数与预期接近。

6. **在 Anki 中导入输出包**
   - `File → Import` 选择 `2026-11-01_tagged.colpkg`；
   - `.colpkg` 是完整集合包，Anki 可能提示替换/合并当前集合。**导入生产集合前先备份**；
   - 更安全的做法是在新的 Anki 配置/临时用户中导入验证，再迁移到主配置。

7. **目视抽检**
   - `bag`：应只有灰字 `Longman Core (S1)` 和 `gk35k`；
   - `apple`：应只有红字和 `zk16k gk35k`；
   - `children`：应有灰字 `S1, W1` + 红字 + 三个标签；
   - `socks` / `swam`：应只有红字 + `zk16k gk35k`；
   - `ache` / `giraffe`：应只有 `gk35k`，无提示；
   - 任意卡片：提示只出现一次，灰在前红在后。

#### 增量更新原则

- 新卡片导出后直接重跑即可；脚本不依赖上一次运行的标签/提示状态。
- 若在已打标签的包上重跑，旧的受管标签和顶部提示会被清理并按当前配置重建；已测试幂等（第二次 `updated = 0`）。
- 若只修改 `LONGMAN_CORE_TARGETS`，建议在**原始包**或**当前 tagged 包**上运行均可；两者最终内容应一致（迁移测试已验证逐条正文/标签一致）。
- 输出路径不能与输入路径相同。

### 3.4 常见问题排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 处理结果只有 1 张卡 | 读到了 `collection.anki2` 占位库 | 确认包内有 `collection.anki21b`；脚本应优先解压它 |
| `no such collation sequence: unicase` | 直接用 `sqlite3.connect()`，未注册 Anki 自定义排序规则 | 所有连接走 `_connect()`，注册 `unicase` |
| 标签/提示重复 | 清洗正则未匹配旧模板 | 检查 `HINT_STRIP_RE` 是否覆盖旧文案/样式 |
| 明明命中了却无提示 | `LONGMAN_CORE_TARGETS` 不含该词的实际频级 | 查看 Longman 频级，按需加入目标集合 |
| 输出校验失败 `gray_hints` 不一致 | 改 HTML 后未同步 `GRAY_HINT_RE` / `HINT_STRIP_RE` | 同步模板与校验正则后重跑 |
| 旧 `lm3kw1` 仍存在 | 手工导入了旧包，或用了旧脚本 | 用当前脚本重跑；`MANAGED_TAGS` 会清除旧标签 |
| 变形词仍漏标 | 词元不在三个词表中，或不在不规则映射里 | 先确认词表是否含词元；必要时扩充 `_IRREGULAR_VERBS/_IRREGULAR_NOUNS` 或源词表 |
| Anki 不识别输出包 | `collection.anki21b` 未正确 zstd 压缩，或 `meta`/`anki2` 被破坏 | 检查文件头 `28 b5 2f fd`，保留 `meta`、`collection.anki2`、`media` |
| 二次运行 `updated` 不为 0 | 标签顺序/提示模板不稳定，或校验正则不一致 | 检查 `_merge_tags()` 固定顺序、`HINT_STRIP_RE`；当前实现应幂等 |

#### 当前回归基线

| 指标 | 数值 |
|---|---:|
| Anki notes / cards | 546 / 546 |
| `zk16k` 红字提示 | 496 |
| `lm3k_core` 灰字提示 | 326 |
| 同时命中两者 | 316 |
| `zk16k` 标签 | 496 |
| `gk35k` 标签 | 521 |
| `lm3k_core` 标签 | 326 |
| 旧 `lm3kw1` 残留 | 0 |
| 旧 `LongMan 3000 W1` 残留 | 0 |
| 词形还原命中 | 34 个唯一原词形 / 34 张卡 |
| 无任何提示的卡 | 40（19 张仅 gk35k + 21 张无标签） |
| 重复运行更新数 | 0（幂等） |

如果未来数据变化导致数量大幅偏离，优先检查：

1. `collection.anki21b` 是否被正确读取；
2. `LONGMAN_CORE_TARGETS` 是否被意外修改；
3. 词表文件是否被替换；
4. 词形还原映射是否被误改；
5. 提示清洗/校验正则是否与模板一致。

### 3.5 关键代码索引

| 内容 | 位置（当前版本） |
|---|---|
| 可调配置 `LONGMAN_CORE_TARGETS` | 第 44 行 |
| 标签常量 / 受管标签 | 第 46–50 行 |
| 频级显示顺序 `LEVEL_ORDER` | 第 52 行 |
| 灰字/红字模板 | 第 53–60 行 |
| `_extract_levels()` | 第 189 行 |
| `_parse_longman_entries()` 状态机 | 第 200 行 |
| `parse_longman_levels()` | 第 243 行 |
| 不规则动词表 | 第 255 行 |
| 不规则名词表 | 第 396 行 |
| 不规则比较级表 | 第 445 行 |
| 缩写表 | 第 486 行 |
| `_regular_candidates()` 规则后缀 | 第 520 行 |
| `lemma_candidates()` 词元入口 | 第 604 行 |
| `_list_hit()` / `_core_hit()` 匹配策略 | 第 631 / 641 行 |
| `parse_vocab()` | 第 715 行 |
| `HINT_STRIP_RE` / `GRAY_HINT_RE` / `RED_HINT_RE` | 第 738 / 746 / 750 行 |
| `_strip_injected_hints()` | 第 758 行 |
| `_hint_flags()` | 第 769 行 |
| `_clean_front()` | 第 783 行 |
| `_merge_tags()` | 第 791 行 |
| `_unicase()` / `_connect()` | 第 810 行起 |
| `_open_collection()` | 第 822 行 |
| `update_notes()` | 第 844 行 |
| `_pack()` | 第 916 行 |
| `_verify_package()` | 第 926 行 |
| `main()` | 第 997 行 |

> 行号以当前文件为准；修改后建议用 `grep -n` 复核，避免文档行号漂移。

---

## 4. 已知限制与后续改进

1. **OCR 无法完全恢复**
   - 例：`ce cream` 缺首字母 `i`；`agenSI.WI` 可能对应 `age`/`agency`；
   - 解析器尽量容错，但源文本损坏严重时仍会漏匹配；必要时人工修正 `LONGMAN3000.txt`。

2. **词形还原是保守规则，不是完整 NLP**
   - 不规则表可继续扩充；
   - 规则后缀对 `-ing` 名词/形容词采用保守策略（`morning`/`evening` 例外）；
   - 多词短语只做精确匹配，不做逐词还原（避免把 `have a look` 误标成 `have`）。

3. **词表不存在则无法命中**
   - `peanuts` 能还原成 `peanut`，但三个词表都没有 `peanut`，所以不打标签；
   - 这不是脚本 bug，而是词典覆盖问题。

4. **统计口径**
   - 标签/提示统计以 `notes` 行为单位；当前牌组 1 note = 1 card，因此与“卡片数”一致；
   - 若未来某 notetype 生成多张卡，需要按 `cards` 关联统计，当前数字不能直接等同于渲染卡片数。

5. **Anki GUI 导入未在本环境验证**
   - 当前通过 SQLite 完整性、zip/zstd、逐条内容不变量、迁移与幂等测试；
   - 首次导入仍建议在备份或临时 Anki 配置中目视确认显示效果。

6. **`unicase` 是 Python 近似实现**
   - 使用 `casefold()` 近似 Anki Rust 侧排序规则；
   - 对当前英文标签/内容足够；若未来大量非 ASCII 内容或需要严格同步 Anki 内部索引语义，建议改用 Anki 后端/官方导入流程。

---

## 5. 本次升级变更摘要

- 旧版：仅解析 W1，统一红字 `LongMan 3000 W1`，标签 `lm3kw1`。
- 新版：
  - Longman 改为“频级集合”解析，支持任意 `LONGMAN_CORE_TARGETS` 组合；
  - 灰字显示实际命中频级，如 `S1`、`W1`、`S1, W1`；
  - 标签升级为 `lm3k_core`，并自动清理旧 `lm3kw1`；
  - 新增 1600 红字分层提示与 3500 纯标签；
  - 新增词形还原，覆盖 be 动词、常见不规则动词/复数、缩写和规则后缀；
  - 新增旧提示正则清洗、受管标签规范化、输出重读校验与幂等/迁移测试。

当前回归结果：

- 546 张卡全部处理完成；
- 496 张红字、326 张灰字、316 张两者同时命中；
- 34 个词形还原命中；
- 0 条旧标签/旧提示残留；
- 二次运行更新数 0。
