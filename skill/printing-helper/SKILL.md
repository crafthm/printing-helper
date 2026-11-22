---
name: printing-helper
description: >
  Print files through the user's configured printers. Use when the user says
  打印/打出/印出来/print, mentions 打印机/队列/网络打印机, or asks for print
  layout control: A4, margins normal/narrow, multiple images on one sheet
  (N-up), text/docx/pdf/image printing, printer discovery, or remembering
  printer details across sessions. Detects native print streams (PDF/PCL/PS/ZJS/
  host-based) and supports raw socket 9100 and CUPS/IPP.
allowed-tools: Bash, Read
---

# Printing Helper

把文件排版后交给用户已登记的打印机。脚本负责确定性工作;你负责理解意图、解析打印机别名、展示摘要并取得确认。

## 位置与数据

```bash
PH="$HOME/.claude/skills/printing-helper/scripts/ph.py"
```

| 内容 | 路径 |
|---|---|
| 打印机注册表(唯一事实来源) | `~/.local/share/printing-helper/printers.json` |
| 用户偏好 | `~/.local/share/printing-helper/preferences.json` |
| 作业产物 | `~/.local/share/printing-helper/jobs/<job_id>/{out.pdf,preview-1.png,spec.json}` |
| 审计日志 | `~/.local/share/printing-helper/jobs.jsonl` |

所有命令都支持 `--json`。默认纸张 A4。

## 先判流,再打印(重要)

打印机不是“都懂 PDF”。host-based 机型(HP LJ 1000/1018/1020/1022、Canon LBP、
Samsung ML 等)只认自己的原生流;9100 透明网桥会把 PDF/PCL 原样送进去,结果
是“连接成功但不出纸”。

先做离线检测(不打印):

```bash
python3 "$PH" printer detect <别名> --json
python3 "$PH" printer show <别名>            # 查看身份/首选流/历史
```

检测阶梯:CUPS 队列 + PPD → 型号知识库 → 网络能力 → 校准页。

- 有 CUPS 队列:优先复用队列(驱动链已就绪,最稳)。
- HP LJ 1020/1018/1022 系列:原生流是 **ZJS**,用 foo2zjs。
- 结果显示 `recommendation`;把依据和候选告诉用户。
- **不要用 PDF/PCL 去“试探” host-based 打印机。**

详见 `references/streams.md`。

## 铁律

1. **未经用户明确确认,绝不真正打印。** `plan`、`preview`、`--dry-run` 只排版不出纸;`send --yes` / `run --yes` 才会发送。
2. 用户没有说“直接打/不用确认”时:先 `plan`,把摘要(打印机、纸张、边距、方向、页数、份数、图片排法)给用户;得到同意后再 `send --job <id> --yes`。
3. 打印机指代不唯一时**不要猜**,列出候选让用户选。
4. 用户给出新的打印机信息(别名/IP/URI/队列)时,用 `printer add` 持久化;以后直接用别名调取。不要依赖对话记忆。
5. 不修改用户原文件;中间产物都留在作业目录。
6. CLI 退出码 7 = 需要确认。看到 7 时向用户复述摘要,确认后加 `--yes` 重跑。

## 标准流程

```bash
# 1. 看已登记打印机(需要时)
python3 "$PH" printer list --json

# 2. 解析用户说的打印机;"上次"/"默认" 也可直接传
python3 "$PH" printer resolve "网络打印机" --json

# 3. 判流(不打印):先复用 CUPS/PPD,再考虑校准
python3 "$PH" printer detect "办公室" --json

# 4. 新打印机:登记(会做 TCP 连通性探测,不出纸)
python3 "$PH" printer add --alias "办公室" \
  --uri socket://192.168.1.42:9100 --raw-format zjs --paper A4 --json
# 可选: --default 设为默认

# 5. 排版 + 生成预览(不打印)
python3 "$PH" plan 合同.docx 图1.png 图2.png \
  --printer "办公室" --paper A4 --margin narrow \
  --images nup --grid 2x2 --copies 1 --json
# 返回 spec.job_id / spec.pdf_path / spec.preview_png / spec.warnings

# 6. 用户确认后发送
python3 "$PH" send --job <job_id> --yes --json

# 7.(更省事的等价写法)排版+发送,但 --yes 才出纸
python3 "$PH" run 文件... --printer "办公室" --dry-run   # 只看 payload,不出纸
```

