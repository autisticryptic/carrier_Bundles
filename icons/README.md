# Operator icon project

这个子项目只负责构建期运营商图标，不解析 iOS 或 Android 基带配置。

## 内容

- `package_icons.py`：更新 NekokoLPA2 最小 checkout，按 SQLite 中已有的 PLMN/GID/SPN/profile 匹配 operator-icons，校验 PNG 并写入 `visual_assets.asset_data`。
- `fallback/`：项目自制的中性 SVG badge 与 manifest。
- `vendor/NekokoLPA2/`：自动生成的 shallow + sparse checkout，只保留图标来源代码和上游许可证，不提交到主仓库。

通常不需要单独调用本项目；`tools/seal_db.py` 会在数据库封存前自动执行。调试时可以运行：

```bash
python3 icons/package_icons.py data/carrier-bundles.sqlite3
```

输入数据库必须尚未封存，并且 iOS/Android 项目已经写入 `carriers`、`plmns`、`carrier_profiles` 和 `profile_match_rules`。
