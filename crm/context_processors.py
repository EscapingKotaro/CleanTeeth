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