`plan` 的 JSON 里重点关注:`printer`、`paper`、`margin`、`estimated_pages`、`copies`、`outputs`、`fidelity`、`warnings`、`preview_png`。把 `preview_png` 路径告诉用户即可查看。

## 参数速查

| 需求 | 参数 |
|---|---|
| 边距 | `--margin normal` / `narrow` / `none` / `20,20,20,20`(mm,上右下左) |
| 纸张/方向 | `--paper A4\|Letter\|210x297mm` · `--orient auto\|portrait\|landscape` |
| 图片整张打印 | `--images one-per-page`(默认) |
| 多图拼一页 | `--images nup --grid auto` 或 `--grid 2x2` |
| 拼版间距/裁剪/文件名 | `--gap 5` · `--fill` · `--caption` |
| 份数/双面/质量 | `--copies N` · `--duplex` · `--quality draft\|normal\|high` |
| 页码/书签 | `--page-numbers` · `--no-bookmarks` |
| socket 直发格式 | `--raw-format pdf\|pcl\|pcl-color\|ps\|zjs` · `--pjl` |

## 自然语言映射

| 用户说 | 你怎么做 |
|---|---|
| “把这份合同打两份,窄边距” | `plan 合同.docx --margin narrow --copies 2`;确认后 `send --job <id> --yes` |
| “这 6 张图拼一张纸” | `--images nup --grid auto`(6 张会选 2×3) |
| “每张图单独一页” | `--images one-per-page` |
| “用上次那台打印机” | `resolve 上次` → 用返回的 slot 作为 `--printer` |
| “我办公室打印机是 192.168.1.42:9100” | `printer add --alias 办公室 --uri socket://192.168.1.42:9100`;询问是否设为默认 |
| “别真打,先给我看看” | 只执行 `plan`(或 `preview`),不发送 |
| “直接打,不用再问” | 可以 `run ... --yes`,但页数 > 20 / 份数 > 1 / 新打印机仍先复述摘要 |
| “docx 打出来版式不对” | 说明本机走 Mammoth,`fidelity: reduced`;建议先转 PDF |
| “打出来是乱码/空白/不出纸” | 先 `printer detect`;host-based 机型用 `zjs`(HP LJ 1020 系)或对应原生流,不要继续试 PDF |
| “这是 HP LaserJet 1020” | `detect` 会匹配 foo2zjs;优先用已建好的 CUPS 队列,或 `--raw-format zjs` |

## 打印机与传输

- **socket(9100)**:只是字节管道,必须是打印机能解析的流。默认取注册表的
  `preferred_stream`;host-based HP 用 `zjs`,通用激光机才用 `pdf/pcl/ps`。
  无法查询作业状态,只能确认“字节已发送”。
- **CUPS**:通过 `lp -d <queue>` 发送,可轮询作业状态;队列的 PPD 负责
  PDF→PS→原生流(如 foo2zjs→ZJS)转换,host-based 机型首选。
- **直连 IPP/LPD**:尚未实现;提示用户先用 `lpadmin` 注册进 CUPS,再按 `--queue` 添加。
- 探测:`printer probe <别名>`;发现:`printer discover`;判流:`printer detect`。
- 记录验证结果:`printer stream <别名> <流> verified|failed [--error ...]`。
- 详见 `references/printer-uri.md` 与 `references/streams.md`。

## 退出码

| 码 | 含义 | 你的动作 |
|---|---|---|
| 0 | 成功 | 汇报结果 |
| 3 | 输入文件问题 | 指出文件,请用户替换 |
| 4 | 打印机问题 | 展示 `printer probe`/`discover` 结果 |
| 5 | 排版失败 | 保留作业目录,报告错误 |
| 6 | 发送失败 | 区分网络/队列/格式,不要静默重试 |
| 7 | 需要确认 | 复述摘要,用户同意后加 `--yes` |

## 注意事项

- docx 走 Mammoth + Chrome,**页眉/页脚、精确分页可能简化**;在摘要中提示 `fidelity: reduced`。对版式敏感的文件建议先转 PDF。
- PDF 的边距调整是**等比缩放居中**(字号会随比例变化);txt/md/docx 是**真重排**。摘要中说明。
- 多图 N-up 由脚本自行拼版,发送时强制 `NupPages=1up`,避免驱动二次拼版。
- 打印是物理副作用:默认确认;批量/多份/新打印机必须确认。
- 所有处理在本机完成,只有最终 PDF 会发往打印机。

更多排错见 `references/troubleshooting.md`。