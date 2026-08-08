# SimAdmin 对 Pixel catalog 的 410 真机兼容性问题

## 1. 结论

2026-08-07 在高通 410（MSM8916）设备上，用 SimAdmin 当前候选版本加载以下 catalog：

```text
data/carrier-bundles-pixel-5-redfin-up1a-231105-001-b2.sqlite3
```

结果如下：

- SQLite 文件本身有效、已封存，结构校验全部通过。
- SimAdmin 后端能启动、识别 modem/SIM，并成功只读加载 release。
- 设备当前运营商为 Maxis `50212`，但 catalog 无法为这台 410 解析出可用于注册的 LTE IMS profile。
- VoLTE 自动恢复连续 3 次在 `identity` 阶段失败，错误为 `volte_carrier_profile_missing`。
- 没有创建 IMS bearer、XFRM SA/policy，也没有发出 SIP REGISTER。
- 全量发布 VoWiFi profiles 时，会被一个不完整的 1&1 profile 阻断，导致整个 resolver 未发布。

因此，这个 catalog 目前适合作为固件事实目录，但还不能直接作为 SimAdmin 的完整 IMS 客户端静态配置源。

本文后续只要求 catalog 收录能够从 CarrierSettings、Qualcomm MCFG、厂商 IMS 配置或其他静态制品中取得并保留证据的内容。网络建立过程中下发或协商出的会话数据不属于 catalog 完整度要求，也不应被固化进 release。

## 2. 测试对象

### 2.1 Catalog

```text
release_id:       pixel-redfin-up1a-231105-001-b2
generated_at:     2026-08-07T10:03:07.036333+00:00
generator_version: android/pixel 0.1.0
schema_version:   5
SHA-256:          c4c43775aa70c1a5568b5e11305f6883eeec33024d3264bcba798a50e4033aab
```

`tools/verify_catalog.py` 的结果：

```text
sealed:            true
quick_check:       ok
foreign_key_check: ok
carrier_profiles:  819
access_configs:    2026
source_snapshots:  181
field_evidence:    10518
```

### 2.2 SimAdmin 候选

```text
branch:     codex/volte-beta8-fix
version:    1.1.3
target:     aarch64-unknown-linux-musl
binary SHA: 25919e37d7eb36e931c3c89b6c1a4bd04dd47f0ad7ecd36e42086c2d8b4c7f4a
```

候选以独立目录、独立配置和独立 `data.db` 运行，只监听 `127.0.0.1:3101`。测试后候选已停止，原服务已恢复。

## 3. 问题一：50212 是 Pixel 5 设备规则，不能自动用于高通 410

50212 profile：

```text
profile_id: pixel-redfin-maxis-my-50212-2cce3fec40
source:     etc/CarrierSettings/maxis_my.pb
source key: maxis_my
device:     redfin
build:      UP1A.231105.001.B2
```

匹配规则为：

```text
plmn:                 50212
device_model_pattern: redfin
```

SimAdmin 有意拒绝把带 `device_model_pattern`、GID、SPN、ICCID 等附加条件的规则降级为纯 PLMN 匹配。否则，一个设备专用或 MVNO profile 会被错误应用到同 PLMN 的所有设备/SIM。

因此 410 上仅凭 IMSI/PLMN 查询时，这条 profile 正确地不会被自动选中。这不是应该通过删除 `device_model_pattern` 来修复的问题。

### Catalog 侧建议

如果 Pixel CarrierSettings 中的部分字段确实是运营商通用配置，需要用额外证据生成一个跨设备 profile，而不是直接放宽现有 `redfin` 规则。额外证据可以来自：

- Qualcomm MCFG 的已验证语义提取；
- 同一运营商多个独立设备/固件的一致配置；
- 运营商公开配置或其他可审计来源。

在没有上述证据前，应保留 `device_model_pattern='redfin'`。

## 4. 问题二：50212 缺少可静态提取的 IMS 客户端策略

50212 当前存在的接入事实：

```text
lte_epc: apn=ims, ip_family=ipv4v6
nr_5gc:  apn=ims, ip_family=ipv4v6
IMS domain/realm: ims.mnc012.mcc502.3gppnetwork.org
authentication:   ims_aka
```

但以下数据缺失：

```text
ims_configs.local_port:          NULL
pcscf_discovery_methods:         无记录
sip_register_configs:            无记录
sip_security_mechanisms:         无记录
sip_header_rules:                无记录
sip_contact_parameters:          无记录
```

