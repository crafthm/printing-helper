# 打印机 URI 与传输配置

## 支持的 URI

| URI 示例 | transport | 说明 |
|---|---|---|
| `socket://192.168.1.42:9100` | `socket` | 原始 TCP(JetDirect/9100),直接发送排版后的 PDF/PCL |
| `ipp://cups.local:631/printers/Office` | `cups-remote` | 远端 CUPS 队列;通过 `lp -h host -d queue` 发送 |
| `ipp://printer.local/ipp/print` | `ipp` | 直连 IPP 打印机;**暂未实现**,请先注册进 CUPS |
| `lpd://host/queue` | `lpd` | **暂未实现**,请先注册进 CUPS |
| `usb://HP/LaserJet%201020` | `cups` | 本机 USB 设备;需已有 CUPS 队列,用 `--queue` 添加 |

## 添加示例

```bash
PH="$HOME/.claude/skills/printing-helper/scripts/ph.py"

# 网络 raw 9100(当前用户环境)
python3 "$PH" printer add --alias 网络打印机 --alias net \
  --uri socket://192.168.1.42:9100 --raw-format pdf --paper A4 --default

# 本机 CUPS 队列
python3 "$PH" printer add --alias 本机HP --queue HP-LaserJet-1020 --paper A4

# 远端 CUPS
python3 "$PH" printer add --alias 办公室 --uri ipp://192.168.1.50:631/printers/HP-M404

# 自定义 slot
python3 "$PH" printer add --slot office --alias 办公室 --queue Office
```

添加时会做连通性探测(socket 只建 TCP 连接,不发数据)。

## raw 9100 的格式选择

9100 端口只负责搬运字节,不告诉你打印机懂什么格式。默认顺序:

1. `pdf`(默认):现代网络激光机通常支持。
2. `pcl`:`gs -sDEVICE=ljet4 -r600`(黑白 PCL5)。
3. `pcl-color`:`gs -sDEVICE=pxlcolor`(PCL XL 彩色)。
4. `ps`:`gs -sDEVICE=ps2write`。

如果出纸空白、乱码或只出半页:

```bash
python3 "$PH" calibrate --printer 网络打印机 --dry-run        # 先预览,不出纸
python3 "$PH" calibrate --printer 网络打印机 --raw-format pcl --yes
# 确认清晰后,把默认格式改掉:
python3 "$PH" printer add --alias 网络打印机 --uri socket://192.168.1.42:9100 \
  --raw-format pcl
```

`--pjl` 会在数据前后包裹 PJL(`@PJL SET PAPER=A4` / `ENTER LANGUAGE=...`)。部分 HP/PCL 设备需要,但并非所有设备都接受;默认关闭。

## 直连 IPP 的限制

`ipp://host/ipp/print` 的直连实现尚未完成。推荐注册到 CUPS(驱动无关):

```bash
sudo lpadmin -p <name> -E -v ipp://host/ipp/print -m everywhere
python3 "$PH" printer add --alias <name> --queue <name>
```

这样能力探测、队列状态、错误信息都由 CUPS 提供。

## 发现

```bash
python3 "$PH" printer discover --json
```

- 本机:`lpstat -v` / `lpstat -p`
- 网络:`ippfind` + `avahi-browse`(mDNS)
- 只列候选,**不会自动添加**;确认后使用 `printer add`。

## 能力探测

```bash
python3 "$PH" printer probe <别名>
```

CUPS 队列会读取 `lpoptions`(纸张、双面、颜色、分辨率)并写回注册表;socket 设备只能确认端口可达,能力字段为保守默认(无双面、颜色未知)。

## 注册表字段(`printers.json`)

```json
{
  "version": 1,
  "default": "net-192-168-1-42",
  "printers": {
    "net-192-168-1-42": {
      "aliases": ["net-192-168-1-42", "网络打印机", "net", "192.168.1.42"],
      "transport": "socket",
      "host": "192.168.1.42",
      "port": 9100,
      "queue": null,
      "uri": "socket://192.168.1.42:9100",
      "paper": "A4",
      "raw_format": "pdf",
      "pjl": false,
      "capabilities": {"media": ["A4"], "duplex": false, "color": null},
      "defaults": {"margin": "normal", "copies": 1},
      "added_at": "...",
      "last_used": "..."
    }
  }
}
```

别名解析顺序:slot 精确 → 别名/队列/型号/位置/URI 精确 → 包含匹配 → 默认/上次。多个匹配时返回候选,不自动选择。