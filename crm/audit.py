import threading
from .models import AuditLog

_thread_locals = threading.local()


def set_current_user(user):
    """Вызывается из middleware: запоминает текущего пользователя для аудита."""
    _thread_locals.user = user


def get_current_user():
    return getattr(_thread_locals, "user", None)


def set_current_ip(ip):
    _thread_locals.ip = ip


def get_current_ip():
    return getattr(_thread_locals, "ip", None)


def log_action(action, instance, field_name="", old_value="", new_value="", comment=""):
    """
    Универсальная запись в журнал. Не роняет основную операцию,
    даже если аудит по какой-то причине упал.
    """
    try:
        AuditLog.objects.create(
            actor=get_current_user(),
            action=action,
            model_name=instance.__class__.__name__,
            object_id=str(getattr(instance, "pk", "") or ""),
            object_repr=str(instance)[:255],
            field_name=field_name,
            old_value=str(old_value or ""),
            new_value=str(new_value or ""),
            comment=comment,
            ip_address=get_current_ip(),
        )
    except Exception as e:  # аудит не должен ломать бизнес-операцию
        import logging
        logging.getLogger(__name__).error(f"Audit log failed: {e}")