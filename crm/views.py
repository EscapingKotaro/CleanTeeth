from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import views as auth_views
from .models import CuratorCase


# ==================== АУТЕНТИФИКАЦИЯ ====================

class CustomLoginView(auth_views.LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True


class CustomLogoutView(auth_views.LogoutView):
    next_page = "crm:login"


class CustomPasswordChangeView(auth_views.PasswordChangeView):
    template_name = "registration/password_change.html"
    success_url = "/crm/password-change/done/"


class CustomPasswordChangeDoneView(auth_views.PasswordChangeDoneView):
    template_name = "registration/password_change_done.html"


# ==================== CRM СТРАНИЦЫ ====================

@login_required
def dashboard(request):
    """Дашборд куратора (с сайдбарами)"""
    # Получаем задачи куратора
    tasks = []  # Пока заглушка, потом подтянем из Task

    # Получаем кейсы куратора
    cases = CuratorCase.objects.filter(
        curator=request.user,
        status__in=[
            CuratorCase.Status.TRANSFERRED,
            CuratorCase.Status.PRESENTED,
            CuratorCase.Status.IN_DECISION,
            CuratorCase.Status.AGREED,
            CuratorCase.Status.IN_PROGRESS,
        ]
    ).select_related("patient", "plan")[:10]

    return render(request, "crm/dashboard.html", {
        "title": "Дашборд",
        "tasks": tasks,
        "cases": cases,
        "use_sidebar": True,  # ← флаг для шаблона
    })


@login_required
def cases_list(request):
    """Список кейсов (с сайдбарами)"""
    # Фильтры
    status_filter = request.GET.get("status")
    sort_by = request.GET.get("sort", "-created_at")

    # Базовый queryset
    queryset = CuratorCase.objects.all()

    # Фильтр по куратору (если не админ/управляющая)
    if request.user.role in ["CURATOR", "SENIOR_CURATOR"]:
        queryset = queryset.filter(curator=request.user)

    # Фильтр по статусу
    if status_filter:
        queryset = queryset.filter(status=status_filter)

    # Сортировка
    allowed_sorts = ["-created_at", "created_at", "-plan__agreed_sum", "plan__agreed_sum", "patient__last_name"]
    if sort_by in allowed_sorts:
        queryset = queryset.order_by(sort_by)
    else:
        queryset = queryset.order_by("-created_at")

    # Подтягиваем связанные данные
    queryset = queryset.select_related("patient", "plan", "curator")

    # Статусы для фильтра
    statuses = CuratorCase.Status.choices

    return render(request, "crm/cases_list.html", {
        "title": "Кейсы",
        "cases": queryset,
        "statuses": statuses,
        "current_status": status_filter,
        "current_sort": sort_by,
        "use_sidebar": True,  # ← флаг для шаблона
        # В views.cases_list добавь в return render:
    "patients": Patient.objects.all()[:50], # Ограничим для производительности
    "plans": TreatmentPlan.objects.all()[:50],
    "curators": CustomUser.objects.filter(role__in=['CURATOR', 'SENIOR_CURATOR']),
    })


@login_required
def case_detail(request, case_id):
    """Детальная страница кейса (с сайдбарами)"""
    case = get_object_or_404(
        CuratorCase.objects.select_related("patient", "plan", "curator"),
        id=case_id
    )

    # Проверка доступа (куратор видит только свои кейсы)
    if request.user.role == "CURATOR" and case.curator != request.user:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("У вас нет доступа к этому кейсу")

    # Задачи кейса (пока заглушка)
    tasks = []

    return render(request, "crm/case_detail.html", {
        "title": f"Кейс #{case.id}",
        "case": case,
        "tasks": tasks,
        "use_sidebar": True,  # ← флаг для шаблона
    })


@login_required
def tasks_list(request):
    """Список задач"""
    return render(request, "crm/tasks_list.html", {
        "title": "Задачи",
        "use_sidebar": True,
    })


from datetime import date
from .models import CuratorMonthlyPlan, CustomUser


@login_required
def motivation(request):
    """Мотивация: куратор видит свою, старший/управляющая — всю команду."""
    today = date.today()
    is_team_view = request.user.role in (
        CustomUser.Role.SENIOR_CURATOR,
        CustomUser.Role.MANAGER,
        CustomUser.Role.ADMIN,
    )

    # Текущий план пользователя (если он куратор)
    own_plan = CuratorMonthlyPlan.objects.filter(
        curator=request.user, year=today.year, month=today.month
    ).first()

    # История куратора
    history = CuratorMonthlyPlan.objects.filter(curator=request.user)[:12]

    # Для руководителей: вся команда за текущий месяц
    team_plans = None
    if is_team_view:
        team_plans = CuratorMonthlyPlan.objects.filter(
            year=today.year, month=today.month
        ).select_related("curator").order_by("curator__last_name")

    return render(request, "crm/motivation.html", {
        "title": "Мотивация",
        "own_plan": own_plan,
        "history": history,
        "team_plans": team_plans,
        "is_team_view": is_team_view,
        "use_sidebar": False,  # мотивация — полноэкранная страница
    })


@login_required
def team_control(request):
    """Контроль отдела (для старшего куратора и выше)"""
    return render(request, "crm/team_control.html", {
        "title": "Контроль отдела",
    })


@login_required
def analytics(request):
    """Аналитика (для управляющей и админа)"""
    return render(request, "crm/analytics.html", {
        "title": "Аналитика",
    })


@login_required
def document_checks(request):
    """Реестр проверок документов"""
    return render(request, "crm/document_checks.html", {
        "title": "Реестр проверок",
    })


@login_required
def settings_view(request):
    """Настройки (только для админа)"""
    return render(request, "crm/settings.html", {
        "title": "Настройки",
    })

from django.shortcuts import redirect
from django.contrib import messages
from .models import Patient, TreatmentPlan

@login_required
def patient_create(request):
    if request.method == 'POST':
        Patient.objects.create(
            last_name=request.POST.get('last_name'),
            first_name=request.POST.get('first_name'),
            phone=request.POST.get('phone'),
            comment=request.POST.get('comment', '')
        )
        messages.success(request, "Пациент успешно создан")
    return redirect('crm:cases_list')

@login_required
def patient_update(request, patient_id):
    if request.method == 'POST':
        patient = Patient.objects.get(id=patient_id)
        patient.last_name = request.POST.get('last_name')
        patient.first_name = request.POST.get('first_name')
        patient.phone = request.POST.get('phone')
        patient.comment = request.POST.get('comment', '')
        patient.save()
        messages.success(request, "Данные пациента обновлены")
    return redirect('crm:case_detail', case_id=request.POST.get('case_id', 1)) # Упрощено для примера

@login_required
def plan_update(request, plan_id):
    if request.method == 'POST':
        plan = TreatmentPlan.objects.get(id=plan_id)
        plan.initial_sum = request.POST.get('initial_sum', 0)
        plan.presentation_sum = request.POST.get('presentation_sum', 0)
        plan.agreed_sum = request.POST.get('agreed_sum', 0)
        plan.presentation_date = request.POST.get('presentation_date') or None
        plan.agreement_date = request.POST.get('agreement_date') or None
        plan.plan_type = request.POST.get('plan_type', '')
        plan.is_signed_by_patient = request.POST.get('is_signed_by_patient') == 'true'
        plan.save()
        messages.success(request, "План лечения обновлён")
    return redirect('crm:case_detail', case_id=plan.cases.first().id if plan.cases.exists() else 1)

@login_required
def case_create(request):
    if request.method == 'POST':
        CuratorCase.objects.create(
            patient_id=request.POST.get('patient_id'),
            plan_id=request.POST.get('plan_id'),
            curator_id=request.POST.get('curator_id'),
            status=CuratorCase.Status.TRANSFERRED
        )
        messages.success(request, "Кейс успешно создан")
    return redirect('crm:cases_list')
