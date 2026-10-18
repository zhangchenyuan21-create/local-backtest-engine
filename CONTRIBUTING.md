# Contributing

**如有更好的修改，欢迎提交 Pull Request；也欢迎在 Issues 里提出设计建议、数据源适配和性能优化。**

## 提交前

1. 保持聚宽策略兼容：不要随意改变 `jqdata` 公开函数的签名和返回结构。
2. 涉及撮合、费用、T+1、公司行动、滑点或前视偏差的修改，必须附回归测试。
3. 不提交真实行情、账号、密码、Cookie、token、个人日志或 `out/` 结果。
4. 更新 README 中受影响的用法和限制。
5. 至少运行：

```powershell
python -m unittest discover -s tests -p "test_smoke.py" -v
python tests/test_trade.py
python tests/test_shim.py
python -m unittest tests.test_analytics tests.test_factors tests.test_optimize tests.test_overfit tests.test_walkforward -v
```

## Pull Request 说明

请写清楚：

- 修改动机和策略场景
- 修改前后的行为差异
- 数据字段/数据源假设
- 测试命令和结果
- 已知限制

如果只是一个想法或尚未完成的补丁，也欢迎先开 Issue 讨论。
