class AppError(Exception):
    """Only controlled, credential-free messages may cross the UI/log boundary."""


class AuthError(AppError):
    def __init__(self):
        super().__init__("登录已失效或权限不足，请重新连接账号。")


class RequestFailed(AppError):
    def __init__(self, status=None, retry_after=0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(
            f"服务器请求失败（状态 {status}）。" if status else "网络请求失败，请检查网络后重试。"
        )


class Cancelled(AppError):
    def __init__(self):
        super().__init__("操作已暂停，可以继续。")


class DeadlineExceeded(AppError):
    def __init__(self, seconds=30):
        super().__init__(f"条目解析超过 {seconds} 秒，NeoDB 可能无法抓取该条目。")
