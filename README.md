# 工业气瓶充装追溯采证台

三套离线业务账：瓶体检验资格、充装批次裁定、出入库交接召回。原始 JSON 及哈希、业务实体、裁定历史和当前状态写入 SQLite；迟到证据重算关联封签与在外责任。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m cylindertrail demo --db demo.sqlite
python -m cylindertrail audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独入口为 `bottle-import`、`fill-import` 和 `dispatch-import`，传入 `--db`、`--file`。`fixtures` 含每套账正常与异常来源。`report` 展示十项逐瓶决策、证据 ID、实体、来源哈希和历史修订。导入事务保证同哈希重放幂等，冲突事件回滚。