这些表描述的是客户端在发起连接前就需要知道的静态策略，原则上可能存在于 Qualcomm MCFG/QIPCALL、厂商 IMS 配置或 CarrierSettings 中。当前 Pixel 提取器尚未把这些来源的内部语义映射进 catalog。

`local_port` 在 schema 中是可选字段，缺失本身不应让整个 catalog 或其他 profile 无法加载。若静态来源明确指定端口则提取；没有证据时保持 `NULL`，由消费者根据自身实现决定是否支持。其余 REGISTER、客户端 header/Contact、P-CSCF 获取方式和客户端支持的安全算法同样只能在有静态证据时写入。

当前 50212 记录即使被显式选中，消费者仍无法从 catalog 构造有证据支持的 REGISTER 客户端策略。SimAdmin 的生产策略是“缺少或不支持的静态配置必须在注册前失败”，不会再使用内置 Maxis profile、无证据推导或 REGISTER 变体探测补齐。

### 不应采用的修复

- 不要凭经验把 `local_port=5060` 写入所有 profile；schema 允许未知时保持 `NULL`。
- 不要为全部运营商生成统一的 REGISTER headers、Contact 参数或 IPsec 算法。
- 不要把 `ipsec_security_agreement='auto'` 解释为可安全生成任意 Security-Client。
- 不要因为 APN/domain 存在，就把 profile 标记为 `static-client-ready`。

### Catalog 侧建议

1. 继续实现 Qualcomm MCFG/QIPCALL 内部 IMS/SIP 语义提取，保留字段级证据。
2. 只有在有来源证据时才写入客户端端口、P-CSCF discovery policy、REGISTER policy、header、Contact 和 Security-Client capability。
3. 在 release 中提供明确的“静态客户端配置完整度”信息，让消费者能区分：
   - 仅有运营商能力或 APN/domain；
   - 具备发起 LTE IMS 流程所需的静态客户端策略；
   - 具备发起 VoWiFi IKE/ePDG + IMS 流程所需的静态客户端策略。
4. 若暂不增加 schema 字段，至少在稳定 view 或验证报告中给出 readiness 结果。

## 5. 问题三：全量 catalog 中没有可直接满足严格消费者的数据集

只读统计结果：

```text
ims_configs:             819
sip_register_configs:    328
sip_security_mechanisms: 0
sip_header_rules:        0
sip_contact_parameters:  0
```

IMS access 统计：

| Access | Profile 数 | `local_port` 为 NULL | 缺对应/common REGISTER row |
|---|---:|---:|---:|
| `lte_epc` | 720 | 720 | 412 |
| `wifi_epdg` | 528 | 528 | 244 |

`local_port` 为 NULL 是数据覆盖统计，不应单独作为 profile 无效条件。这里的“存在 REGISTER row”也不等于静态客户端策略完整；当前 328 行大多只携带部分标量，例如 User-Agent，安全机制、header 和 Contact 参数表仍然是空的。

建议让 `tools/verify_catalog.py` 除结构完整性外，再输出静态语义覆盖报告。结构校验通过不能被解释为“客户端已具备发起 IMS 流程所需的全部静态策略”，完整度报告也不应承诺最终注册一定成功。

## 6. 问题四：1&1 不完整条目导致消费者整批发布失败

第一个触发错误的 profile：

```text
profile_id: pixel-redfin-1and1-de-26223-0e95881154
plmn:       26223
device:     redfin
source:     etc/CarrierSettings/1and1_de.pb
```

它声明了以下 access：

```text
lte_epc
nr_5gc
wifi_epdg
```

但缺少：

```text
ims.local_port（当前为 NULL；该字段可选，不能单独判定 profile 无效）
可静态解析的 ePDG endpoint/派生模板
ike_configs
客户端 IKE/ESP proposals
P-CSCF discovery policy
完整的客户端 SIP REGISTER policy
客户端 SIP security mechanisms
```

SimAdmin 启动日志：

```text
Failed to publish VoWiFi carrier profiles to the resolver
error=carrier_catalog_required_field_missing:
pixel-redfin-1and1-de-26223-0e95881154:wifi_epdg:ims.local_port
```

SimAdmin 当前会遍历全部 `wifi_epdg` profiles，并将整批结果一次性发布。任意一个条目无法映射时，整批发布失败。

### 职责边界

Catalog 侧应提供静态配置 readiness，让消费者不要把“有 `wifi_epdg` access row”误认为“已经具备发起 VoWiFi 流程所需的静态客户端策略”。

