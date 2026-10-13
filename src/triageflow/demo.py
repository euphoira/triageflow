"""Transparent rule-based fixtures, not a simulated LLM benchmark."""

CASES = [
    {
        "id": "coupon",
        "title": "免费订单仍被收费",
        "project_id": "demo-shop",
        "issue": "优惠券将订单总额降为 0 后，checkout 仍返回原价。请定位原因并给出验证方案。",
        "keywords": ["coupon", "优惠券", "免费", "zero", "free order"],
        "query": "coupon_total",
        "path": "checkout.py",
        "start": 4,
        "end": 8,
        "root_cause": (
            "calculate_total 使用真值判断，合法的 coupon_total=0 "
            "被当作未提供优惠价，回退到 subtotal。"
        ),
        "recommendations": [
            "将条件改为 coupon_total is not None，区分未传参和零金额。",
            "明确金额边界规则；真实支付系统应使用 Decimal 或最小货币单位。",
        ],
        "test_plan": [
            "subtotal=100, coupon_total=0 时应返回 0。",
            "coupon_total=None 时应返回原价；非零优惠价应保持不变。",
        ],
    },
    {
        "id": "retry",
        "title": "重试次数少了一次",
        "project_id": "demo-shop",
        "issue": "retry_request 配置 max_attempts=3 实际只调用 2 次；设为 1 则没有调用。",
        "keywords": ["retry", "重试", "attempt", "timeout"],
        "query": "max_attempts",
        "path": "retry.py",
        "start": 4,
        "end": 14,
        "root_cause": (
            "range(max_attempts - 1) 少执行一次；max_attempts=1 时循环为空，直接返回 None。"
        ),
        "recommendations": [
            "循环使用 range(max_attempts)，并明确 max_attempts 包含第一次请求。",
            "拒绝小于 1 的参数；保留最后一次异常用于诊断。",
        ],
        "test_plan": [
            "用计数 stub 验证 max_attempts 为 1、2、3 时的调用次数。",
            "验证第一次成功即返回，以及最终 TimeoutError 透传。",
        ],
    },
    {
        "id": "cache",
        "title": "租户之间缓存串数据",
        "project_id": "demo-shop",
        "issue": "两个 tenant 使用相同 user_id 时，第二个租户会读到第一个租户的缓存资料。",
        "keywords": ["cache", "缓存", "tenant", "租户"],
        "query": "key =",
        "path": "cache.py",
        "start": 6,
        "end": 11,
        "root_cause": (
            "缓存键仅包含 user_id，但该标识只在租户内唯一；同名用户在不同租户间发生缓存碰撞。"
        ),
        "recommendations": [
            "使用 (tenant_id, user_id) 组合作为缓存键。",
            "修复发布时清理旧缓存，并评估缓存有效期和容量。",
        ],
        "test_plan": [
            "不同 tenant_id、相同 user_id 必须分别调用 loader 并返回各自资料。",
            "相同 tenant_id 与 user_id 的第二次请求应命中缓存。",
        ],
    },
]


def match_case(issue: str) -> dict | None:
    lowered = issue.casefold()
    matches = [case for case in CASES if any(key in lowered for key in case["keywords"])]
    return matches[0] if len(matches) == 1 else None


def examples() -> list[dict]:
    return [{key: case[key] for key in ("id", "title", "project_id", "issue")} for case in CASES]
