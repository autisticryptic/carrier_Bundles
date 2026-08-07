# iOS Carrier Bundle project

这是 iOS 固件/Carrier Bundle 解构与 catalog 重建项目。它与 Android 提取器独立实现，但不复制数据库结构，输出必须符合根目录 [`schema.sql`](../schema.sql)。

项目职责是解包 IPSW、解析 plist/override、归一化公共 IMS/VoWiFi 配置并写入一个由 `tools/init_db.py` 新建的数据库。图标下载和最终只读封存由根项目统一完成。

## 输入

- IPSW 固件元数据。
- `/System/Library/Carrier Bundles/iPhone/*.bundle`。
- `/System/Library/CountryBundles/iPhone/*.bundle`。
- `carrier.plist`、`Info.plist`、设备/基带 override 与签名元数据。

## 工具边界

[blacktop/ipsw](https://github.com/blacktop/ipsw) 作为官方 IPSW 的获取、内容枚举和文件提取工具。本项目负责识别 Carrier/Country Bundle 的优先级，解释 plist/override 语义，并映射到主 schema。`ipsw` 不是现成的 IMS profile 解析器。

AppleDB 和 ipsw.me 可以帮助发现设备/build 与 Apple CDN 固件地址，但它们是第三方索引，不是 Apple 的 IMS 配置 API。来源记录应尽量保存最终 Apple CDN URL、build id 和制品 SHA-256。`idevicerestore` 可作为官方最新恢复固件获取流程的参考。

Apple 没有公开可直接查询完整 IMS/VoWiFi 参数的接口。首版以 Carrier Bundle、Country Bundle 和相关 override 为权威输入；`.bbfw` 只记录制品路径、版本、设备关联和哈希，不以逆向 baseband 内部格式作为首版交付条件。

## 首个验证样本

首个基准不是 iPhone 8，而是支持现代 5G 的 iPhone 16 Pro：

| 项目 | 值 |
|---|---|
| 设备名称 | iPhone 16 Pro |
| Product Type | `iPhone17,1` |
| iOS / build | 26.6 / `23G71` |
| Apple 官方 IPSW | `iPhone17,1_26.6_23G71_Restore.ipsw` |
| IPSW SHA-256 | `2dbcf24e7abd0b7d1b7e5c281bc39f9298b9959c35d0a5b6fc39b30edb0992f7` |
| baseband | `Mav24-2.70.01` |
| baseband 文件 | `Firmware/Mav24-2.70.01.Release.bbfw` |

iPhone 16 Pro 可用于观察 LTE、5G NSA/SA、VoWiFi 和可能存在的 VoNR 配置。硬件能力不能替代字段证据：Carrier Bundle 未公开的值仍保持 `NULL`，不能由“设备支持 5G”推断出运营商已开通 VoNR，也不能从 `.bbfw` 文件名推断 IMS 参数。

建议流水线：

```text
Apple 官方 IPSW/CDN
  -> blacktop/ipsw 下载、枚举、提取
  -> 本项目解析 plist/override 和 bundle 优先级
  -> 字段证据与规范配置写入根 SQLite
```

## 主要映射

| iOS 信息 | 主 schema |
|---|---|
| IPSW、设备、系统与基带版本 | `source_snapshots` |
| bundle 名、运营商名称 | `carriers`、`carrier_profiles` |
| `SupportedSIMs`、GID、SPN | `plmns`、`profile_match_rules` |
| `AttachAPN`、IMS APN、地址族 | `access_configs` |
| P-CSCF 必需性/发现方式 | `pcscf_discovery_methods`、`network_endpoints` |
| IMS domain、realm、transport、IMPI/IMPU 模板 | `ims_configs`、`ims_identity_templates` |
| LTE/VoWiFi SIP Header 差异 | `sip_register_configs` 及 SIP 子表 |
| VoLTE/VoWiFi/SMS 能力开关 | `service_capabilities` |
| entitlement/E911 端点 | `entitlement_configs`、`emergency_configs` |
| 尚未归一化的安全静态键 | `raw_config_values` |

适配器不能输出 IMSI、ICCID、MSISDN、IMEI、IMPI/IMPU 实值、SIM 密钥或 AKA 会话材料。

完整路线、参考项目和里程碑见 [`docs/提取器路线图.md`](../docs/提取器路线图.md)。当前正在以 iPhone 16 Pro 样本实现 I1 获取/提取和 I2 规范化；后续代码和平台测试直接放在本项目目录中。大型 IPSW、解包目录及生成数据库不提交。
