# printing-helper

用于协助调用打印机的 AI Skill:识别/记忆打印机、支持 txt/docx/pdf/图片打印、可调边距与多图拼版,并通过 CUPS/IPP 或 raw socket 9100 发送。

- 设计方案:[docs/printing-helper-design.md](docs/printing-helper-design.md)
- Skill 源码:[skill/printing-helper](skill/printing-helper)
- 全局安装(已创建):`~/.claude/skills/printing-helper` → `skill/printing-helper`

## 快速开始

```bash
PH="$HOME/.claude/skills/printing-helper/scripts/ph.py"

python3 "$PH" printer list                     # 已登记打印机
python3 "$PH" plan 合同.docx --margin narrow   # 只排版,不出纸
python3 "$PH" send --job <job_id> --yes        # 用户确认后发送
```

在 Claude Code 中直接说“把这个文件打印出来”“这 4 张图拼一张纸”“用上次那台打印机”,Skill 会自动走“解析打印机 → 排版预览 → 确认 → 发送”。

## 已登记的打印机

- `net-192-168-1-42`:网络打印机 `192.168.1.42:9100`(raw socket,默认 PDF),默认打印机
- `cups-hp-laserjet-1020`:本机 CUPS 队列 `HP-LaserJet-1020`

默认 A4、normal 边距;偏好保存在 `~/.local/share/printing-helper/`。