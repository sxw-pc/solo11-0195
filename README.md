# 工业气瓶充装追溯采证台

三套离线业务账：瓶体检验资格、充装批次裁定、出入库交接召回。原始 JSON 及哈希、业务实体、裁定历史和当前状态写入 SQLite；迟到证据重算关联封签与在外责任。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m cylindertrail demo --db demo.sqlite
python -m cylindertrail audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独入口为 `bottle-import`、`fill-import` 和 `dispatch-import`，传入 `--db`、`--file`。`fixtures` 含每套账正常与异常来源。`report` 展示十项逐瓶决策、证据 ID、实体、来源哈希和历史修订。导入事务保证同哈希重放幂等，冲突事件回滚。

演示中的 `bottle-peer.json` 与 `fill-peer-recall.json` 给同一批次 G1 引入第二只气瓶及迟到召回；召回会沿批次身份重裁所有关联瓶，而不只改变报送召回的瓶。召回原证 ID 同时进入其它关联瓶的裁定证据。

## 清洗废水物理台账

`effluent_sample.py`、`discharge_gate.py` 与 `effluent_capture.py` 处理两槽设备清洗废水（各 1000 L）。两槽未转入同一均化容器前是独立采样对象：pH 是对数尺度，算术平均（如 7 与 11 平均成 9）不代表任何真实样品；`blended_ph` 只对实际混合的体积按电荷平衡求平衡 pH，且混合批仍须独立滴定复测。逐槽核对 pH、校准误差与滴定碱度后，T-NEUTRAL 可直接排放，T-ALKALINE（pH 11）只能中和复测后排放或交危废承运人。

`EffluentLedger` 是仅追加、哈希链可回放的物理去向台账，约束包括：

- 当日市政排口有效流量累计不超过 1500 L；阀门开闭、在线流量计起止读数、签认出水样按分钟追到具体槽，即使两槽分别合格，2000 L 也不能当天排完。
- 中和机单批不超过 1000 L；酸耗按碱度（mmol/L × L）与实测终点从同一 60000 mmol 库存扣减，库存不足时第二槽投酸被拒，不允许重复占用。
- 承运人单车不超过 1000 L，须持有对应废物介质许可、双签交接，并以终端实际接收回执闭环；现场交出不等于处置完毕，许可在交接前撤销则路线 B 不成立。
- 迟到的校准撤销或复测否决已排体积时，实际排放事实与计量原样保留，剩余槽冻结并生成超标调查与通知，数据库不存在把已流出水量改回库存的路径。

两条合法去向：A 当日只排 T-NEUTRAL，T-ALKALINE 中和复测合格后站内封存、次日在新限额下排放；B 当日只排 T-NEUTRAL，未中和的 T-ALKALINE 联单外运并取得终端回执。验证：

```
python -B -m pytest -q -p no:cacheprovider tests/test_effluent_seed.py
```

