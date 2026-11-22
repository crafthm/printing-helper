# 打印流模型与快速检测

## 1. 打印有多少种"流"

"流"至少分三层,排错时必须分清是哪一层:

### A. 页面描述语言 / 设备流(PDL)

| 流 | 典型设备 | 说明 |
|---|---|---|
| PDF | 现代网络打印机、IPP Everywhere | 由打印机内置 RIP 解析 |
| PostScript (PS/PS2/PS3) | 激光机、绘图仪 | `%!PS` 开头 |
| PCL5 / PCL5e / PCL5c | HP 及兼容激光机 | `ESC E` 复位、`ESC &l` 设置 |
| PCL6 / PCL XL | 较新激光机 | `ESC ) HP-PCL XL` 开头 |
| **ZJS (Zenographics)** | **HP LJ 1000/1018/1020/1022/PSC** | **host-based;必须用 foo2zjs** |
| CAPT / UFR II | Canon LBP/MF | host-based,需 Canon 驱动 |
| SPL | Samsung ML | host-based,需 splix |
| GDI / 专有 | Brother/Lexmark 等 | 需厂商驱动 |
| ESC/P、ESC/POS | Epson 针式/热敏 | 文本+控制码 |
| ZPL/EPL、CPCL | 标签打印机 | 标签专用 |
| URF / PWG Raster / Apple Raster | IPP Everywhere | 驱动无关光栅 |
| PCLm | 移动打印 | PDF 子集 |
| JPEG/TIFF/PNG | 照片打印机 | 直接图片 |

### B. 传输/封装层

- Raw TCP **9100**(JetDirect;透明网桥只是搬运字节)
- **LPD 515**(带控制文件)
- **IPP/IPPS 631**(属性和作业语义)
- SMB、WSD、USB、厂商云/App(私有协议)

### C. 作业封装

- **PJL/UEL**:`ESC%-12345X` + `@PJL ...`(HP/PCL 常见)
- LPD 控制文件
- IPP 属性(`document-format`, `media`, `sides`)

> 关键:**9100 只是管道,不代表打印机懂 PDF**。host-based 机型只认自己的原生流
> (如 ZJS),发 PDF/PCL 会"连接成功但不出纸"。

## 2. 快速检测(尽量不出纸)

按可靠性和成本从高到低:

### 第 1 层:复用本机 CUPS 队列 + PPD(不打印,置信度最高)

```bash
ph.py printer detect <别名> --json
```

检测内容:

- `lpstat -v` / `lpstat -p`:设备 URI、队列状态
- `/etc/cups/ppd/<queue>.ppd`:
  - `*NickName`、`*FoomaticRIPCommandLine`
  - `*cupsFilter` / `*cupsFilter2`
  - `*DefaultPageSize`、`*DefaultResolution`
- URI 匹配:`socket://host:port` 对应当前网络打印机
- 结论:
  - PPD 含 `foo2zjs` → `zjs` + `foo2zjs-wrapper`
  - 含 `hpcups`/`hplip` → 交给 CUPS
  - 含 `splix`/`brlaser`/`capt` → 对应原生流
  - 其他 → `cups`(队列负责转换)

本环境实测:

```
队列 xiaobai-1020 → socket://192.168.1.42:9100
PPD: HP LaserJet 1020 Foomatic/foo2zjs-z1
驱动命令: foo2zjs-wrapper -z1 -P -L0 %A
推荐: cups + zjs(99%) / socket + zjs(95%)
```

### 第 2 层:型号知识库(不打印)

`detect.py` 内置常见 host-based 型号映射:

- HP LJ 1000/1018/1020/1022/PSC → ZJS(foo2zjs)
- Canon LBP/MF → CAPT/UFR II
- Samsung ML → SPL(splix)
- Brother HL → PCL/PS(brlaser)
- IPP Everywhere/Mopria/AirPrint → PDF/URF/PWG

### 第 3 层:网络能力探测(不打印)

- IPP(631):`document-format-supported`、`printer-make-and-model`、
  `urf-supported`、`sides-supported`
- SNMP:`hrDeviceDescr`、`prtInterpreterDescription`
- mDNS TXT:`ty`、`product`、`pdl`、`URF`
- USB IEEE-1284 Device ID:`CMD:` 列表(本机直连时)
- 端口:9100 / 631 / 515 / 80 / 443

### 第 4 层:校准页(最后手段,每次 1 页)

