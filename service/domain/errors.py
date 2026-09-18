class DomainError(Exception):
    """业务规则违例。拒绝本身也会被记录为审计事件。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
