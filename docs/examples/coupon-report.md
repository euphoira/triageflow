# TriageFlow · 故障分析报告

- Run: `b37a203932e649eb8814752285500ba9`
- 模式: **demo**
- 置信度: medium
- 引用核验: 通过

## 问题
优惠券将订单总额降为 0 后，checkout 仍返回原价。请定位原因并给出验证方案。

## 结论
免费订单仍被收费

calculate_total 使用真值判断，合法的 coupon_total=0 被当作未提供优惠价，回退到 subtotal。

## 代码证据

### checkout.py:4-8
```text
def calculate_total(subtotal: float, coupon_total: float | None = None) -> float:
    """A coupon_total of 0 means the order should be free."""
    if coupon_total:
        return coupon_total
    return subtotal
```

## 修复建议
- 将条件改为 coupon_total is not None，区分未传参和零金额。
- 明确金额边界规则；真实支付系统应使用 Decimal 或最小货币单位。

## 验证计划（尚未执行）
- subtotal=100, coupon_total=0 时应返回 0。
- coupon_total=None 时应返回原价；非零优惠价应保持不变。

## 局限性
- 离线规则演示；结论来自内置故障场景，未调用大模型。
- 仅读取源代码，未执行目标仓库的代码或测试。

## 执行指标
```json
{
  "tool_calls": 3,
  "model_calls": 0,
  "input_tokens": 0,
  "output_tokens": 0,
  "duration_ms": 390
}
```
