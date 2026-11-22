# Printing Helper —— 打印机 AI Skill 设计方案

> 状态:已实现 v1 · 2026-11-22
> 实现位置:`skill/printing-helper/`(全局软链接到 `~/.claude/skills/printing-helper`)
> 目标:在 Claude Code 中以 Skill + 本地 CLI 的形式,把"找打印机 → 选文件 → 排版 → 打印"做成一条可确认、可复现、可记忆的流水线。

---

## 0. 可行性结论

**可行**。本机已具备全部关键组件,且核心链路已做技术验证:

| 验证项 | 结果 |
|---|---|
| Chrome headless 将中文 HTML 渲染为 A4 PDF | ✅ 实测通过(Noto Sans CJK 生效) |
| PyMuPDF 将源页面等比缩放/居中到指定边距框 | ✅ 实测通过 |
| PyMuPDF 将 4 张图片按 2×2 拼到单张 A4 | ✅ 实测通过 |
| CUPS 运行中,`lp/lpstat/lpoptions` 可用 | ✅ |
| CUPS IPP 端点(`localhost:631/printers/...`) | ✅ HTTP 200 |
| LibreOffice 7.3.7 headless | ⚠️ core/draw 已装,但 **Writer 组件缺失**,docx→PDF 当前会报 `source file could not be loaded` |
| mammoth(docx→HTML)+ Chrome(无 sudo 备选) | ✅ 已在隔离 venv 实测通过(中文 A4 PDF) |
| 网络发现工具(`avahi-browse`/`ippfind`) | ✅ |
| 中文字体、Pillow、markdown、requests | ✅ |

**依赖结论**:
- 核心链路(txt/md/pdf/图片 + CUPS)基于已装组件(**零新增依赖**):PyMuPDF、Pillow、markdown、requests、Chrome。
- docx 有两条路:
  - **路径 A(高保真)**:`libreoffice-writer` 当前**未安装**(约 100 MB,需用户 `sudo apt install`);安装后走 OOXML 改边距 + LibreOffice 转 PDF。
  - **路径 B(无 sudo 备选)**:`pip install mammoth`(纯 Python),docx→HTML→Chrome;已在隔离 venv 实测通过,但会丢失页眉/页脚、精确分页等 Word 版式,需在 JobSpec 标注 `fidelity: reduced`。
- docx 边距通过标准库改 OOXML(A 路)或 CSS(B 路)实现,**不依赖 UNO**;不依赖 reportlab/python-docx/pypdf。

**关键约束(必须在方案中体现)**:

1. 当前默认打印机 HP LaserJet 1020 为 **USB 机型**,无网络口、无双面单元。本机所谓"网络接口"落地为 **CUPS 的 IPP 服务**;接入真正的网络打印机时通过 `ipp://` / `ipps://` 适配,架构不变。
2. PDF 的边距调整是 **等比缩放+居中**(不重排);txt/md/docx 才能做到真正的重排。
3. 打印是物理副作用动作,默认 **先排版预览、再确认、后发送**,避免浪费纸张。

---

## 0.1 实现状态(2026-11-22)

已落地并验证(**全程未实际出纸**):

- 注册表/别名/发现:`printer list/add/resolve/default/remove/probe/discover`
- 排版:`plan` / `preview`(等价 `compose`),覆盖 txt/md/html、docx(Mammoth)、PDF、图片
- 边距:`normal` / `narrow` / `none` / 自定义四边;文档真重排,PDF 等比缩放
- 图片:`one-per-page` 与 `nup`(`auto` 或 `RxC`),支持 `--caption` / `--fill` / `--gap`
- 传输:socket 9100(本地 TCP sink 实测收到完整 2 份字节)、CUPS/远端 CUPS(mock `lp` 验证参数)
- 确认门禁(退出码 7)、`--dry-run`、校准页(`calibrate`)、审计日志
- 已登记:网络 `192.168.1.42:9100`(默认,raw PDF);本机 CUPS `HP-LaserJet-1020`

未实现/已知限制:直连 IPP/LPD(需先注册进 CUPS)、LibreOffice Writer 高保真 docx 路径、
raw 格式自动探测(提供 `calibrate` 与 `--raw-format pcl` 手动切换)。

---

## 1. 需求 → 设计映射

| 需求 | 设计落点 |
|---|---|
| 识别、记忆、调取用户输入过的打印机信息 | `printers.json` 结构化注册表 + 别名解析 + 自动发现;不依赖 LLM 记忆,跨会话稳定 |
| 纯文本打印 | txt → 编码探测 → HTML(CSS `@page`)→ Chrome → PDF |
| docx 打印 | 复制副本 → 修改 OOXML 页边距 → LibreOffice 转 PDF;无 Writer 时走 mammoth→HTML→Chrome |
| PDF 打印 | 直接合并;按边距模式等比缩放/居中 |
| 图片打印 | Pillow 校正方向/缩放 → PyMuPDF 拼版 |
| margins: normal / narrow | normal = 25.4 mm;narrow = 12.7 mm;也支持自定义四边(mm) |
| 多图整张打印 | `--images one-per-page`:每图一页,适配内容框 |
| 多图拼一张纸 | `--images nup --grid auto\|2x2\|3x3...`,按网格拼版 |
| 调用打印机网络接口 | CUPS(`lp`,本地 IPP)/ 远端 CUPS(`lp -h`)/ 直连 IPP / raw 9100(可选) |

