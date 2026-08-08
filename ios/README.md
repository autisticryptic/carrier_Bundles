# iOS Carrier Bundle 提取器

本目录从 Apple 官方 IPSW 的 Carrier/Country Bundle 提取公共静态 IMS、LTE/NR、VoWiFi、IKE、SIP、媒体和运营商能力，输出根目录 schema v7。iOS 与 Android 独立构建，不互补 profile。

## 数据源与工具

- [blacktop/ipsw](https://github.com/blacktop/ipsw) 用于远程 ZIP 枚举、AEA 解密和文件提取。
- ipsw.me 只用于发现 Product Type 对应的 Apple CDN URL；数据库证据仍来自 Apple IPSW/Carrier Bundle。
- Apple 没有公开的完整 IMS/VoWiFi 配置 API。`.bbfw` 目前只作为固件制品，不猜测其中未确认的 IMS 字段。

首个验证样本是 iPhone 16 Pro (`iPhone17,1`) iOS 26.6 `23G71`。默认线上目标改为 iPhone 16 Pro Max (`iPhone17,2`) 的最新 signed IPSW；还可以独立构建 iPhone 15 Pro Max (`iPhone16,2`) 等代际。

## 构建

```bash
# 当前 Pro Max
python3 ios/build_catalog.py --product-type iPhone17,2 --version latest

# 上一代 Pro Max；输出仍是另一份独立 SQLite
python3 ios/build_catalog.py --product-type iPhone16,2 --version latest
```

正式 release 应把 `--version` 固定为具体 iOS 版本或 build。需要 Python 3、Git、Go、Rust/Cargo、FUSE 3 和足够容纳 AEA/APFS 的空间。

已经导出 bundle 时可跳过下载和 APFS：

```bash
python3 ios/build_catalog.py \
  --bundle-root data/tmp/ios/iphone16pro-23g71/carrier-extract \
  --baseband data/tmp/ios/iphone16pro-23g71/outer/Mav24-2.70.01.Release.bbfw
```

不带 `--product-type` 的离线命令沿用上述 iPhone 16 Pro 固定元数据。其他离线制品应显式给出 `--product-type`、`--version` 和必要时的 `--device-class`。

## v7 映射

| iOS 信息 | v7 位置 |
|---|---|
| Bundle 来源、哈希、解析器和字段证据 | `source_artifacts`、`profile_sources`、`field_evidence` |
| 运营商、PLMN、GID、SPN 和前缀 | `carriers`、`profile_match_rules` |
| APN/DNN、P-CSCF、IMS identity、IKE、SIP、codec、能力、entitlement、E911 | `carrier_profiles.config_json` |
| 图标 | `visual_assets` |

设备、Product Type、OS/build 和 baseband 只存在于输出文件名、日志与构建 summary，不写入数据库。未发现字段保持缺失；禁止输出实际 IMSI、ICCID、IMEI、IMPI/IMPU、AKA 材料、会话密钥和动态网络数据。

## Actions

全量 IPSW 需要大磁盘和 `/dev/fuse`。仓库的多 catalog workflow 将 iOS job 放在带 `ios-extractor` 标签的 self-hosted Linux runner；普通 GitHub runner 只执行 fixture、语法和 schema 测试。
