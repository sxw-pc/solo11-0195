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

## 退瓶温压联合泄漏裁决

`pressure_capture.py` 离线采录瓶身表温/环境温度、校准曲线、封阀称重、库房气体探测、时钟锚等原件：HMAC-SHA256 验签、SHA-256 哈希去重、单位换算与 SQLite 事务保证整包导入。压力与温度必须来自同一物理瓶、同一封闭气室、同一时钟锚与共同校准纪元的可比较时刻，跨瓶温度原件直接拒收，传感器可信度来自签名原件而非配置开关。

`pressure.py` 保留固定的 `drop`（种子复现 100→90），另由 `joint_feasible_states` 在测量误差界内同时列出冷却、泄漏与压力表漂移三个可行世界，并为被排除的世界输出最小冲突见证；300→270 K 恒质量冷却按 `p1·t2/t1` 恰得 90。无可信温度时冷却与泄漏同时可行，禁止放行。`leak_triage.py` 据此给 `release/isolate/pending_verification` 与常规/隔离/待核储位，`TriageLedger` 追加版本化处置单：迟到证据只修订目标瓶，原 isolate 版本与撤销依据永久可读，储区联动检查只能由阴性探测原件关闭。放行闸门经 `rules.return_handoff_state` 与退瓶交接状态接合，未回站或污染瓶不得解除隔离。

签名原件在 `fixtures/evidence/`（路线 A 冷却放行、路线 B 恒温漏气扩检、温度错配、校准漂移），由 `tools/generate_evidence_fixtures.py` 确定性再生成；测试见 `tests/test_pressure_cooling_seed.py`。