```bash
# 先离线生成 payload,不出纸
ph.py run 测试.txt --printer <别名> --raw-format zjs --dry-run

# 用户确认后出 1 页
ph.py run 测试.txt --printer <别名> --raw-format zjs --yes
```

顺序(不要暴力轮询):

1. `zjs`(已知 host-based HP)
2. `pcl`(通用激光机)
3. `ps`
4. `pdf`

每次结果写回注册表,下次直接跳过失败流:

```bash
ph.py printer stream <别名> zjs verified
ph.py printer stream <别名> pdf failed --error "connection reset"
ph.py printer show <别名>
```

### 流指纹(如果已经能通过 App/CUPS 打印)

用 Wireshark 或在 CUPS 里替换 backend 捕获一份数据,按魔数识别:

| 魔数(hex) | 流 |
|---|---|
| `25 50 44 46` | PDF |
| `25 21 50 53` | PostScript |
| `1b 25 2d 31` | PJL |
| `4a 5a 4a 5a` | ZJS(无 PJL) |
| `1b 45` | PCL5 复位 |
| `1b 29 48` | PCL XL |
| `55 4e 49 52` | URF |
| `52 61 53 32` | PWG Raster |

## 3. 打印机信息如何保存

### 3.1 注册表 `printers.json`(schema v2)

```json
{
  "schema_version": 2,
  "default": "net-192-168-1-42",
  "printers": {
    "net-192-168-1-42": {
      "aliases": ["网络打印机", "net", "192.168.1.42"],
      "transport": "socket",
      "host": "192.168.1.42",
      "port": 9100,
      "uri": "socket://192.168.1.42:9100",
      "queue": null,
      "paper": "A4",
      "pjl": false,
      "identity": {
        "model": "HP LaserJet 1020",
        "vendor": "HP",
        "serial": null,
        "device_id": null,
        "mac": "c0:1c:30:32:49:61",
        "location": "书房"
      },
      "preferred_stream": "zjs",
      "streams": {
        "zjs": {"status": "unknown", "attempted_at": "..."},
        "pdf": {"status": "failed", "error": "connection reset", "attempted_at": "..."},
        "pcl": {"status": "failed", "error": "no paper", "attempted_at": "..."}
      },
      "generator": {"type": "foo2zjs-wrapper", "model": "z1", "resolution": "1200x600"},
      "detection": {"method": "cups-ppd", "confidence": 0.99, "detected_at": "..."},
      "capabilities": {
        "media": ["A4", "Letter", "A5"],
        "duplex": false,
        "color": false,
        "resolution": ["1200x600dpi", "600x600dpi"]
      },
      "defaults": {"margin": "normal", "copies": 1},
      "history": {
        "last_used": null,
        "last_success": null,
        "last_error": null
      }
    }
  }
}
```

要点:

- **`preferred_stream`** 是唯一"当前可用流";`streams` 保存每个流的验证状态和证据。
- **`generator`** 记录如何生成该流(CUPS 过滤器链 / foo2zjs-wrapper / gs 设备)。
- **`detection`** 记录怎么得出的结论(PPD/IPP/型号库/校准),便于复现。
- **`history`** 是轻量成功率/最近错误,完整记录在 `jobs.jsonl`。
- **不存密钥**:云打印 token、Wi-Fi 密码、代理凭据放系统 keyring/环境变量,
  注册表只存引用。
- 原子写入 + 版本迁移;旧字段 `raw_format` 保留兼容。

### 3.2 其他文件

| 文件 | 内容 |
|---|---|
| `preferences.json` | 全局默认:纸张、边距、份数、双面、图片排法 |
| `jobs.jsonl` | 每次作业/校准/失败的审计(含流、字节数、结果) |
| `models.json`(内置) | 型号→流/驱动知识库,随 Skill 版本更新 |
| `jobs/<job_id>/` | `out.pdf`、`preview-1.png`、`spec.json` |

## 4. 本环境结论

- 打印机:HP LaserJet 1020(host-based)
- 盒子:小白盒(透明 9100 网桥)
- 原生流:**ZJS**
- 已验证:PDF/PCL 不出纸;ZJS 离线生成正确(`1b252d31` 开头)
- 推荐路径:
  1. **CUPS 队列 `xiaobai-1020`**(PPD 已含 foo2zjs,最稳)
  2. **socket + ZJS**(`foo2zjs-wrapper`,直连 9100)