---

## 2. 总体架构

```
用户自然语言
    │
    ▼
┌─────────────────────────────────────────────┐
│ Claude Code + SKILL.md(意图解析/询问/确认) │
└──────────────┬──────────────────────────────┘
               │ 结构化调用
               ▼
┌─────────────────────────────────────────────┐
│ ph.py  (确定性 CLI,支持 --json)             │
│  plan → compose → send                       │
└───┬───────────────┬───────────────┬─────────┘
    │               │               │
    ▼               ▼               ▼
 输入转换        排版引擎         传输层
 txt/md→Chrome   margins          CUPS (lp)
 docx→OOXML+LO   N-up / 合并     远端 CUPS (-h)
 pdf/image       preview         直接 IPP / raw
    │               │               │
    └───────────────┴───────┬───────┘
                            ▼
                   job PDF → 打印机
                            │
                            ▼
                   jobs.jsonl 审计日志
```

**分工原则**:
- **LLM 负责模糊语义**:理解"把上次那个合同打两份""图都放一页上""用书房的打印机"。
- **脚本负责确定性**:文件解析、排版数学、PDF 生成、打印命令、状态轮询。LLM 不手写 PDF 操作,避免不可复现。

---

## 3. 目录结构

```
skill/printing-helper/
├── SKILL.md                     # frontmatter + 工作流 + 触发词
├── scripts/
│   ├── ph.py                    # 统一入口(argparse 子命令)
│   ├── registry.py              # printers.json 增删改查、别名解析
│   ├── discover.py              # lpstat/lpinfo/avahi/ippfind 发现
│   ├── compose.py               # 调度转换器 + 合并 + 预览 + JobSpec
│   ├── converters/
│   │   ├── text_pdf.py          # txt/md/html → Chrome → PDF
│   │   ├── docx_pdf.py          # mammoth → HTML → Chrome
│   │   ├── pdf_layout.py        # 边距缩放/居中/合并
│   │   └── image_layout.py      # one-per-page / N-up
│   ├── transport.py             # lp / 远端 CUPS / raw socket
│   ├── jobs.py                  # 作业目录、spec、审计日志
│   └── errors.py                # 稳定退出码
└── references/
    ├── printer-uri.md           # 支持的 URI 与添加方法
    └── troubleshooting.md
```

**安装方式**:源码在仓库 `skill/printing-helper/`;已创建全局软链接
`~/.claude/skills/printing-helper -> <repo>/skill/printing-helper`。
运行时数据在 `~/.local/share/printing-helper/`,不放进仓库。

---

## 4. 状态与"记忆"

### 4.1 数据位置

遵循 XDG 规范,默认 `~/.local/share/printing-helper/`,可用 `PH_DATA_DIR` 覆盖:

```
~/.local/share/printing-helper/
├── printers.json      # 打印机注册表(唯一事实来源)
├── preferences.json   # 用户默认偏好
├── jobs/              # 每次排版产物(jobs/<id>/out.pdf, preview-1.png)
└── jobs.jsonl         # 打印审计日志(只追加)
```

**设计决策**:打印机信息**不**依赖 LLM 对话记忆,而是结构化持久化。LLM 记忆只用于补充偏好类软信息(如"用户习惯双面"),避免跨会话丢失、歧义或幻觉。

### 4.2 `printers.json`(v1)

```json
{
  "version": 1,
  "default": "home-hp",
  "printers": {
    "home-hp": {
      "alias": ["home-hp", "书房", "HP1020"],
      "queue": "HP-LaserJet-1020",
      "transport": "cups",
      "uri": "usb://HP/LaserJet%201020",
      "model": "HP LaserJet 1020",
      "location": "书房",
      "paper": "A4",
      "capabilities": {
        "media": ["A4", "Letter", "A5"],
        "duplex": false,
        "color": false,
        "max_dpi": "1200x600dpi"
      },
      "defaults": { "margin": "normal", "quality": "normal", "copies": 1 },
      "added_at": "2026-11-22T11:00:00+08:00",
      "last_used": "2026-11-22T11:40:00+08:00",
      "notes": "USB 连接,经 CUPS/hplip"
    }
  }
}
```

字段说明:
- `alias`:用户可能说出的名称(中文/英文/型号/位置),解析时大小写不敏感。
- `queue`:CUPS 队列名,发送时作为 `lp -d` 的值。
- `transport`:`cups` | `cups-remote` | `ipp` | `socket`。
- `capabilities`:由 `lpoptions -p` + `lpstat` 探测写入,决定是否允许 duplex/color/纸张。

### 4.3 `preferences.json`