SimAdmin 侧也需要调整：

- 全量发布时逐 profile 隔离失败，不能让一个无关运营商阻断全部 resolver；
- 保留每个被跳过 profile 的精确错误；
- 当前线路被选中但配置不完整时，返回精确缺失字段，而不是折叠成通用 `volte_carrier_profile_missing`；
- 仍然禁止 builtin/dynamic/REGISTER probing fallback。

## 7. 建议增加的 catalog 验证规则

建议验证工具至少报告下列静态条件，但不能用猜测值自动修补，也不能要求 catalog 保存网络交互产生的会话数据：

### 7.1 LTE IMS readiness

对于标记为“LTE IMS 静态客户端配置已就绪”的 profile：

- 有 `lte_epc`、IMS APN 和 IP family；
- 有可解释的 IMS identity/domain/realm；
- 有来自静态制品的 P-CSCF discovery method；
- 有 access-specific 或 common SIP REGISTER 客户端配置；
- 被静态来源声明为必需的 REGISTER 标量有值；
- 静态策略要求 `sec-agree` 时，有客户端支持且具备证据的 security mechanism；
- 静态来源声明的 required header/Contact rule 有对应值与证据；
- 设备/GID/SPN/ICCID 限制不会被丢失。

`local_port` 等 schema 可选值没有静态证据时允许保持 `NULL`，readiness 应按消费者能力契约判断，不能由验证器擅自填默认值。

### 7.2 VoWiFi readiness

除 LTE IMS 的静态客户端策略外，还应有：

- `wifi_epdg` access；
- 来自静态制品的 ePDG endpoint 或明确、可执行的派生模板；
- 客户端 IKE EAP method；
- 客户端支持的 IKE/ESP proposals；
- 客户端 IDi/IDr policy；
- 静态配置指定的 P-CSCF 获取方式；
- access-specific SIP security/header 覆盖。

### 7.3 Release summary

建议在 `catalog-summary.json` 中增加类似统计：

```json
{
  "static_client_readiness": {
    "lte_ims_static_ready_profiles": 0,
    "vowifi_static_ready_profiles": 0,
    "partial_profiles": 819
  }
}
```

字段名和 schema 形式可以调整，但必须让消费者能明确区分 partial 与 static-client-ready。这个状态只表达静态配置覆盖，不表达网络侧最终认证或注册结果。

## 8. 验收标准

修复后至少满足：

1. `tools/verify_catalog.py` 继续通过 quick check、外键和只读校验。
2. readiness 报告不会把当前 50212 redfin partial profile 标为 410 的 static-client-ready profile。
3. 设备约束不会被展平为 PLMN-only 匹配。
4. 一个不完整的 1&1 profile 不会让消费者无法加载其他完整 profiles。
5. 对 50212：
   - 若没有完整、跨设备的静态证据，消费者应返回明确的 partial/not-ready 原因；
   - 若后续 MCFG 提取补齐，则必须包含可审计的 discovery/REGISTER/security/header/Contact 客户端证据。
6. SimAdmin 在 410 上只会在选中满足其静态能力契约的 profile 后进入网络连接阶段。

## 9. 复现查询

以下查询均应使用只读 URI：

```sql
SELECT cp.profile_id, mr.plmn, mr.device_model_pattern
FROM carrier_profiles AS cp
JOIN profile_match_rules AS mr USING (profile_id)
WHERE mr.plmn = '50212';

SELECT profile_id, home_domain, realm, local_port, ipsec_security_agreement
FROM ims_configs
WHERE profile_id = 'pixel-redfin-maxis-my-50212-2cce3fec40';

SELECT *
FROM v_sip_register_catalog
WHERE profile_id = 'pixel-redfin-maxis-my-50212-2cce3fec40';

SELECT
    (SELECT count(*) FROM ims_configs) AS ims_configs,
    (SELECT count(*) FROM sip_register_configs) AS sip_register_configs,
    (SELECT count(*) FROM sip_security_mechanisms) AS sip_security_mechanisms,
    (SELECT count(*) FROM sip_header_rules) AS sip_header_rules,
    (SELECT count(*) FROM sip_contact_parameters) AS sip_contact_parameters;
```

## 10. 安全说明

本文不包含 IMSI、ICCID、MSISDN、AKA material、SIP nonce、密钥或设备登录凭据。所有结论均来自公开运营商字段、catalog 元数据和脱敏运行阶段日志。
