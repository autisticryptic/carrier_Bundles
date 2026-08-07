# Carrier icon assets

本目录中的 SVG 是本项目生成的中性 fallback badge，用于在没有明确授权的官方 logo 时识别运营商。它们不是运营商官方图标，也不表示运营商认可本项目。

设计参考了 NekokoLPA2 的匹配思路：先按 MCC/MNC，再按 GID/SPN/profile name 细分；区别是本项目在**构建期**决定和校验资产，直接写入 SQLite BLOB，运行时数据库只读且不联网。

`manifest.json` 只管理仓库内置的 fallback。真实 logo 由 `icons/package_icons.py` 在发布构建时按最终 PLMN 集合下载并写进数据库，不复制进本目录。operator-icons 未提供可确认的统一再分发许可，因此打包器保留来源、SHA-256、attribution 并标记 `license_spdx='NOASSERTION'`，不会把它们声明为官方或已获授权资产。