```json
{
  "paper": "A4",
  "margin": "normal",
  "image_layout": "one-per-page",
  "copies": 1,
  "duplex": false,
  "confirm_before_print": true
}
```

用户说"以后都用窄边距"时,Skill 调用 `ph.py prefs set margin=narrow` 持久化。

### 4.4 别名解析算法(`ph.py printer resolve <query>`)

优先级从高到低:
1. `alias` 精确匹配(忽略大小写/空格);
2. 队列名精确匹配;
3. `alias` / 型号 / 位置包含匹配;
4. 若 query 为空或是"默认/上次",取 `last_used` → `default`;
5. 无匹配 → 返回候选 + 建议执行 `discover`。

输出:`--json` 返回 `{ "chosen": slot|null, "matches": [...], "reason": "..." }`。多个匹配时 Skill 必须向用户确认,不得自行猜测。

### 4.5 识别用户输入的打印机信息

Skill 从自然语言/粘贴文本中抽取字段,再调用结构化命令,**不把原始文本直接拼进 shell**:

```
"办公室 192.168.1.50 的 HP M404" 
  → ph.py printer add --alias 办公室 --uri ipp://192.168.1.50:631/ipp/print --model "HP M404"
```

`ph.py` 对 URI 做白名单校验(`ipp|ipps|socket|lpd|usb|hp`),拒绝路径与命令注入。

---

## 5. CLI 规格

入口 `ph.py`,所有子命令支持 `--json`(稳定 schema)与 `--help`;退出码见 §11。

```
# 打印机管理
ph.py printer list [--json]
ph.py printer add --alias <a> [--queue <q> | --uri <uri>] [--model M]
                  [--location L] [--paper A4] [--default] [--install]
ph.py printer remove <alias>
ph.py printer default <alias>
ph.py printer resolve <query> [--json]
ph.py printer probe <alias>            # 刷新 capabilities
ph.py printer discover [--network] [--json]

# 检查与规划(不产生打印)
ph.py inspect <file...> [--json]       # 类型、页数、尺寸、编码
ph.py plan <input...> --printer <alias|default> [选项] --json
ph.py preview <input...> [选项] --page 1 --out <png>

# 排版与发送
ph.py compose --spec <job.json> [--out <pdf>]
ph.py send <pdf> --printer <alias> [--copies N] [--duplex] [--yes] [--json]
ph.py run <input...> [plan 选项] [--yes] [--no-wait]   # compose + send 一体

# 任务
ph.py jobs [--json]
ph.py cancel <job-id>
ph.py prefs get|set ...
```

### 5.1 排版选项(`plan/compose/run`)

```
--paper A4|Letter|A5|<WxHmm>     默认取打印机/偏好
--margin normal|narrow|none|T,R,B,L    normal=25.4mm, narrow=12.7mm
--orient auto|portrait|landscape
--images one-per-page|nup        默认 one-per-page
--grid auto|RxC                   如 2x2、3x2
--gap <mm>                        拼版间距,默认 5
--fill                            N-up 时裁剪填满格子(默认 contain)
--caption                         图片下方显示文件名/序号
--copies N
--duplex                          仅当 capabilities.duplex=true
--quality draft|normal|high
--page-numbers                    后置添加页脚页码
--title <str>                     作业名,默认取首个文件名
```

### 5.2 JobSpec(plan 的 JSON 输出)

`plan` 只做检查与计算,不生成 PDF;其输出是可复现、可展示、可确认的作业契约:

```json
{
  "job_id": "20261122-114512-a1b2",
  "printer": { "alias": "书房", "queue": "HP-LaserJet-1020", "transport": "cups" },
  "paper": "A4",
  "margin": { "mode": "normal", "pt": [72, 72, 72, 72] },
  "orientation": "portrait",
  "outputs": [
    { "input": "合同.docx", "kind": "docx", "pages": 3, "method": "ooxml-margin+libreoffice" },
    { "input": "a.jpg", "kind": "image", "count": 4, "layout": "nup", "grid": "2x2", "pages": 1 }
  ],
  "estimated_pages": 4,
  "copies": 1,
  "warnings": ["该打印机不支持双面,已忽略 --duplex"]
}
```

Skill 将 JobSpec 渲染成人类可读摘要(打印机/纸张/边距/页数/份数/图片排法)供用户确认。

---

## 6. 输入处理管线

| 类型 | 处理 |
|---|---|
| `.txt` | 编码探测(`utf-8 → utf-8-sig → gb18030 → big5 → latin-1`),保留换行;转 HTML(等宽预格式化或段落化) |
| `.md` | `markdown` 库转 HTML,套用排版模板 |
| `.html` | 直接交给 Chrome |
| `.docx` | 路径 A:复制副本 → 改 `w:pgMar` → LibreOffice 转 PDF;路径 B:`mammoth` 转 HTML → Chrome(标注 `fidelity: reduced`) |
| `.doc/.rtf/.odt` | 需 LibreOffice Writer;无 Writer 时 `.doc` 提示另存为 docx/pdf,`.rtf/.odt` 可先转 HTML 或提示安装 |
| `.pdf` | PyMuPDF 读取,合并;按边距模式处理 |
| `.png/.jpg/.jpeg/.webp/.bmp/.tif/.tiff` | Pillow `exif_transpose` 校正方向;按目标 DPI 缩放;透明底铺白 |
| 混合输入 | 非图片按顺序合并;**连续**图片构成一个图片组,按 `--images/--grid` 排版;组间分页 |

