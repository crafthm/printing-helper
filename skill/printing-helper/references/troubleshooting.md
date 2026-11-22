# 排错指南

## 9100 已连接但不出纸 / 空白 / 乱码

1. 先确认端口:`printer probe <别名>`。
2. 用校准页(会实际出纸,需用户确认):
   ```bash
   ph.py calibrate --printer <别名> --raw-format pdf --yes
   ```
3. 乱码/空白 → 换 PCL:
   ```bash
   ph.py calibrate --printer <别名> --raw-format pcl --yes
   ```
4. 仍失败 → 该设备可能只接受专用驱动(host-based),或个人打印服务器需要特定协议。
   请改用 CUPS 注册后通过 `--queue` 打印,或让用户在电脑端测试。
5. 部分 PCL 设备需要 PJL:在 `printer add` 时加 `--pjl`,或发送时加 `--pjl`。

### 连接被重置(Connection reset by peer)

TCP 能连上、但一写数据就 reset,说明设备拒绝该数据,常见原因:

1. **格式不匹配**:设备只认 PCL/PS,不认 PDF。先试 `--raw-format pcl`。
2. **打印机不在线**:部分 USB 打印服务器在打印机断电/缺纸时会重置连接;先检查电源、USB、纸张。
3. **设备不支持 raw 9100 打印**:host-based 打印机(如 HP LaserJet 1020 的 ZJS 语言)
   配通用 USB 打印服务器时,PDF/PCL/PJL 都可能被拒。此时改用本机 CUPS 队列或厂商驱动。

诊断记录(2026-11-22,192.168.1.42:9100):

- 仅 9100 开放;631/80/443/515 均关闭。
- 发送 PDF → `Connection reset by peer`。
- 发送 PJL `INFO ID` / `INFO STATUS` → 同样 reset(PJL 可能不支持)。
- 发送最小 PCL 复位 `ESC E`(2 字节)→ 对端正常关闭连接,未 reset。

结论:该设备更可能接受 **PCL**;应先用 `calibrate --raw-format pcl` 验证。
若 PCL 也 reset,则基本可判定该 raw 通道不可用,需改走 CUPS/厂商驱动。

## 一页内容分成多页 / 驱动二次拼版

脚本已经自行完成 N-up,发送时强制 `-o NupPages=1up`(CUPS)。
若仍异常,检查打印机默认设置中的“每张页数”是否为 1。

## 边距看起来不对

- txt/md/docx:**真重排**,边距精确。
- PDF:**等比缩放居中**,字号会随比例变化;这是 PDF 无法重排的固有限制。
  需要原样输出时用 `--margin none`。
- 自定义四边:`--margin T,R,B,L`,单位 mm。
- 若某页方向不同,`--orient auto` 会按每页横竖选择纸张方向;强制 `--orient portrait` 可统一。

## docx 版式不一致

本机没有 `libreoffice-writer`,docx 走 **Mammoth → HTML → Chrome**:

- 页眉/页脚、精确分页、文本框、浮动对象可能丢失或重排。
- `plan` 输出中 `fidelity: reduced`,摘要会提示。
- 对版式敏感时,让用户先在 Word/LibreOffice 中导出 PDF,再打印 PDF。
- 需要高保真可安装 Writer 后扩展路径(当前实现未启用):
  `sudo apt install libreoffice-writer`。

## 中文变成方框/乱码

- 文本与 docx 由 Chrome 渲染,系统已安装 Noto Sans CJK / AR PL 字体。
- 图片内嵌文字不受影响。
- 若某文件编码特殊,脚本会依次尝试 utf-8、gb18030、big5;仍异常时请用户另存为 UTF-8。

## CUPS 相关

```bash
ph.py printer probe                     # 查看队列状态
lpstat -p <queue>                       # 是否 disabled / offline
lpstat -o <queue>                       # 活动作业
```

- 队列被停用:需要用户执行 `cupsenable <queue>`(可能需要 sudo)。
- 打印机离线:检查设备电源/USB。
- 远端 CUPS 认证失败:`lp -h host -d queue` 的权限由服务器控制。
- 作业状态显示 `unknown`:可能已提交但无法确认;不要谎报成功,提示用户检查打印机。

## 作业目录与清理

- 产物在 `~/.local/share/printing-helper/jobs/<job_id>/`。
- 失败时保留 `work/` 便于排查;成功后中间文件会删除。
- 清理 7 天前的作业:`jobs.cleanup(7)`(Python 调用;审计日志保留)。

## 依赖

| 依赖 | 用途 | 缺失时 |
|---|---|---|
| PyMuPDF (`fitz`) | PDF 合并/缩放/预览 | 排版失败(退出码 5) |
| Pillow | 图片方向/缩放/拼版 | 图片失败 |
| Chrome/Chromium | txt/md/docx → PDF | 文本/docx 失败 |
| mammoth | docx → HTML | 提示 `pip install mammoth` |
| Ghostscript (`gs`) | PDF → PCL/PS | 仅 raw 非 pdf 格式失败 |
| CUPS (`lp`/`lpstat`) | CUPS 发送/状态 | CUPS 打印机不可用 |

## 常见退出码

| 码 | 排查方向 |
|---|---|
| 3 | 文件不存在/为空/不支持的 `.doc`(请转 docx/PDF) |
| 4 | 别名不唯一或找不到打印机:先 `printer resolve` / `discover` |
| 5 | Chrome/docx 渲染失败:查看 stderr;保留作业目录 |
| 6 | 网络不通、端口错误、队列不存在、gs 转换失败 |
| 7 | 正常确认门禁:用户同意后加 `--yes` |