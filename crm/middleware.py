from .audit import set_current_user, set_current_ip


class AuditMiddleware:
    """Запоминает текущего пользователя и IP для журнала аудита."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user if request.user.is_authenticated else None
        set_current_user(user)

        ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
             or request.META.get("REMOTE_ADDR")
        set_current_ip(ip or None)

        return self.get_response(request)

import threading

_local = threading.local()


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.user = request.user
        try:
            return self.get_response(request)
        finally:
            _local.user = None


def get_current_user():
    return getattr(_local, "user", None)