### 6.1 文本/网页 → PDF(Chrome)

- 生成 HTML 模板,CSS 控制纸张与边距:
  ```css
  @page { size: A4; margin: 25.4mm; }
  body { margin: 0; font-family: "Noto Sans CJK SC", "AR PL UMing CN", sans-serif;
         font-size: 11pt; line-height: 1.6; }
  ```
- 命令(已实测):
  ```
  google-chrome --headless --disable-gpu --no-sandbox \
    --no-pdf-header-footer --print-to-pdf=<out.pdf> file:///<tmp.html>
  ```
- 中文渲染依赖已安装的 Noto Sans CJK;若缺字形,fallback 字体栈兜底。
- 页码 Chrome CLI 不支持 `@page` 边距框计数器 → 由 PyMuPDF 后置添加(可选开关)。

### 6.2 docx → PDF(真重排)

**先做依赖探测,再选路径**:

| 路径 | 前置条件 | 保真度 | 边距实现 |
|---|---|---|---|
| A. LibreOffice | `libreoffice-writer` 已安装 | 高(接近 Word) | 改 OOXML `w:pgMar` 后重排 |
| B. Mammoth + Chrome | `pip install mammoth` | 中(丢失页眉/页脚/精确分页) | CSS `@page` 重排 |
| C. 兜底 | 两者都不可用 | — | 提示安装 Writer 或改传 PDF |

- 本机现状:`libreoffice-core/draw` 已装、**`libreoffice-writer` 未装**,所以默认会走到路径 B(若已安装 mammoth),否则提示安装。
- `.doc`(二进制)只能走路径 A。
- 无论哪条路,**只处理副本**,不动原件;JobSpec 记录所选路径与 `fidelity`。

**路径 A:OOXML 改边距 + LibreOffice**

1. 解压 docx 副本(不修改原件)。
2. 遍历 `word/document.xml` 中所有 `w:sectPr/w:pgMar`,按模式改写:
   - normal:`top=right=bottom=left=1440` twips(=1 英寸=25.4mm)
   - narrow:`720` twips(=12.7mm)
   - 自定义:每边独立(mm × 56.6929,四舍五入)
   - 保留 `header/footer/gutter` 原值。
3. 需要改纸张时同步改写 `w:pgSz`(A4 = 11906×16838 twips)。
4. 重新打包为临时 docx。
5. 转换(隔离用户配置,避免锁冲突):
   ```
   soffice -env:UserInstallation=file:///tmp/ph-lo-<id> \
     --headless --convert-to pdf --outdir <tmp> <copy.docx>
   ```
6. 失败兜底:若转换失败,回退路径 B(已装 mammoth 时)或提示用户安装 Writer;已有 PDF 可直接走 §7.2 缩放。

> `.doc` 二进制格式无法安全改 XML,且只能走路径 A(Writer);无 Writer 时提示用户另存为 docx/pdf。

**路径 B:Mammoth → HTML → Chrome**

1. `mammoth.convert_to_html(docx)` 得到语义 HTML(标题/列表/表格/内嵌图片保留)。
2. 套用与 §6.1 相同的页面模板(CSS `@page` 控制边距;`body{margin:0}`)。
3. Chrome 无头渲染为 PDF。
4. 局限:不保留页眉/页脚、精确分页、文本框/浮动对象;复杂表格可能重排。JobSpec 标注 `"method":"mammoth"`、`"fidelity":"reduced"`,确认摘要中提示"版式可能简化"。

> 注意:Chrome 的 `body` 默认有 8px 外边距,模板必须显式 `margin:0`,否则实际边距会比设定值多约 6pt(本次实测已复现)。

### 6.3 PDF 合并与页面操作

- 同尺寸页合并为一份作业;不同尺寸则统一按目标纸张缩放居中。
- 可选书签:每个源文件在合并结果中加一级书签(`doc.set_toc`)。
- 可选页码:底部居中插入 `i/n`(helv 字体,不依赖中文字体)。

### 6.4 图片

- `ImageOps.exif_transpose` 处理手机照片方向。
- 目标 DPI 上限 300;按最终放置尺寸预缩放,避免嵌入原始 4000px 大图导致 PDF 膨胀(实测 4 张 1200×900 PNG 嵌入后 12MB,预缩放/JPEG 化后可降一个数量级)。
- 透明 PNG 铺白底,避免打印成黑块。
- 支持 CMYK/TIFF 多帧:取首帧并提示。

---

## 7. 排版规则

### 7.1 纸张与边距

