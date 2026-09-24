from datetime import date
from .models import CuratorMonthlyPlan, CustomUser


def motivation_context(request):
    """Даёт текущую мотивацию куратора в любой шаблон (для сайдбара)."""
    if not request.user.is_authenticated:
        return {}
    if request.user.role not in (CustomUser.Role.CURATOR, CustomUser.Role.SENIOR_CURATOR):
        return {}

    today = date.today()
    current_motivation = CuratorMonthlyPlan.objects.filter(
        curator=request.user, year=today.year, month=today.month
    ).first()

    return {"current_motivation": current_motivation}


from datetime import timedelta
from django.utils import timezone
from .models import Task, CuratorMonthlyPlan, CustomUser


def sidebar_context(request):
    """
    Глобальный контекст для боковых сайдбаров (задачи + мотивация).
    Работает только для авторизованных кураторов.
    """
    if not request.user.is_authenticated:
        return {}

    if request.user.role not in (CustomUser.Role.CURATOR, CustomUser.Role.SENIOR_CURATOR):
        return {}

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    soon_end = today_start + timedelta(days=4)  # ближайшие 3 дня

    base_qs = Task.objects.filter(
        assignee=request.user,
        status=Task.TaskStatus.PENDING,
    ).select_related("case", "case__patient")

    # Левый сайдбар: план дня
    sidebar_tasks = {
        "overdue": base_qs.filter(due_date__lt=today_start).order_by("due_date")[:5],
        "today": base_qs.filter(due_date__gte=today_start, due_date__lt=today_end).order_by("due_date")[:5],
        "soon": base_qs.filter(due_date__gte=today_end, due_date__lt=soon_end).order_by("due_date")[:5],
    }

    # Правый сайдбар: мотивация на текущий месяц
    current_motivation = CuratorMonthlyPlan.objects.filter(
        curator=request.user,
        year=now.year,
        month=now.month,
    ).first()

    return {
        "sidebar_tasks": sidebar_tasks,
        "current_motivation": current_motivation,
    }
