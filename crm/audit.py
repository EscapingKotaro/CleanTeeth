import threading
from .models import *

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


def log_action(action, instance, field_name="", old_value="", new_value="",
               comment="", amount=None, actor=None):
    """Единая точка журнала: сама определяет кейс, пациента и владельца."""
    from .middleware import get_current_user

    case = patient = owner = None
    if isinstance(instance, CuratorCase):
        case, patient, owner = instance, instance.patient, instance.curator
    elif isinstance(instance, TreatmentPlan):
        case = instance.cases.first()
        patient = instance.patient
        owner = case.curator if case else None
    elif isinstance(instance, Task):
        case = instance.case
        patient = case.patient if case else None
        owner = case.curator if case else None
    elif isinstance(instance, (Payment, WorkOrder)):
        plan = instance.plan
        case = plan.cases.first() if plan else None
        patient = plan.patient if plan else None
        owner = case.curator if case else None
    elif isinstance(instance, DocumentCheck):
        case = instance.case
        patient = case.patient if case else None
        owner = case.curator if case else None
    elif isinstance(instance, Patient):
        patient = instance
    elif isinstance(instance, Contract):
        patient = instance.patient

    return AuditLog.objects.create(
        actor=actor or get_current_user(),
        owner=owner,
        action=action,
        model_name=instance.__class__.__name__,
        object_id=str(getattr(instance, "id", "")),
        object_repr=str(instance)[:200],
        case=case, patient=patient,
        field_name=field_name,
        old_value=str(old_value or ""), new_value=str(new_value or ""),
        amount=amount, comment=comment,
    )