| 模式 | 四边 | 说明 |
|---|---|---|
| `normal` | 25.4 mm (72 pt) | 对应 Word "常规" |
| `narrow` | 12.7 mm (36 pt) | 对应 Word "窄" |
| `none` | 0 | PDF 原样输出 |
| 自定义 | `T,R,B,L` | 单位 mm,支持四边不同 |

A4 = 595.28 × 841.89 pt。normal 时内容框 = 451.28 × 697.89 pt。
硬件不可打印区(激光机常见 ~4 mm)远小于 narrow 的 12.7 mm,不会裁切。

### 7.2 两种"调边距"语义(必须向用户说明)

| 输入 | 语义 | 实现 |
|---|---|---|
| txt / md / html / docx | **真重排**:字号不变,行宽/分页变化 | CSS `@page` / OOXML `pgMar`(mammoth 路径为近似重排) |
| PDF | **等比缩放居中**:整页缩进新内容框,字号随之变化 | `page.show_pdf_page(content_box, src, i, keep_proportion=True)` |

`--margin none` 用于"我要原样打印 PDF"的场景。Skill 在确认摘要中注明当前是"重排"还是"缩放"。

### 7.3 多图:整张打印

`--images one-per-page`:
- 每张图独占一页,等比缩放适配内容框并居中。
- 竖图/横图混排时按 `--orient auto`:跟随多数图片方向选择纸张方向(可用 `--orient` 强制)。
- 小图默认不放大(避免糊),可用 `--fill-page` 放大填充。

### 7.4 多图:一张纸 N-up

```
--grid auto | RxC
```

- `auto` 预设:2 → 2×1 / 1×2;4 → 2×2;6 → 2×3;9 → 3×3;按纸张方向与图片中位宽高比选择格子最不浪费的布局。
- 每格内等比 contain(默认)或 `--fill` cover(居中裁剪)。
- 格子间 `--gap`(默认 5 mm),页边距仍遵循 `--margin`。
- 超出格子数自动分页;最后一页空位留白(可 `--no-blank-pages` 截断)。
- 可选 `--caption`:格子底部 8 pt 文件名/序号。
- 数学:设内容框 `W×H`,列 `c` 行 `r`,间距 `g`:
  `cell_w = (W - (c-1)g)/c`,`cell_h = (H - (r-1)g)/r`。

> 注意:打印机驱动本身有 `NupPages` 选项,但我们**自行拼版**,发送时必须 `-o NupPages=1up`,否则会被驱动二次拼版。

### 7.5 混合输入示例

```
"把 report.md、合同.docx,还有这 4 张产品图打出来,图两两一页,窄边距"
→ report.md 重排(narrow) + 合同.docx 重排(narrow) + 图片组 2×2 分 2 页
```

---

## 8. 传输层(网络接口)

### 8.1 适配器

| 适配器 | 使用场景 | 实现 |
|---|---|---|
| `socket` | raw 9100 设备(当前默认打印机) | TCP 直发排版后的 PDF;可选 gs 转 PCL/PS;支持 PJL 包裹 |
| `cups` | 本机队列(本机 HP LaserJet 1020) | `lp -d <queue> ... <pdf>` |
| `cups-remote` | 局域网内其它 CUPS 服务器 | `lp -h <host>[:631] -d <queue> ...`(IPP over HTTP) |
| `ipp` | 直连 IPP Everywhere 打印机 | **未实现**;建议 `lpadmin -m everywhere` 注册后走 `cups` |
| `lpd` | LPD 设备 | **未实现**;建议注册进 CUPS |

**当前环境**:`192.168.1.42` 只开放 9100,无 IPP/HTTP/LPD,因此登记为 `socket`,
默认发送 PDF;若出纸乱码/空白,用 `calibrate` 校准并切换到 `--raw-format pcl`。

**推荐路径**:有 IPP 的网络打印机优先用 `lpadmin -p <name> -E -v ipp://<ip>/ipp/print -m everywhere`
注册进 CUPS,再统一走 `cups`。这样能力探测、队列管理、状态轮询都由 CUPS 承担。

### 8.2 发送参数

```
lp -d <queue> \
   -t "<title>" -n <copies> \
   -o media=A4 \
   -o NupPages=1up \
   -o Resolution=600x600dpi -o Quality=normal \
   [-o sides=two-sided-long-edge] \
   <job.pdf>
```

- 作业 PDF 已按目标纸张/方向生成 → **不加 `fit-to-page`**,避免二次缩放。
- `sides` / `Quality` / `Resolution` 先由 `capabilities` 校验,不支持则告警并忽略。
- 从输出 `request id is <queue>-<id> (1 file(s))` 解析 job id。
- **socket 路径**:不走 `lp`;按份数逐次建立 TCP 连接发送。`--raw-format pcl|pcl-color|ps`
  时先用 Ghostscript 转换(`ljet4`/`pxlcolor`/`ps2write`);`--pjl` 可包裹
  `@PJL SET PAPER=A4` 与 `ENTER LANGUAGE`。

