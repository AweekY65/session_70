"""领域错误定义。"""


class LeaseError(Exception):
    pass


class LeaseHeldError(LeaseError):
    """资源已被其他持有者持有且未过期。"""


class LeaseNotHeldError(LeaseError):
    """当前客户端不持有有效租约（未持有、已过期或 token 不匹配）。"""


class StaleFencingTokenError(LeaseError):
    """下游资源拒绝了过期的 fencing token（旧持有者恢复后被识别）。"""
