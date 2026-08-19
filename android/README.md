# Android carrier/modem project

这是 Android 系统、厂商和 modem 配置解构与 catalog 重建项目。它与 iOS 提取器独立实现，但不复制数据库结构，输出必须符合根目录 [`schema.sql`](../schema.sql)。

项目职责是解析系统镜像、CarrierConfig、overlay 和厂商 modem 配置，归一化公共 IMS/VoWiFi 配置并写入一个由 `tools/init_db.py` 新建的数据库。图标下载和最终只读封存由根项目统一完成。

## 输入层次

1. AOSP `CarrierConfig`、APN database 与 resource overlay。
2. `/product`、`/system_ext`、`/vendor` 中的运营商/IMS 配置。
3. 高通 MCFG/MBN、Samsung CSC、联发科 modem 配置等厂商格式。

这些层次的覆盖范围不同。AOSP/系统配置不能证明厂商 IMS 实现包含同样的行为，CarrierConfig 中的能力开关也不能证明网络已经为某条用户线路开通服务。

## 实现顺序

### 1. Pixel 官方 Factory Image/完整 OTA

Pixel 是首个目标：官方固件具有稳定的设备、版本和 build 映射，便于自动下载、校验、解包和做跨版本回归。当前 [`pixel`](./pixel/) 提取器已实现 Factory Image 获取、SHA-256 校验、sparse/ext4 解包、`product` CarrierSettings 解析、`vendor` Qualcomm MCFG inventory、SQLite/图标打包和只读封存。

当前首版只支持 Factory Image 的 ZIP/partition 路线。完整 OTA `payload.bin`、EROFS/super、`system_ext` overlay 和 Pixel 6+ Tensor/Exynos modem 配置仍是后续工作；增量 OTA 不作为首版输入。

Pixel 6 及后续多数设备不是 Qualcomm modem 路线，因此不得默认扫描结果中存在 MCFG。Qualcomm MCFG 测试使用明确采用 Qualcomm 平台的旧 Pixel 或其他官方厂商固件，并作为独立适配器运行。

### 2. Samsung 官方固件

第二阶段先解析 AP、CSC/OMC、`epdg_apns_conf.xml`、`imsservice.apk`、`optics`、`prism` 和 product/vendor 配置。CP/Exynos-Shannon modem 的深度逆向不作为 Samsung 首版的交付条件。

### 3. Xiaomi fastboot 基带 inventory

Xiaomi 15 Ultra (`xuanyuan`) 官方 fastboot ROM 现在有独立 inventory 构建器：[`xiaomi`](./xiaomi/)。它只抽取并记录 `NON-HLOS.bin`、`modem*.img`、`dsp*.img` 等基带相关制品的来源、路径、大小和哈希，不解码 modem 内部语义，也不生成运营商 profile。后续 Qualcomm/MediaTek 语义适配器应在这份 inventory 基础上追加经过确认的字段证据。

### 4. Qualcomm 和其他 modem

Qualcomm MCFG/MBN、MediaTek 配置、Exynos/Shannon 配置分别建适配器。逆向工具输出必须保留工具版本、原始文件/键路径和置信度；含义未确认的字段不能直接写入规范列。

### 5. 三方 ROM

三方 ROM 仅用于比较 AOSP CarrierConfig/APN、定位文件和构造公开测试 fixture。它通常不包含完整原厂 IMSService、ePDG 和 modem provisioning，默认不能覆盖官方固件证据。

## 主要映射

| Android 信息 | 主 schema |
|---|---|
| 工厂镜像、配置文件和解析证据 | `source_artifacts`、`profile_sources`、`field_evidence` |
| MCC/MNC、SPN/GID 和公开前缀 | `carriers`、`profile_match_rules` |
| LTE/5G/VoWiFi、ePDG、IKE、SIP、媒体和服务能力 | `carrier_profiles.config_json` |

普通运行日志、SIM 身份、线路绑定和注册结果属于使用方项目，禁止写入此只读目录。

完整路线、参考项目和里程碑见 [`docs/提取器路线图.md`](../docs/提取器路线图.md)。Pixel 的运行方式和已知边界见 [`pixel/README.md`](./pixel/README.md)。后续平台实现和测试直接放在对应目录；大型 firmware image、解包目录及生成数据库不提交。