### 8.3 状态与审计

- CUPS:轮询 `lpstat -o <queue>` 消失 + `lpstat -p <queue>` 为 idle → 完成;超时(默认 120 s)
  报告"已提交,状态未确认",不谎报成功。
- **socket:无作业状态接口**,只能确认字节已发送;结果中 `job_status: "unknown"`,
  提示用户检查打印机面板/出纸。
- `--no-wait` 只返回 job id。
- 审计日志 `jobs.jsonl`(只追加):
  ```json
  {"ts":"2026-11-22T11:45:12+08:00","job_id":"HP-LaserJet-1020-42",
   "printer":"home-hp","queue":"HP-LaserJet-1020","inputs":["合同.docx"],
   "pages":3,"copies":1,"options":{"margin":"narrow","paper":"A4"},
   "result":"completed"}
  ```
- 成功后更新 `printers.json` 的 `last_used`,供"用上次那台"解析。

### 8.4 发现

- `ph.py printer discover`:合并 `lpstat -v`(已配置)、`lpinfo -v`(可用后端)、`avahi-browse -rt _ipp._tcp,_ipps._tcp,_printer._tcp` 与 `ippfind`(局域网 mDNS)。
- 输出候选列表(名称/URI/型号/位置),**不自动添加**;用户确认后 `printer add`。
- 当前环境预期只发现本地 3 个 USB 队列(均指向同一台 LJ1020)。

---

## 9. Skill 工作流(SKILL.md)

### 9.1 frontmatter

```yaml
---
name: printing-helper
description: >
  Print files through the user's printers. Use when the user asks to print
  (打印/打出/印出来), mentions a printer (打印机/队列/书房/办公室),
  or needs layout control for printing: margins normal/narrow, multiple
  images on one sheet (N-up), docx/pdf/text/image printing, printer
  discovery and remembering printer details across sessions.
---
```

### 9.2 固定流程

1. **解析意图**:文件清单、打印机线索、排版要求(normal/narrow、图片排法、份数)。
2. **解析打印机**:`ph.py printer resolve`;无匹配时先 `discover`,仍无则询问 IP/名称。
3. **检查输入**:`ph.py plan ... --json`,生成 JobSpec。
4. **展示摘要并确认**(除非用户已明确"直接打印"):
   ```
   打印机: 书房 (HP-LaserJet-1020, USB)
   纸张: A4 · 边距: narrow(1.27cm, 重排)· 份数: 1
   内容: 合同.docx(3页) + 4张图(2×2, 2页)
   合计: 5 页
   ```
   可附 `preview` 生成的第 1 页缩略图。
5. **排版**:`ph.py compose --spec <job.json>`(生成 `out.pdf`)。
6. **发送**:`ph.py send ...`,轮询并报告 job id 与最终状态。
7. **学习**:
   - 新打印机/别名 → `printer add`;
   - 反复出现的偏好("以后都窄边距""默认双面")→ `prefs set`;
   - 不写入 LLM 记忆的内容(如临时文件名)不落盘。

### 9.3 自然语言示例

| 用户说 | Skill 行为 |
|---|---|
| "把这份合同打两份" | 用默认打印机、当前偏好,plan 后确认 |
| "用上次那台打印机" | `resolve "上次"` → `last_used` |
| "我办公室打印机是 192.168.1.50 的惠普" | 解析 URI → `printer add` → 探测 → 设为候选(是否默认需确认) |
| "这 6 张图拼一张纸" | `--images nup --grid auto`(6 → 2×3) |
| "每张图单独一页,窄边距" | `--images one-per-page --margin narrow` |
| "直接打,不用再问" | 跳过第 4 步确认(`--yes`) |
| "别真打,先给我看看" | 只执行 plan + preview,不发送 |

### 9.4 确认策略

- `preferences.confirm_before_print` 默认 `true`。
- 以下情况**强制确认**,即使说过"直接打":份数 > 1、页数 > 20、混合多文件、首次使用的新打印机。
- 打印前必须能看到:打印机、纸张、边距模式、总页数、份数。

---

## 10. 安全与可靠性

- **不拼 shell**:全部 `subprocess.run([...], shell=False)`;URI/别名做白名单与字符校验。
- **不改原件**:docx 只改临时副本;PDF/图片只读。
- **路径校验**:文件必须存在且可读;拒绝符号链接逃逸到敏感目录(可选策略)。
- **资源上限**:单作业页数(默认 200)、文件大小(默认 100 MB)、份数(默认 10);超限需显式覆盖。
- **临时目录**:`jobs/<id>/`,失败保留 24 h,成功只保留缩略图(可配置)。
- **隐私**:所有处理在本机完成;仅最终 PDF 经 CUPS/IPP 发给打印机。
- **权限**:`lpadmin`、`cupsenable` 不在无确认情况下执行;无权限时打印所需命令。
- **幂等**:同一 JobSpec 重复 compose 结果稳定(时间戳除外);send 每次都是新作业,不重试以免重复出纸(重试需用户确认)。

---

## 11. 错误处理与退出码

| 退出码 | 含义 | Skill 动作 |
|---|---|---|
| 0 | 成功 | 报告结果 |
| 2 | 参数错误 | 展示 `--help` 要点 |
| 3 | 输入错误(不存在/损坏/不支持) | 指出文件与原因,请求替换 |
| 4 | 打印机错误(不存在/离线/停用) | 给出 `lpstat -p` 状态与修复建议 |
| 5 | 排版失败 | 保留临时文件,报告日志路径 |
| 6 | 发送失败 | 区分认证/网络/队列错误 |
| 7 | 需要用户确认 | Skill 转为确认流程 |

常见故障:
- LibreOffice 配置锁 → 每次用独立 `UserInstallation`;
- LibreOffice 报 `source file could not be loaded` → 先检查 `libreoffice-writer` 是否安装,再走 mammoth 降级;
- Chrome 沙箱 → 已验证 `--no-sandbox` 可用;
- 字体缺失 → fallback 字体栈并在摘要中告警;
- 驱动二次 N-up → 强制 `NupPages=1up`;
- PDF 字体未嵌入 → 输出告警(打印可能替换字形),不阻断。

---

## 12. 测试与验收

### 12.1 测试层次

- **单元**:边距换算、网格数学、别名解析、URI 校验、编码探测。
- **黄金文件**:固定输入的 `plan` JSON 快照;compose 后用 fitz 断言页数、页面尺寸、内容框位置、图片数量。
- **传输层 mock**:用假 `lp` 脚本捕获参数,断言 `-d/-o media/-o NupPages=1up/-n`;不实际出纸。
- **集成(无纸)**:`run --dry-run` 全链路生成 PDF + 预览;必要时用虚拟 PDF 队列。
- **人工**:各类型各打一页,核对边距/方向/拼版。

### 12.2 验收标准

1. 首次登记打印机后,重启会话说"用书房的打印机"能正确解析(读 `printers.json`,不依赖对话记忆)。
2. txt/md/docx/pdf/图片五类输入均可打印;中文无乱码。
3. `--margin normal` 与 `narrow` 在 txt/docx 上产生**页数可能不同**的真重排;PDF 上为等比缩放且不裁切。
4. 4 张图 `--images nup` 输出 1 页、4 格、每图完整可见;`--images one-per-page` 输出 4 页。
5. 发送命令含正确队列、纸张、份数,且 `NupPages=1up`;状态轮询能报告 completed 或超时。
6. 未经确认不产生实际打印;`preview` 不出纸。
7. 失败时不误报成功,并能定位到类型/队列/文件层。

---

## 13. 里程碑

| 阶段 | 范围 | 产物 |
|---|---|---|
| **P0(1–2 天)** | 注册表 + 解析 + 发现;txt/md/pdf/image 处理;one-per-page/N-up;CUPS 发送;plan/preview/confirm | 可用的最小闭环 |
| **P1(1–2 天)** | docx(Writer 路径 + mammoth 备选);混合输入;页码/书签;任务轮询与审计日志 | 全格式覆盖 |
| **P2(1 天)** | 远端 CUPS、`lpadmin` 网络打印机接入、直连 IPP 适配;能力探测驱动选项 | 真正的网络打印 |
| **P3(0.5–1 天)** | 测试补齐、README/参考文档、全局安装脚本 | 可交付 |

估算:约 700–900 行 Python + 约 200 行 SKILL.md。

---

## 14. 风险与待确认

| 风险/问题 | 影响 | 应对 |
|---|---|---|
| 目标网络打印机的协议未知 | 传输适配选择 | 先 `discover`;优先 IPP Everywhere 注册进 CUPS |
| LJ1020 为 host-based USB 机型 | 无法 raw 9100/无驱动直发 | 固定走 CUPS + hplip(现状已可用) |
| **libreoffice-writer 未安装** | docx 高保真转换不可用 | 路径 B(mammoth+Chrome)零 sudo 可用;或用户自行 `apt install libreoffice-writer` |
| mammoth 版式简化 | 页眉页脚/精确分页丢失 | 摘要中标注 `fidelity: reduced`;对版式敏感的文件建议先转 PDF |
| **docx 路径选择** | 保真度 vs 免安装 | 需用户决定:安装 `libreoffice-writer`(推荐),或接受 mammoth 简化版式 |
| PDF 边距只能缩放 | 字号变化,可能与预期不符 | 摘要中明示;提供 `--margin none` |
| `.doc` 无法真重排 | 边距近似 | 明确降级标注 |
| LibreOffice 首次启动慢/锁 | 超时 | 独立 profile + 长超时 + 重试一次 |
| 打印属物理副作用 | 浪费纸张 | 默认确认 + 强制确认规则 + preview |
| CUPS 权限 | 添加网络打印机失败 | 输出 sudo 命令,由用户执行 |

**已确认(2026-11-22)**:
1. 网络打印机:`192.168.1.42:9100`(仅 9100 开放,raw socket;无 IPP/HTTP/LPD)
2. 默认纸张:A4
3. 安装方式:全局(`~/.claude/skills/printing-helper`)
4. docx:使用 `mammoth`(免 sudo,接受版式简化)

**仍待确认**:是否需要双面默认、是否需要接入其它网络打印机。

---

## 15. 附:本机环境基线(2026-11-22)

- CUPS active;默认队列 `HP-LaserJet-1020`(USB,无 duplex);另有 `Hewlett-Packard-HP-LaserJet-1020`、`HP_LaserJet_1020` 两个指向同一设备的队列。
- 网络打印机 `192.168.1.42`:**仅 9100 开放**,631/80/443/515 均拒绝连接;确认是**透明网桥 + HP LaserJet 1020(host-based)**,原生流为 **ZJS**;现登记为 `socket` + `zjs`,并保留 CUPS 队列 `xiaobai-1020`。
- IPP:`http://localhost:631`(仅监听 127.0.0.1 / ::1)。
- LibreOffice 7.3.7.2 已装 core/draw,**未装 `libreoffice-writer`**;docx 走 mammoth(默认 Python 可导入)。
- Chrome 155.0.8059.84;PyMuPDF 1.26.7;Pillow 11.0.0;Python 3.11.7。
- 字体:Noto Sans CJK(SC/HK 等)、AR PL UMing/UKai。
- `avahi-browse`、`ippfind`、`gs`(含 `ljet4`/`pcl3`/`pxlcolor`/`ps2write`)、`pdfjam` 可用。
- `uno` 仅 `/usr/bin/python3` 可导入(本实现不依赖它)。

---

## 16. 补充:原生流、检测与档案 v2(2026-11-22)

### 16.1 问题定位

`192.168.1.42:9100` 是**透明网桥**,后面的 HP LaserJet 1020 是 **host-based** 机型,
原生流为 **ZJS(Zenographics)**。PDF/PCL/PJL 都会被盒子原样转发,但打印机不解析,
于是表现为“连接成功、不出纸”。正确路径是 foo2zjs。

### 16.2 流的分层

1. **PDL/设备流**:PDF、PS、PCL5/PCL6、PCL XL、**ZJS**、CAPT/UFR II、SPL、GDI、
   ESC/P、ESC/POS、ZPL/EPL、URF/PWG/Apple Raster、PCLm、JPEG/TIFF。
2. **传输**:raw 9100、LPD 515、IPP/IPPS 631、SMB、WSD、USB、厂商云/App。
3. **封装**:PJL/UEL、LPD 控制文件、IPP 属性。

关键:**9100 只是管道**;host-based 机型必须发原生流,不能发 PDF/PCL。

### 16.3 检测阶梯(尽量不出纸)

1. **复用本机 CUPS 队列 + PPD**:`printer detect` 读取 `lpstat -v`、
   `/etc/cups/ppd/<queue>.ppd` 的 `NickName`/`FoomaticRIPCommandLine`/`cupsFilter`,
   按 URI 匹配当前设备。PPD 含 `foo2zjs` → `zjs`。
2. **型号知识库**:HP LJ 1000/1018/1020/1022 → ZJS;Canon LBP → CAPT;Samsung ML → SPL;
   Brother HL → PCL/PS;IPP Everywhere → PDF/URF。
3. **网络能力**:IPP `document-format-supported`、SNMP、mDNS TXT、USB Device ID。
4. **校准页**:最后才打印,顺序 `zjs → pcl → ps → pdf`,每次 1 页,结果写回注册表。

命令:

```bash
ph.py printer detect <别名> --json
ph.py printer show <别名>
ph.py printer stream <别名> <流> verified|failed [--error ...]
```

### 16.4 注册表 schema v2

`printers.json` 在原有 transport/defaults 之外增加:

- `identity`:model/vendor/serial/device_id/mac/location
- `preferred_stream`:当前可用流
- `streams`:每个流的 `status`(verified/failed/unknown)、`attempted_at`、`error`、`evidence`
- `generator`:生成方式(`foo2zjs-wrapper` / CUPS 过滤链 / gs 设备)
- `detection`:检测方法、置信度、时间
- `history`:last_used/last_success/last_error

密钥(云 token、Wi-Fi 密码)不入库,放系统 keyring/环境变量。完整说明见
`skill/printing-helper/references/streams.md`。

### 16.5 实现状态

- 新增 `zjs` 流:`gs ps2write` → `foo2zjs-wrapper -z1 -P -L0 -p9 -r1200x600 ...`;
  离线实测 3,871 字节,魔数 `1b252d31`。
- 新增 `printer detect/show/stream`;检测结果:`cups+zjs 99%`、`socket+zjs 95%`。
- 已登记 `net-192-168-1-42`(socket,preferred=zjs)与 `cups-xiaobai-1020`
  (队列 `xiaobai-1020`,PPD=foo2zjs-z1)。
- 已记录 `pdf`/`pcl` 失败,`zjs` 待实机验证。