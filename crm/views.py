from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import views as auth_views
from .models import CuratorCase

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.http import HttpResponseForbidden
from datetime import datetime, timedelta, time as dtime
from .models import *

from functools import wraps
from django.core.paginator import Paginator
from django.db.models import Q
from .models import Doctor, Direction
from django.db.models import Count, Exists, OuterRef, Q
from django.db.models import F
from decimal import Decimal
from .audit import log_action
from django.urls import reverse

import csv

import json

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


MANAGER_ROLES = (CustomUser.Role.MANAGER, CustomUser.Role.ADMIN)
LEAD_ROLES = (CustomUser.Role.SENIOR_CURATOR, CustomUser.Role.MANAGER, CustomUser.Role.ADMIN)


def is_case_owner(case, user):
    return case.curator_id == user.id


def can_lead_case(case, user):
    """Владелец КПЛ или руководитель (старший куратор и выше)."""
    return user.role in LEAD_ROLES or is_case_owner(case, user)

ROLE_HOME = {
    CustomUser.Role.CURATOR: "crm:home",
    CustomUser.Role.SENIOR_CURATOR: "crm:team_control",
    CustomUser.Role.MANAGER: "crm:manager_report",
    CustomUser.Role.ADMIN: "crm:users_list",
}


@login_required
def role_home(request):
    """Начальная страница в зависимости от роли."""
    return redirect(ROLE_HOME.get(request.user.role, "crm:dashboard"))

def _next_url(request):
    """Куда вернуться после действия: только внутренние URL из скрытого поля next."""
    nxt = (request.POST.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return None

from django.db import IntegrityError


def next_contract_number(d):
    """Авто-нумерация: Д-<год>-<порядковый 4 знака>."""
    prefix = f"Д-{d.year}-"
    last = Contract.objects.filter(number__startswith=prefix).order_by("-number").first()
    if last:
        try:
            n = int(last.number[len(prefix):]) + 1
        except ValueError:
            n = Contract.objects.filter(number__startswith=prefix).count() + 1
    else:
        n = 1
    return f"{prefix}{n:04d}"


def ensure_service_contract(patient):
    """
    БЗ: договор относится к пациенту, один действующий покрывает несколько планов.
    При согласовании плана создаём договор АВТОМАТИЧЕСКИ, если у пациента
    нет действующего. Нумерация — сквозная по году.
    """
    if patient.contracts.filter(status=Contract.Status.ACTIVE).exists():
        return None  # действующий уже покрывает новый план

    today = timezone.now().date()
    for _ in range(5):  # защита от гонки нумерации
        try:
            contract = Contract.objects.create(
                patient=patient,
                number=next_contract_number(today),
                date=today,
                status=Contract.Status.ACTIVE,
                comment="Создан автоматически при согласовании плана лечения",
            )
            log_action(
                action=AuditLog.Action.CREATE, instance=contract,
                field_name="contract",
                new_value=f"№{contract.number} от {today:%d.%m.%Y}",
                comment="Авто-создание при согласовании плана",
            )
            return contract
        except IntegrityError:
            continue
    return None
# ==================== CRM СТРАНИЦЫ ====================

import calendar
from datetime import date as date_type


def add_months(d, months):
    """Дата плюс N месяцев с защитой от 31-го числа."""
    idx = d.month - 1 + months
    year = d.year + idx // 12
    month = idx % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date_type(year, month, day)

from django.db.models import Sum, Count, Avg, Q, F, Case, When, DecimalField
from django.db.models.functions import Coalesce
from datetime import date, timedelta
import calendar


@login_required
def dashboard(request):
    """Личный дашборд куратора (ТЗ п.13.1)."""
    today = date.today()

    # -------- Период фильтрации --------
    period = request.GET.get("period", "month")
    if period == "today":
        start_date = end_date = today
    elif period == "week":
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
    elif period == "quarter":
        q = (today.month - 1) // 3
        start_month = q * 3 + 1
        start_date = date(today.year, start_month, 1)
        end_date = date(today.year, start_month + 2, calendar.monthrange(today.year, start_month + 2)[1])
    elif period == "custom":
        start_date = parse_date(request.GET.get("from")) or date(today.year, today.month, 1)
        end_date = parse_date(request.GET.get("to")) or today
    else:  # month
        start_date = date(today.year, today.month, 1)
        end_date = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])

    # -------- Базовые queryset'ы по куратору --------
    my_cases = CuratorCase.objects.filter(curator=request.user)
    my_plans = TreatmentPlan.objects.filter(cases__curator=request.user).distinct()
    my_tasks = Task.objects.filter(assignee=request.user)

    # ============================================================
    # 1. ВОРОНКА: количество кейсов по статусам за период
    # ============================================================
    funnel_stats = {
        "transferred": my_cases.filter(status=CuratorCase.Status.TRANSFERRED, created_at__date__range=(start_date, end_date)).count(),
        "presented": my_cases.filter(status=CuratorCase.Status.PRESENTED, created_at__date__range=(start_date, end_date)).count(),
        "in_decision": my_cases.filter(status=CuratorCase.Status.IN_DECISION, created_at__date__range=(start_date, end_date)).count(),
        "agreed": my_cases.filter(status=CuratorCase.Status.AGREED, created_at__date__range=(start_date, end_date)).count(),
        "in_progress": my_cases.filter(status=CuratorCase.Status.IN_PROGRESS, created_at__date__range=(start_date, end_date)).count(),
        "completed": my_cases.filter(status=CuratorCase.Status.COMPLETED, created_at__date__range=(start_date, end_date)).count(),
        "lost": my_cases.filter(status=CuratorCase.Status.LOST, created_at__date__range=(start_date, end_date)).count(),
    }

    # ============================================================
    # 2. КОНВЕРСИИ за период
    # ============================================================
    period_plans = my_plans.filter(
        agreement_date__range=(start_date, end_date),
    )
    presented_count = my_plans.filter(
        presentation_date__range=(start_date, end_date),
    ).count()
    agreed_count = period_plans.count()
    started_count = period_plans.filter(start_date__isnull=False).count()
    completed_count = period_plans.filter(end_date__isnull=False).count()

    conversion_pres_to_agreed = (agreed_count / presented_count * 100) if presented_count else 0
    conversion_agreed_to_start = (started_count / agreed_count * 100) if agreed_count else 0
    conversion_start_to_complete = (completed_count / started_count * 100) if started_count else 0

    # ============================================================
    # 3. ДЕНЬГИ за период (по согласованным планам)
    # ============================================================
    money = period_plans.aggregate(
        sum_presentations=Coalesce(Sum("presentation_sum"), Decimal("0")),
        sum_agreed=Coalesce(Sum("agreed_sum"), Decimal("0")),
        avg_agreed=Coalesce(Avg("agreed_sum"), Decimal("0")),
    )

    # Суммы авансов и оплат (из связанных payments за период)
    period_payments = my_plans.filter(
        cases__curator=request.user,
        payments__payment_date__range=(start_date, end_date),
    ).distinct()
    payments_agg = period_payments.aggregate(
        sum_advances=Coalesce(Sum("payments__amount", filter=Q(payments__is_advance=True, payments__payment_date__range=(start_date, end_date))), Decimal("0")),
        sum_paid=Coalesce(Sum("payments__amount", filter=Q(payments__payment_date__range=(start_date, end_date))), Decimal("0")),
    )

    # Остаток = согласовано - оплачено
    remaining = money["sum_agreed"] - payments_agg["sum_paid"]

    money_stats = {
        "sum_presentations": money["sum_presentations"],
        "sum_agreed": money["sum_agreed"],
        "sum_advances": payments_agg["sum_advances"],
        "sum_paid": payments_agg["sum_paid"],
        "remaining": remaining,
        "avg_agreed": money["avg_agreed"],
    }

    # ============================================================
    # 4. АВАНСЫ за период
    # ============================================================
    plans_with_advance = period_plans.filter(
        payments__is_advance=True,
        payments__payment_date__range=(start_date, end_date),
    ).distinct()
    advance_count = plans_with_advance.count()
    advance_sum = payments_agg["sum_advances"]
    advance_share = (advance_count / agreed_count * 100) if agreed_count else 0
    advance_percent = (advance_sum / money["sum_agreed"] * 100) if money["sum_agreed"] else 0

    advance_stats = {
        "count": advance_count,
        "sum": advance_sum,
        "share": round(advance_share, 1),
        "percent": round(advance_percent, 1),
    }

    # ============================================================
    # 5. ОПЕРАЦИОННАЯ РАБОТА (всегда на текущий день, без фильтра по периоду)
    # ============================================================
    tasks_today = my_tasks.filter(
        status=Task.TaskStatus.PENDING,
        due_date__date=today,
    ).count()
    tasks_overdue = my_tasks.filter(
        status=Task.TaskStatus.PENDING,
        due_date__date__lt=today,
    ).count()
    tasks_soon = my_tasks.filter(
        status=Task.TaskStatus.PENDING,
        due_date__date__range=(today + timedelta(days=1), today + timedelta(days=3)),
    ).count()
    cases_without_next = my_cases.filter(
        status__in=[
            CuratorCase.Status.TRANSFERRED,
            CuratorCase.Status.PRESENTED,
            CuratorCase.Status.IN_DECISION,
            CuratorCase.Status.AGREED,
            CuratorCase.Status.IN_PROGRESS,
        ],
    ).annotate(
        future_tasks=Count("tasks", filter=Q(tasks__status=Task.TaskStatus.PENDING, tasks__due_date__date__gte=today)),
    ).filter(future_tasks=0).count()

    ops_stats = {
        "today": tasks_today,
        "overdue": tasks_overdue,
        "soon": tasks_soon,
        "no_next_action": cases_without_next,
    }

    # ============================================================
    # 6. МОТИВАЦИЯ (текущий месяц)
    # ============================================================
    monthly_plan = CuratorMonthlyPlan.objects.filter(
        curator=request.user, year=today.year, month=today.month,
    ).first()

    return render(request, "crm/dashboard.html", {
        "title": "Дашборд",
        "use_sidebar": True,
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "funnel": funnel_stats,
        "conversions": {
            "pres_to_agreed": round(conversion_pres_to_agreed, 1),
            "agreed_to_start": round(conversion_agreed_to_start, 1),
            "start_to_complete": round(conversion_start_to_complete, 1),
            "presented_count": presented_count,
            "agreed_count": agreed_count,
            "started_count": started_count,
            "completed_count": completed_count,
        },
        "money": money_stats,
        "advance": advance_stats,
        "ops": ops_stats,
        "monthly_plan": monthly_plan,
    })

@login_required
def cases_list(request):
    """Список кейсов: видны все сопровождения отдела + фильтр «только мои»."""
    status_filter = request.GET.get("status")
    sort_by = request.GET.get("sort", "-created_at")
    mine_only = request.GET.get("mine") == "1"

    # Базовый queryset: ВСЕ кейсы (кураторы видят друг друга по правилу подмены)
    queryset = CuratorCase.objects.all()

    if mine_only:
        queryset = queryset.filter(curator=request.user)

    if status_filter:
        queryset = queryset.filter(status=status_filter)

    allowed_sorts = ["-created_at", "created_at", "-plan__agreed_sum", "plan__agreed_sum", "patient__last_name"]
    queryset = queryset.order_by(sort_by if sort_by in allowed_sorts else "-created_at")
    queryset = queryset.select_related("patient", "plan", "curator")

    # Данные для модалки создания плана
    patients = Patient.objects.all().order_by("last_name")[:200]

    active_contracts = (
        Contract.objects.filter(status=Contract.Status.ACTIVE)
        .select_related("patient")
        .order_by("-date")
    )
    contracts_by_patient = {}
    for c in active_contracts:
        contracts_by_patient.setdefault(c.patient_id, []).append({
            "id": c.id,
            "label": f"№{c.number} от {c.date:%d.%m.%Y}",
        })

    return render(request, "crm/cases_list.html", {
        "title": "КПЛ",
        "cases": queryset,
        "statuses": CuratorCase.Status.choices,
        "current_status": status_filter,
        "current_sort": sort_by,
        "current_mine": mine_only,
        "patients": patients,
        "contracts_json": json.dumps(contracts_by_patient, ensure_ascii=False),
        "doctors": Doctor.objects.filter(is_active=True).order_by("last_name"),
        "curators": CustomUser.objects.filter(
            role__in=[CustomUser.Role.CURATOR, CustomUser.Role.SENIOR_CURATOR]
        ),
    })

STATUS_TO_MODAL = {
    CuratorCase.Status.PRESENTED: "presented",
    CuratorCase.Status.IN_DECISION: "decision",
    CuratorCase.Status.AGREED: "agreed",
    CuratorCase.Status.IN_PROGRESS: "in_progress",
    CuratorCase.Status.COMPLETED: "complete",
}

TRANSITION_UI = {
    CuratorCase.Status.PRESENTED: {
        "modal": "presented", "label": "План презентован", "style": "primary",
    },
    CuratorCase.Status.IN_DECISION: {
        "modal": "decision", "label": "Пациент думает", "style": "warning",
    },
    CuratorCase.Status.AGREED: {
        "modal": "agreed", "label": "План согласован", "style": "success",
    },
    CuratorCase.Status.IN_PROGRESS: {
        "modal": "in_progress", "label": "Начать лечение", "style": "primary",
    },
    CuratorCase.Status.COMPLETED: {
        "modal": "complete", "label": "Завершить лечение", "style": "success",
    },
}

@login_required
def case_detail(request, case_id):
    case = get_object_or_404(
        CuratorCase.objects.select_related("patient", "plan", "curator", "lost_reason"),
        id=case_id,
    )

    # Кнопки переходов (с развилкой после презентации)
    transitions = []
    for status in case.next_statuses:
        if status == CuratorCase.Status.IN_PROGRESS and not case.plan.is_ready_for_work:
            continue
        ui = TRANSITION_UI.get(status, {})
        transitions.append({
            "modal": ui.get("modal"),
            "label": ui.get("label", dict(CuratorCase.Status.choices).get(status)),
            "style": ui.get("style", "primary"),
        })

    # Шаги воронки для индикатора (с пометкой опционального шага)
    choices = dict(CuratorCase.Status.choices)
    current_idx = (
        CuratorCase.FUNNEL_ORDER.index(case.status)
        if case.status in CuratorCase.FUNNEL_ORDER else -1
    )
    funnel_steps = []
    for i, s in enumerate(CuratorCase.FUNNEL_ORDER):
        funnel_steps.append({
            "label": choices.get(s),
            "is_past": current_idx >= 0 and i < current_idx,
            "is_current": i == current_idx,
            "is_optional": s == CuratorCase.Status.IN_DECISION,
        })

    # Направления с текущими суммами — для модалки с живым счётчиком
    amount_map = {d.direction: d.amount for d in case.plan.directions.all()}
    directions_ctx = [
        (v, l, amount_map.get(v, Decimal("0")))
        for v, l in PlanDirection.Direction.choices
    ]

    active_contract = case.patient.contracts.order_by("-date", "-id").first()

    active_contract = case.patient.contracts.order_by("-date", "-id").first()
    contract_history = case.patient.contracts.order_by("-date", "-id")[1:]

    is_owner = (case.curator == request.user)
    can_delete_case = is_owner or request.user.role in (CustomUser.Role.MANAGER, CustomUser.Role.ADMIN)
    
    return render(request, "crm/case_detail.html", {
        "title": f"Кейс #{case.id}",
        "case": case,
        "tasks": case.tasks.filter(status=Task.TaskStatus.PENDING).order_by("due_date"),
        "done_tasks": case.tasks.exclude(status=Task.TaskStatus.PENDING).order_by("-due_date")[:15],
        "funnel_steps": CuratorCase.funnel_choices(),
        "task_types": Task.TaskType.choices,
        "priorities": Task.Priority.choices,
        "plan_types": TreatmentPlan.PlanType.choices,
        "lost_reasons": LostReason.objects.filter(is_active=True),
        "curators": CustomUser.objects.filter(
            role__in=[CustomUser.Role.CURATOR, CustomUser.Role.SENIOR_CURATOR]
        ),
        "use_sidebar": True,
        "transitions": transitions,
        "funnel_steps": funnel_steps,
        "active_contract": active_contract,
        "contract_history": contract_history,
        "is_owner": is_owner,
        "can_delete_case": can_delete_case,
        "directions": directions_ctx,
        "payment_categories": Payment.PaymentCategory.choices,
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
    """Мотивация: куратор видит свою, старший/управляющая — любого куратора."""
    today = timezone.now().date()
    can_manage = request.user.role in LEAD_ROLES

    user_id = request.GET.get("user_id")
    if can_manage and user_id:
        target_user = get_object_or_404(CustomUser, id=user_id)
    else:
        # Куратор всегда видит только свою мотивацию, даже если подделал user_id
        target_user = request.user

    # Старший куратор и управляющая могут смотреть любого куратора
    if request.user.role in (
        CustomUser.Role.SENIOR_CURATOR, CustomUser.Role.MANAGER, CustomUser.Role.ADMIN
    ):
        user_id = request.GET.get("user_id")
        if user_id:
            target_user = get_object_or_404(CustomUser, id=user_id)

    monthly_plan = CuratorMonthlyPlan.objects.filter(
        curator=target_user, year=today.year, month=today.month
    ).first()

    breakdown_plans = []
    breakdown_orders = []
    if monthly_plan:
        breakdown_plans = TreatmentPlan.objects.filter(
            cases__curator=target_user,
            agreement_date__year=monthly_plan.year,
            agreement_date__month=monthly_plan.month,
        ).exclude(cases__status=CuratorCase.Status.LOST).distinct().select_related(
            "patient"
        ).order_by("agreement_date")

        breakdown_orders = WorkOrder.objects.filter(
            plan__cases__curator=target_user,
            closed_date__year=monthly_plan.year,
            closed_date__month=monthly_plan.month,
        ).select_related("plan__patient").order_by("closed_date")

    history = CuratorMonthlyPlan.objects.filter(curator=target_user).order_by("-year", "-month")[:12]

    curators = CustomUser.objects.filter(
        role__in=[CustomUser.Role.CURATOR, CustomUser.Role.SENIOR_CURATOR]
    ).order_by("last_name")

    can_manage = request.user.role in (CustomUser.Role.MANAGER, CustomUser.Role.ADMIN)

    return render(request, "crm/motivation.html", {
        "title": "Мотивация",
        "target_user": target_user,
        "monthly_plan": monthly_plan,
        "history": history,
        "curators": curators,
        "is_own": target_user.id == request.user.id,
        "can_manage": can_manage,
        "use_sidebar": False,
        "breakdown_plans": breakdown_plans,
        "breakdown_orders": breakdown_orders,
        "schemes": MotivationScheme.objects.all(),
    })

# осторожно! говнокод
@login_required
def team_control(request):
    if request.user.role not in (CustomUser.Role.SENIOR_CURATOR, CustomUser.Role.MANAGER, CustomUser.Role.ADMIN):
        messages.error(request, "Недостаточно прав.")
        return redirect("crm:home")

    curator_id = request.GET.get("curator", "")
    curator = CustomUser.objects.filter(pk=curator_id).first() if curator_id else None
    deviations = collect_deviations(curator=curator)

    deviation_blocks = [(label, deviations[key]) for key, label in DEVIATION_LABELS]

    return render(request, "crm/team_control.html", {
        "title": "Контроль отдела",
        "deviations": deviations,
        "deviation_labels": DEVIATION_LABELS,
        "deviation_blocks": deviation_blocks,
        "curators": CustomUser.objects.filter(role=CustomUser.Role.CURATOR).order_by("last_name"),
        "curator_id": curator_id,
    })
## !! конец говнокода

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




from django.shortcuts import redirect
from django.contrib import messages
from .models import Patient, TreatmentPlan

from django.utils.dateparse import parse_date

@login_required
def patient_create(request):
    if request.method != "POST":
        return redirect(_next_url(request) or reverse("crm:patients_list"))

    last_name = request.POST.get("last_name", "").strip()
    first_name = request.POST.get("first_name", "").strip()
    phone = request.POST.get("phone", "").strip()

    if not last_name or not first_name or not phone:
        messages.error(request, "Фамилия, имя и телефон обязательны.")
        return redirect(_next_url(request) or reverse("crm:patients_list"))

    Patient.objects.create(
        last_name=last_name,
        first_name=first_name,
        middle_name=request.POST.get("middle_name", "").strip(),
        phone=phone,
        birth_date=parse_date(request.POST.get("birth_date", "")) or None,
        comment=request.POST.get("comment", "").strip(),
    )
    messages.success(request, f"Пациент {last_name} {first_name} создан.")
    return redirect(_next_url(request) or reverse("crm:patients_list"))

@login_required
def patient_update(request, patient_id):
    if request.method == 'POST':
        old_values = {
            "last_name": patient.last_name,
            "first_name": patient.first_name,
            "middle_name": patient.middle_name,
            "phone": patient.phone,
            "comment": patient.comment,
        }
        patient = Patient.objects.get(id=patient_id)
        patient.last_name = request.POST.get('last_name')
        patient.first_name = request.POST.get('first_name')
        patient.middle_name=request.POST.get("middle_name")
        patient.phone = request.POST.get('phone')
        patient.comment = request.POST.get('comment', '')
        patient.birth_date=parse_date(request.POST.get("birth_date"))
        patient.save()
        for f, old in old_values.items():
            new = getattr(patient, f)
            if str(old) != str(new):
                log_action(
                    action=AuditLog.Action.UPDATE, instance=patient,
                    field_name=f,
                    old_value=old or "—", new_value=new or "—",
                    comment="Изменение карточки пациента",
                )
        messages.success(request, "Данные пациента обновлены")
    return redirect('crm:case_detail', case_id=request.POST.get('case_id', 1)) # Упрощено для примера

@login_required
def plan_update(request, plan_id):
    if request.method == 'POST':
        old_values = {
            "initial_sum": plan.initial_sum,
            "presentation_sum": plan.presentation_sum,
            "agreed_sum": plan.agreed_sum,
            "plan_type": plan.plan_type,
            "presentation_date": plan.presentation_date,
            "agreement_date": plan.agreement_date,
        }
        plan = TreatmentPlan.objects.get(id=plan_id)
        plan.initial_sum = request.POST.get('initial_sum', 0)
        plan.presentation_sum = request.POST.get('presentation_sum', 0)
        plan.agreed_sum = request.POST.get('agreed_sum', 0)
        plan.presentation_date = request.POST.get('presentation_date') or None
        plan.agreement_date = request.POST.get('agreement_date') or None
        plan.plan_type = request.POST.get('plan_type', '')
        plan.is_signed_by_patient = request.POST.get('is_signed_by_patient') == 'true'
        plan.save()
        for f, old in old_values.items():
            new = getattr(plan, f)
            if str(old) != str(new):
                log_action(
                    action=AuditLog.Action.UPDATE, instance=plan,
                    field_name=f,
                    old_value=old if old not in (None, "") else "—",
                    new_value=new if new not in (None, "") else "—",
                    amount=new if f == "agreed_sum" else None,
                    comment="Критичное поле плана",
                )
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


from django.db import transaction
from django.contrib import messages
from django.shortcuts import redirect
from decimal import Decimal


def _to_decimal(value):
    """Безопасно парсим сумму из формы в Decimal."""
    try:
        return Decimal(str(value)) if value else Decimal("0")
    except Exception:
        return Decimal("0")


@login_required
def plan_create(request):
    if request.method == "POST":
        patient_id = request.POST.get("patient_id")
        patient = get_object_or_404(Patient, id=patient_id)

        if request.user.role == CustomUser.Role.CURATOR:
            foreign_active = CuratorCase.objects.filter(
                plan__patient=patient,
                status__in=ACTIVE_STATUSES,
            ).exclude(curator=request.user).exists()
            if foreign_active:
                messages.error(
                    request,
                    "Пациент в активном сопровождении у другого куратора — создать новый КПЛ нельзя. "
                    "Для подмены используйте задачи и платежи по существующему КПЛ.",
                )
                return redirect(_next_url(request) or reverse("crm:cases_list"))
        
        plan = TreatmentPlan.objects.create(
            patient=patient,
            doctor_id=request.POST.get("doctor_id") or None,
            initial_sum=_to_decimal(request.POST.get("initial_sum", 0)),
            presentation_sum=_to_decimal(request.POST.get("presentation_sum", 0)),
            agreed_sum=_to_decimal(request.POST.get("agreed_sum", 0)),
            plan_type=request.POST.get("plan_type", ""),
        )
        
        # Связь с договором (если выбран)
        contract_id = request.POST.get("contract_id")
        if contract_id:
            contract = get_object_or_404(Contract, id=contract_id, patient=patient)
            plan.contract = contract
            plan.save(update_fields=["contract"])
        
        # Создание кейса
        curator_id = request.POST.get("curator_id") or request.user.id
        case = CuratorCase.objects.create(
            patient=patient,
            plan=plan,
            curator_id=curator_id,
            status=CuratorCase.Status.TRANSFERRED,
        )
        
        log_action(AuditLog.Action.CREATE, case, "case", "", f"#{case.id}", "Создан КПЛ")
        messages.success(request, f"План и КПЛ созданы.")
        return redirect("crm:case_detail", case_id=case.id)


def _parse_dt(value):
    """Принимает '2026-02-15T14:00' или '2026-02-15' → aware datetime."""
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        d = parse_date(value)
        if d:
            dt = datetime.combine(d, dtime(12, 0))
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt

def _apply_status_transition(case, new_status, post, user):
    """Валидация условий выхода + применение статуса. Возвращает список ошибок."""
    from django.db import transaction

    errors = []
    today = timezone.now().date()

    # ===== 2. План презентован =====
    if new_status == CuratorCase.Status.PRESENTED:
        result_text = post.get("presentation_result", "").strip()
        pres_date = parse_date(post.get("presentation_date", "")) or today
        if not result_text:
            errors.append("Зафиксируйте результат презентации — это обязательное условие статуса.")
        else:
            with transaction.atomic():
                plan = case.plan
                plan.presentation_date = pres_date
                plan.presentation_result = result_text
                plan.save(update_fields=["presentation_date", "presentation_result"])

    # ===== 3. Решение в работе =====
    elif new_status == CuratorCase.Status.IN_DECISION:
        reason = post.get("decision_reason", "").strip()
        comment = post.get("decision_comment", "").strip()
        next_dt = _parse_dt(post.get("next_action_date", ""))
        next_type = post.get("next_action_type") or Task.TaskType.CONTACT

        if not reason:
            errors.append("Укажите причину, по которой пациент ещё не принял решение.")
        if not comment:
            errors.append("Добавьте комментарий.")
        if not next_dt:
            errors.append("Укажите дату и время следующего действия.")

        if not errors:
            with transaction.atomic():
                case.decision_reason = reason
                case.decision_comment = comment
                # Правило непрерывности: автоматически создаём следующую задачу
                Task.objects.create(
                    case=case,
                    task_type=next_type,
                    description=f"Следующий шаг после «Решение в работе»: {reason}",
                    due_date=next_dt,
                    assignee=case.curator or user,
                    priority=Task.Priority.HIGH,
                )

    # ===== 4. План согласован =====
    elif new_status == CuratorCase.Status.AGREED:
        agreed_sum = _to_decimal(post.get("agreed_sum"))
        plan_type = post.get("plan_type", "")
        next_step = post.get("next_step", "").strip()
        next_dt = _parse_dt(post.get("next_step_date", "")) or timezone.now() + timedelta(days=1)

        if agreed_sum <= 0:
            errors.append("Укажите согласованную сумму.")
        if not plan_type:
            errors.append("Выберите тип плана (с авансом / по факту).")

        if not errors:
            with transaction.atomic():
                plan = case.plan
                plan.agreed_sum = agreed_sum
                plan.plan_type = plan_type
                plan.agreement_date = plan.agreement_date or today
                plan.save(update_fields=["agreed_sum", "plan_type", "agreement_date"])
             #   ensure_service_contract(case.patient)
                if next_step:
                    Task.objects.create(
                        case=case,
                        task_type=Task.TaskType.DOCUMENTS,
                        description=f"Дальнейший шаг: {next_step}",
                        due_date=next_dt,
                        assignee=case.curator or user,
                        priority=Task.Priority.HIGH,
                    )

    # ===== 5. Лечение в процессе =====
    elif new_status == CuratorCase.Status.IN_PROGRESS:
        # План должен быть готов к работе (п.5.3 + п.6 условие выхода)
        if not case.plan.is_ready_for_work:
            if case.plan.plan_type == TreatmentPlan.PlanType.WITH_ADVANCE:
                errors.append(
                    "План не готов к работе: внесите аванс и отметьте график подписанным."
                )
            else:
                errors.append("План не готов к работе: нужно согласование и подпись пациента.")
        control_date = parse_date(post.get("next_control_date", ""))
        if not control_date:
            errors.append("Укажите следующий визит / контрольную дату.")
        else:
            with transaction.atomic():
                case.next_control_date = control_date
                plan = case.plan
                plan.start_date = plan.start_date or today
                plan.save(update_fields=["start_date"])
                Task.objects.create(
                    case=case,
                    task_type=Task.TaskType.NEXT_STAGE,
                    description="Проконтролировать следующий этап лечения",
                    due_date=timezone.make_aware(datetime.combine(control_date, dtime(10, 0))),
                    assignee=case.curator or user,
                    priority=Task.Priority.MEDIUM,
                )

    # ===== 6. Лечение завершено =====
    elif new_status == CuratorCase.Status.COMPLETED:
        with transaction.atomic():
            case.completed_at = today
            plan = case.plan
            plan.end_date = plan.end_date or today
            plan.save(update_fields=["end_date"])
            case.tasks.filter(status=Task.TaskStatus.PENDING).update(
                status=Task.TaskStatus.CANCELLED
            )

    # ===== Закрывающий исход: Отказ / потерян =====
    elif new_status == CuratorCase.Status.LOST:
        lost_date = parse_date(post.get("lost_date", ""))
        lost_reason_id = post.get("lost_reason") or None
        lost_sum = _to_decimal(post.get("lost_potential_sum"))
        lost_comment = post.get("lost_comment", "").strip()

        if not lost_date:
            errors.append("Укажите дату отказа.")
        if not lost_reason_id:
            errors.append("Выберите причину отказа из справочника.")
        if lost_sum <= 0:
            errors.append("Укажите сумму потенциального плана.")
        if not lost_comment:
            errors.append("Добавьте комментарий к отказу.")

        if not errors:
            with transaction.atomic():
                case.lost_date = lost_date
                case.lost_reason_id = lost_reason_id
                case.lost_potential_sum = lost_sum
                case.lost_comment = lost_comment
                case.tasks.filter(status=Task.TaskStatus.PENDING).update(
                    status=Task.TaskStatus.CANCELLED
                )

    else:
        errors.append("Неизвестный статус.")

    return errors


@login_required
def case_change_status(request, case_id):
    """Смена статуса КПЛ с сохранением доп. полей для IN_DECISION и LOST."""
    case = get_object_or_404(CuratorCase, id=case_id)
    touch_case(case)
    if request.method != "POST":
        return redirect("crm:case_detail", case_id=case.id)

    new_status = request.POST.get("status")
    if new_status not in dict(CuratorCase.Status.choices):
        messages.error(request, "Некорректный статус.")
        return redirect("crm:case_detail", case_id=case.id)

    old_status = case.status
    case.status = new_status

    # Специфичные поля для IN_DECISION
    if new_status == CuratorCase.Status.IN_DECISION:
        case.decision_reason = request.POST.get("decision_reason", "").strip()
        case.decision_comment = request.POST.get("decision_comment", "").strip()
        case.next_action_type = request.POST.get("next_action_type", "")
        next_action_date_str = request.POST.get("next_action_date", "")
        if next_action_date_str:
            try:
                case.next_action_date = datetime.fromisoformat(next_action_date_str)
            except ValueError:
                messages.warning(request, "Некорректная дата следующего действия.")

    # Специфичные поля для LOST
    if new_status == CuratorCase.Status.LOST:
        lost_date_str = request.POST.get("lost_date", "")
        if lost_date_str:
            case.lost_date = parse_date(lost_date_str)
        case.lost_reason_id = request.POST.get("lost_reason") or None
        case.lost_potential_sum = _to_decimal(request.POST.get("lost_potential_sum", 0))
        case.lost_comment = request.POST.get("lost_comment", "").strip()

    # Специфичные поля для AGREED
    if new_status == CuratorCase.Status.AGREED:
        case.plan.agreed_sum = _to_decimal(request.POST.get("agreed_sum", 0))
        case.plan.plan_type = request.POST.get("plan_type", "")
        case.plan.agreement_date = timezone.now().date()
        case.plan.save(update_fields=["agreed_sum", "plan_type", "agreement_date"])

        # Создание задачи из "Дальнейший шаг"
        next_step = request.POST.get("next_step", "").strip()
        if next_step:
            Task.objects.create(
                case=case,
                task_type=Task.TaskType.OTHER,
                description=next_step,
                due_date=timezone.now() + timedelta(days=1),
                assignee=case.curator,
                priority=Task.Priority.MEDIUM,
            )

    # Специфичные поля для PRESENTED
    if new_status == CuratorCase.Status.PRESENTED:
        pres_date_str = request.POST.get("presentation_date", "")
        if pres_date_str:
            case.plan.presentation_date = parse_date(pres_date_str)
        case.plan.presentation_result = request.POST.get("presentation_result", "").strip()
        case.plan.save(update_fields=["presentation_date", "presentation_result"])

    # Специфичные поля для IN_PROGRESS
    if new_status == CuratorCase.Status.IN_PROGRESS:
        next_control_str = request.POST.get("next_control_date", "")
        if next_control_str:
            case.next_control_date = parse_date(next_control_str)
        # Создание задачи контроля
        if case.next_control_date:
            Task.objects.create(
                case=case,
                task_type=Task.TaskType.NEXT_STAGE,
                description="Контроль следующего визита",
                due_date=datetime.combine(case.next_control_date, dtime(9, 0)),
                assignee=case.curator,
                priority=Task.Priority.MEDIUM,
            )

    # Завершение лечения — отмена активных задач
    if new_status == CuratorCase.Status.COMPLETED:
        case.tasks.filter(status=Task.TaskStatus.PENDING).update(
            status=Task.TaskStatus.CANCELLED,
            result="Отменено при завершении лечения",
        )

    case.save()

    log_action(
        action=AuditLog.Action.STATUS, instance=case,
        field_name="status",
        old_value=old_status,
        new_value=new_status,
        comment=(
            f"{dict(CuratorCase.Status.choices).get(old_status)} → "
            f"{dict(CuratorCase.Status.choices).get(new_status)}"
        ),
    )
    messages.success(request, f"Статус изменён на «{dict(CuratorCase.Status.choices).get(new_status)}».")
    return redirect(_next_url(request) or reverse("crm:case_detail", kwargs={"case_id": case.id}))


@login_required
def task_create(request, case_id):
    case = get_object_or_404(CuratorCase, id=case_id)
    touch_case(case)
    if request.method == "POST":
        due_dt = _parse_dt(request.POST.get("due_date"))
        if not due_dt:
            messages.error(request, "Укажите дату и время задачи.")
        else:
            Task.objects.create(
                case=case,
                task_type=request.POST.get("task_type") or Task.TaskType.CONTACT,
                description=request.POST.get("description", "").strip(),
                due_date=due_dt,
                assignee_id=request.POST.get("assignee_id") or case.curator_id or request.user.id,
                priority=request.POST.get("priority") or Task.Priority.MEDIUM,
            )
            log_action(
        action=AuditLog.Action.CREATE, instance=task,
        field_name="task",
        new_value=f"{task.get_task_type_display()} · {task.due_date:%d.%m.%Y %H:%M}",
        comment="Задача создана",
    )
            messages.success(request, "Задача создана.")
    return redirect("crm:case_detail", case_id=case_id)


@login_required
def task_complete(request, task_id):
    task = get_object_or_404(
        Task.objects.select_related("case", "case__curator", "assignee"), id=task_id
    )
    case = task.case
    back = _next_url(request) or reverse("crm:case_detail", kwargs={"case_id": task.case_id})

    if request.method != "POST":
        return redirect(back)

    # ТЗ гл.4: последняя задача АКТИВНОГО КПЛ требует следующего действия.
    # Для закрытого КПЛ (COMPLETED/LOST) не требует.
    is_last_active = bool(case) and case.is_active and not case.tasks.filter(
        status=Task.TaskStatus.PENDING
    ).exclude(id=task.id).exists()

    next_type = request.POST.get("next_action_type", "")
    next_date = _parse_dt(request.POST.get("next_action_date", ""))

    if is_last_active and not (next_type and next_date):
        messages.error(
            request,
            "Это последняя задача активного КПЛ — укажите следующее действие (тип и дату).",
        )
        return redirect(back)

    task.status = Task.TaskStatus.DONE
    task.result = request.POST.get("result", "").strip()
    task.completed_at = timezone.now()
    task.save(update_fields=["status", "result", "completed_at"])
    log_action(
        action=AuditLog.Action.COMPLETE, instance=task,
        field_name="status", old_value="PENDING", new_value="DONE",
        comment=f"Результат: {task.result or '—'}",
    )

    if is_last_active and next_type and next_date:
        next_task = Task.objects.create(
            case=case,
            task_type=next_type,
            description=request.POST.get("next_description", "").strip()
                        or "Следующее действие после завершения задачи",
            due_date=next_date,
            assignee=task.assignee or case.curator,
            priority=Task.Priority.MEDIUM,
        )
        log_action(
            action=AuditLog.Action.CREATE, instance=next_task,
            field_name="task",
            new_value=f"{next_task.get_task_type_display()} · {next_task.due_date:%d.%m.%Y %H:%M}",
            comment="Следующее действие при завершении последней задачи",
        )
        messages.success(request, "Задача выполнена. Создано следующее действие.")
    else:
        messages.success(request, "Задача выполнена.")
    return redirect(back)

@login_required
def task_postpone(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if request.method == "POST":
        old_due = task.due_date
        new_due = _parse_dt(request.POST.get("due_date", "")) or old_due
        task.due_date = new_due
        task.save(update_fields=["due_date"])
        log_action(
            action=AuditLog.Action.UPDATE, instance=task,
            field_name="due_date",
            old_value=f"{old_due:%d.%m.%Y %H:%M}",
            new_value=f"{new_due:%d.%m.%Y %H:%M}",
            comment="Задача перенесена",
        )
        messages.success(request, "Задача перенесена.")
    return redirect(_next_url(request) or reverse("crm:case_detail", kwargs={"case_id": task.case_id}))

def manager_required(view_func):
    """Доступ только для Управляющей и Админа (справочники — их зона)."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.role not in (CustomUser.Role.MANAGER, CustomUser.Role.ADMIN):
            return HttpResponseForbidden("Недостаточно прав для этого раздела.")
        return view_func(request, *args, **kwargs)
    return wrapper


@login_required
def doctors_list(request):
    """Список врачей: поиск + фильтр по направлению/статусу + пагинация."""
    query = request.GET.get("q", "").strip()
    direction_filter = request.GET.get("direction", "")
    status_filter = request.GET.get("active", "")

    queryset = Doctor.objects.all().order_by("last_name", "first_name")

    if query:
        queryset = queryset.filter(
            Q(last_name__icontains=query)
            | Q(first_name__icontains=query)
            | Q(middle_name__icontains=query)
            | Q(phone__icontains=query)
            | Q(email__icontains=query)
        )
    if direction_filter:
        queryset = queryset.filter(direction=direction_filter)
    if status_filter == "active":
        queryset = queryset.filter(is_active=True)
    elif status_filter == "inactive":
        queryset = queryset.filter(is_active=False)

    paginator = Paginator(queryset, 15)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Сохраняем параметры поиска при переходах по страницам
    search_params = request.GET.copy()
    search_params.pop("page", None)
    search_params = search_params.urlencode()



    return render(request, "crm/doctors_list.html", {
        "title": "Врачи",
        "page_obj": page_obj,
        "total_count": paginator.count,
        "directions": Doctor._meta.get_field("direction").choices,
        "query": query,
        "direction_filter": direction_filter,
        "status_filter": status_filter,
        "search_params": search_params,
        # без сайдбаров
    })


@login_required
def doctor_create(request):
    if request.method != "POST":
        return redirect("crm:doctors_list")

    last_name = request.POST.get("last_name", "").strip()
    first_name = request.POST.get("first_name", "").strip()
    if not last_name or not first_name:
        messages.error(request, "Фамилия и имя обязательны.")
        return redirect("crm:doctors_list")

    Doctor.objects.create(
        last_name=last_name,
        first_name=first_name,
        middle_name=request.POST.get("middle_name", "").strip(),
        phone=request.POST.get("phone", "").strip(),
        email=request.POST.get("email", "").strip(),
        direction=request.POST.get("direction", ""),
        is_active=request.POST.get("is_active") == "on",
        ident_doctor_id=request.POST.get("ident_doctor_id", "").strip() or None,
        comment=request.POST.get("comment", "").strip(),
    )
    messages.success(request, f"Врач {last_name} {first_name} добавлен.")
    return redirect("crm:doctors_list")

@login_required
def doctor_update(request, doctor_id):
    doctor = get_object_or_404(Doctor, id=doctor_id)
    if request.method != "POST":
        return redirect("crm:doctors_list")

    doctor.last_name = request.POST.get("last_name", doctor.last_name).strip()
    doctor.first_name = request.POST.get("first_name", doctor.first_name).strip()
    doctor.middle_name = request.POST.get("middle_name", "").strip()
    doctor.phone = request.POST.get("phone", "").strip()
    doctor.email = request.POST.get("email", "").strip()
    doctor.direction = request.POST.get("direction", "")
    doctor.is_active = request.POST.get("is_active") == "on"
    doctor.ident_doctor_id = request.POST.get("ident_doctor_id", "").strip() or None
    doctor.comment = request.POST.get("comment", "").strip()
    doctor.save()

    messages.success(request, "Данные врача обновлены.")
    return redirect("crm:doctors_list")

@login_required
def doctor_delete(request, doctor_id):
    doctor = get_object_or_404(Doctor, id=doctor_id)
    if request.method != "POST":
        return redirect("crm:doctors_list")

    # Защита от случайного удаления (ТЗ п.23):
    # если на врача есть планы — не удаляем, а деактивируем
    if doctor.treatment_plans.exists():
        doctor.is_active = False
        doctor.save(update_fields=["is_active"])
        messages.warning(
            request,
            f"У врача «{doctor}» есть планы лечения — он деактивирован вместо удаления.",
        )
    else:
        name = str(doctor)
        doctor.delete()
        messages.success(request, f"Врач «{name}» удалён.")

    return redirect("crm:doctors_list")


from django.core.paginator import Paginator
from django.contrib.auth.hashers import make_password


def admin_or_manager_required(view_func):
    """Управление пользователями — только Админ и Управляющая (ТЗ п.16)."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.role not in (CustomUser.Role.ADMIN, CustomUser.Role.MANAGER):
            return HttpResponseForbidden("Недостаточно прав.")
        return view_func(request, *args, **kwargs)
    return wrapper


@admin_or_manager_required
def users_list(request):
    query = request.GET.get("q", "").strip()
    role_filter = request.GET.get("role", "")

    queryset = CustomUser.objects.all().order_by("last_name", "first_name")
    if query:
        queryset = queryset.filter(
            Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
        )
    if role_filter:
        queryset = queryset.filter(role=role_filter)

    paginator = Paginator(queryset, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    search_params = request.GET.copy()
    search_params.pop("page", None)

    return render(request, "crm/users_list.html", {
        "title": "Пользователи",
        "page_obj": page_obj,
        "total_count": paginator.count,
        "roles": CustomUser.Role.choices,
        "query": query,
        "role_filter": role_filter,
        "search_params": search_params.urlencode(),
    })


@admin_or_manager_required
def user_create(request):
    if request.method != "POST":
        return redirect("crm:users_list")

    username = request.POST.get("username", "").strip()
    password = request.POST.get("password", "").strip()
    if not username or not password:
        messages.error(request, "Логин и пароль обязательны.")
        return redirect("crm:users_list")

    if CustomUser.objects.filter(username=username).exists():
        messages.error(request, f"Пользователь с логином «{username}» уже существует.")
        return redirect("crm:users_list")

    CustomUser.objects.create(
        username=username,
        password=make_password(password),
        first_name=request.POST.get("first_name", "").strip(),
        last_name=request.POST.get("last_name", "").strip(),
        email=request.POST.get("email", "").strip(),
        phone=request.POST.get("phone", "").strip(),
        role=request.POST.get("role", CustomUser.Role.CURATOR),
        is_active=request.POST.get("is_active") == "on",
    )
    # Аудит создания сработает автоматически в save()
    messages.success(request, f"Пользователь «{username}» создан.")
    return redirect("crm:users_list")


@admin_or_manager_required
def user_update(request, user_id):
    user = get_object_or_404(CustomUser, id=user_id)
    if request.method != "POST":
        return redirect("crm:users_list")

    user.first_name = request.POST.get("first_name", user.first_name).strip()
    user.last_name = request.POST.get("last_name", user.last_name).strip()
    user.email = request.POST.get("email", "").strip()
    user.phone = request.POST.get("phone", "").strip()
    user.role = request.POST.get("role", user.role)
    # is_active меняем отдельным действием, здесь не трогаем
    user.save()  # аудит роли сработает в save()

    # Опциональная смена пароля
    new_password = request.POST.get("new_password", "").strip()
    if new_password:
        user.set_password(new_password)
        user.save(update_fields=["password"])
        log_action(
            action=AuditLog.Action.PASSWORD_CHANGE,
            instance=user,
            field_name="password",
            comment="Пароль изменён вручную",
        )

    messages.success(request, "Пользователь обновлён.")
    return redirect("crm:users_list")


@admin_or_manager_required
def user_toggle_active(request, user_id):
    user = get_object_or_404(CustomUser, id=user_id)

    # Защита: нельзя деактивировать самого себя
    if user.id == request.user.id:
        messages.error(request, "Нельзя деактивировать собственную учётную запись.")
        return redirect("crm:users_list")

    if request.method == "POST":
        user.is_active = not user.is_active
        user.save(update_fields=["is_active"])  # аудит в save()
        state = "активирован" if user.is_active else "деактивирован"
        messages.success(request, f"Пользователь «{user.username}» {state}.")
    return redirect("crm:users_list")




@login_required
def patients_list(request):
    query = request.GET.get("q", "").strip()
    status_filter = request.GET.get("active", "")
    contract_filter = request.GET.get("contract", "")

    active_contracts = Contract.objects.filter(
        patient=OuterRef("pk"),
        status=Contract.Status.ACTIVE,
    )

    queryset = Patient.objects.annotate(
        cases_count=Count("cases", distinct=True),
        plans_count=Count("plans", distinct=True),
        has_active_contract=Exists(active_contracts),
    ).order_by("last_name", "first_name")

    if query:
        queryset = queryset.filter(
            Q(last_name__icontains=query)
            | Q(first_name__icontains=query)
            | Q(middle_name__icontains=query)
            | Q(phone__icontains=query)
        )
    if status_filter == "active":
        queryset = queryset.filter(is_active=True)
    elif status_filter == "inactive":
        queryset = queryset.filter(is_active=False)
    if contract_filter == "yes":
        queryset = queryset.filter(has_active_contract=True)
    elif contract_filter == "no":
        queryset = queryset.filter(has_active_contract=False)

    paginator = Paginator(queryset, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    search_params = request.GET.copy()
    search_params.pop("page", None)

    return render(request, "crm/patients_list.html", {
        "title": "Пациенты",
        "page_obj": page_obj,
        "total_count": paginator.count,
        "query": query,
        "status_filter": status_filter,
        "contract_filter": contract_filter,
        "search_params": search_params.urlencode(),
    })

@login_required
def patient_create(request):
    if request.method != "POST":
        return redirect("crm:patients_list")

    last_name = request.POST.get("last_name", "").strip()
    first_name = request.POST.get("first_name", "").strip()
    phone = request.POST.get("phone", "").strip()

    if not last_name or not first_name or not phone:
        messages.error(request, "Фамилия, имя и телефон обязательны.")
        return redirect("crm:patients_list")

    if Patient.objects.filter(phone=phone).exists():
        messages.error(request, f"Пациент с телефоном «{phone}» уже существует.")
        return redirect("crm:patients_list")

    Patient.objects.create(
        last_name=last_name,
        first_name=first_name,
        middle_name=request.POST.get("middle_name", "").strip(),
        phone=phone,
        birth_date=parse_date(request.POST.get("birth_date", "")) or None,
        comment=request.POST.get("comment", "").strip(),
    )
    messages.success(request, f"Пациент {last_name} {first_name} создан.")
    return redirect("crm:patients_list")

@login_required
def patient_update(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    if request.method != "POST":
        return redirect("crm:patients_list")

    phone = request.POST.get("phone", patient.phone).strip()
    if Patient.objects.filter(phone=phone).exclude(pk=patient.pk).exists():
        messages.error(request, f"Телефон «{phone}» уже занят другим пациентом.")
        return redirect("crm:patients_list")

    patient.last_name = request.POST.get("last_name", patient.last_name).strip()
    patient.first_name = request.POST.get("first_name", patient.first_name).strip()
    patient.middle_name = request.POST.get("middle_name", "").strip()
    patient.phone = phone
    patient.birth_date = parse_date(request.POST.get("birth_date", "")) or None
    patient.comment = request.POST.get("comment", "").strip()
    patient.save()

    messages.success(request, "Данные пациента обновлены.")
    return redirect("crm:patients_list")

@manager_required
def patient_delete(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    if request.method != "POST":
        return redirect("crm:patients_list")

    # Если есть кейсы или планы — не удаляем, а деактивируем
    if patient.cases.exists() or patient.plans.exists():
        patient.is_active = False
        patient.save(update_fields=["is_active"])
        messages.warning(
            request,
            f"У пациента «{patient}» есть кейсы или планы — он деактивирован вместо удаления.",
        )
    else:
        name = str(patient)
        patient.delete()
        messages.success(request, f"Пациент «{name}» удалён.")

    return redirect("crm:patients_list")



from django.utils import timezone
from datetime import timedelta


def ensure_payment_tasks(case):
    """
    Для плана с авансом: если до платежа <= 1 дня и он не оплачен —
    создать задачу-напоминание куратору. Просроченные — с высоким приоритетом.
    Защита от дублей через Task.schedule_item.
    """
    plan = case.plan
    if plan.plan_type != TreatmentPlan.PlanType.WITH_ADVANCE:
        return

    today = timezone.now().date()
    horizon = today + timedelta(days=1)  # напоминаем за 1 день

    items = (
        plan.schedule.filter(due_date__lte=horizon)
        .exclude(paid_amount__gte=F("planned_amount"))
    )

    for item in items:
        already = Task.objects.filter(
            schedule_item=item, status=Task.TaskStatus.PENDING
        ).exists()
        if already:
            continue

        due_dt = timezone.make_aware(
            datetime.combine(item.due_date, dtime(10, 0))
        )
        Task.objects.create(
            case=case,
            task_type=Task.TaskType.PAYMENT_REMINDER,
            description=(
                f"Напомнить об оплате {item.planned_amount - item.paid_amount} ₽ "
                f"(плановая дата {item.due_date:%d.%m.%Y}"
                f"{', аванс' if item.is_advance else ''})"
            ),
            due_date=due_dt,
            assignee=case.curator,
            priority=Task.Priority.HIGH if item.is_overdue else Task.Priority.MEDIUM,
            schedule_item=item,
        )

@login_required
def plan_directions_update(request, plan_id):
    plan = get_object_or_404(TreatmentPlan, id=plan_id)
    if request.method != "POST":
        return redirect("crm:case_detail", case_id=plan.cases.first().id)

    changes = []
    with transaction.atomic():
        for direction_value, _ in Direction.choices:
            amount = _to_decimal(request.POST.get(f"direction_{direction_value}", 0))
            obj, _ = PlanDirection.objects.update_or_create(
                plan=plan, direction=direction_value, defaults={"amount": amount}
            )
            changes.append(f"{obj.get_direction_display()}: {amount}")

    log_action(
        action=AuditLog.Action.UPDATE,
        instance=plan,
        field_name="directions",
        new_value="; ".join(changes),
        comment="Обновлены суммы по направлениям",
    )
    messages.success(request, "Направления сохранены.")
    return redirect("crm:case_detail", case_id=plan.cases.first().id)

@login_required
def schedule_item_create(request, plan_id):
    plan = get_object_or_404(TreatmentPlan, id=plan_id)
    if request.method != "POST":
        return redirect("crm:case_detail", case_id=plan.cases.first().id)

    due_date = parse_date(request.POST.get("due_date", ""))
    amount = _to_decimal(request.POST.get("planned_amount", 0))
    is_advance = request.POST.get("is_advance") == "on"

    if not due_date or amount <= 0:
        messages.error(request, "Укажите дату и сумму пункта графика.")
        return redirect("crm:case_detail", case_id=plan.cases.first().id)

    item = PaymentSchedule.objects.create(
        plan=plan,
        due_date=due_date,
        planned_amount=amount,
        is_advance=is_advance,
        comment=request.POST.get("comment", "").strip(),
    )

    log_action(
        action=AuditLog.Action.CREATE, instance=item,
        field_name="schedule_item",
        new_value=(
            f"{item.planned_amount} ₽ на {item.due_date:%d.%m.%Y}"
            f"{' (аванс)' if item.is_advance else ''}"
        ),
        amount=item.planned_amount,
        comment="Пункт графика платежей",
    )
    messages.success(request, "Пункт графика добавлен.")
    return redirect("crm:case_detail", case_id=plan.cases.first().id)


@login_required
def schedule_sign(request, plan_id):
    plan = get_object_or_404(TreatmentPlan, id=plan_id)
    case_id = plan.cases.first().id if plan.cases.exists() else None

    if request.method == "POST":
        if not plan.schedule.exists():
            messages.error(request, "График пуст — сначала добавьте пункты.")
            return redirect("crm:case_detail", case_id=case_id)

        # ПРАВИЛО: сумма графика обязана равняться согласованной сумме
        if plan.schedule_mismatch:
            messages.error(
                request,
                f"Нельзя подписать график: сумма пунктов ({plan.schedule_total} ₽) "
                f"не равна согласованной сумме ({plan.agreed_sum} ₽).",
            )
            return redirect("crm:case_detail", case_id=case_id)

        plan.is_schedule_signed = True
        plan.save(update_fields=["is_schedule_signed"])
        log_action(
            action=AuditLog.Action.UPDATE, instance=plan, field_name="is_schedule_signed",
            new_value="Подписан", comment="График платежей подписан",
        )
        messages.success(request, "График подписан.")
    return redirect("crm:case_detail", case_id=case_id)

@login_required
def plan_sign(request, plan_id):
    """Подписать план пациентом (отдельная кнопка, ТЗ п.5.3)."""
    plan = get_object_or_404(TreatmentPlan, id=plan_id)
    case_id = plan.cases.first().id if plan.cases.exists() else None

    if request.method == "POST":
        plan.is_signed_by_patient = True
        plan.save(update_fields=["is_signed_by_patient"])
        log_action(
            action=AuditLog.Action.UPDATE, instance=plan,
            field_name="is_signed_by_patient", new_value="Подписан",
            comment="План подписан пациентом",
        )
        messages.success(request, "План отмечен как подписанный пациентом.")
    return redirect("crm:case_detail", case_id=case_id)

@login_required
def payment_create(request, plan_id):
    """Создать платёж по плану лечения."""
    plan = get_object_or_404(TreatmentPlan, id=plan_id)
    case_id = plan.cases.first().id if plan.cases.exists() else None
    back = _next_url(request) or reverse("crm:case_detail", kwargs={"case_id": case_id})

    if request.method != "POST":
        return redirect(back)

    amount = _to_decimal(request.POST.get("amount", 0))
    payment_date = parse_date(request.POST.get("payment_date", "")) or timezone.now().date()

    if amount <= 0:
        messages.error(request, "Сумма платежа должна быть больше нуля.")
        return redirect(back)

    payment = Payment.objects.create(
        plan=plan,
        amount=amount,
        payment_date=payment_date,
        payment_category=request.POST.get("payment_category", Payment.PaymentCategory.CARD),
        is_advance=request.POST.get("is_advance") == "on",
        schedule_item_id=request.POST.get("schedule_item_id") or None,
        comment=request.POST.get("comment", "").strip(),
    )

    log_action(
        action=AuditLog.Action.CREATE, instance=payment,
        field_name="payment",
        new_value=(
            f"{payment.amount} ₽ от {payment.payment_date:%d.%m.%Y}"
            f"{' (аванс)' if payment.is_advance else ''}"
            f" · {payment.get_payment_category_display()}"
        ),
        amount=payment.amount,
        comment="Внесён платёж",
    )

    # Если платёж закрывает пункт графика — обновляем
    if payment.schedule_item:
        item = payment.schedule_item
        item.refresh_from_db()  # пересчитать paid_amount
        # (свойство paid_amount на модели уже суммирует платежи)

    messages.success(request, f"Платёж на {amount} ₽ внесён.")
    return redirect(back)




def compute_k1(curator, year, month):
    """Сумма согласованных сумм планов куратора, согласованных в указанном месяце."""
    from django.db.models import Sum

    plan_ids = (
        CuratorCase.objects.filter(
            curator=curator,
            plan__agreement_date__year=year,
            plan__agreement_date__month=month,
        )
        .values_list("plan_id", flat=True)
        .distinct()
    )
    total = TreatmentPlan.objects.filter(id__in=plan_ids).aggregate(
        total=Sum("agreed_sum")
    )["total"]
    return total or 0

from .motivation import calculate_bonuses


@login_required
def motivation_page(request):
    """Личная страница мотивации. Куратор — свою; старший/управляющая — любого куратора."""
    today = timezone.now().date()
    can_manage = request.user.role in LEAD_ROLES

    user_id = request.GET.get("user_id")
    if can_manage and user_id:
        target_user = get_object_or_404(CustomUser, id=user_id)
    else:
        # Куратор всегда видит только свою мотивацию, даже если подделал user_id
        target_user = request.user

    # Старший куратор и управляющая могут смотреть любого куратора
    if request.user.role in (
        CustomUser.Role.SENIOR_CURATOR, CustomUser.Role.MANAGER, CustomUser.Role.ADMIN
    ):
        user_id = request.GET.get("user_id")
        if user_id:
            target_user = get_object_or_404(CustomUser, id=user_id)

    monthly_plan = CuratorMonthlyPlan.objects.filter(
        curator=target_user, year=today.year, month=today.month
    ).first()

    breakdown_plans = []
    breakdown_orders = []
    if monthly_plan:
        breakdown_plans = TreatmentPlan.objects.filter(
            cases__curator=target_user,
            agreement_date__year=monthly_plan.year,
            agreement_date__month=monthly_plan.month,
        ).exclude(cases__status=CuratorCase.Status.LOST).distinct().select_related(
            "patient"
        ).order_by("agreement_date")

        breakdown_orders = WorkOrder.objects.filter(
            plan__cases__curator=target_user,
            closed_date__year=monthly_plan.year,
            closed_date__month=monthly_plan.month,
        ).select_related("plan__patient").order_by("closed_date")

    history = CuratorMonthlyPlan.objects.filter(curator=target_user).order_by("-year", "-month")[:12]

    # Список кураторов для переключателя (для руководителей)
    curators = CustomUser.objects.filter(
        role__in=[CustomUser.Role.CURATOR, CustomUser.Role.SENIOR_CURATOR]
    ).order_by("last_name")

    can_manage = request.user.role in (CustomUser.Role.MANAGER, CustomUser.Role.ADMIN)

    return render(request, "crm/motivation.html", {
        "title": "Мотивация",
        "target_user": target_user,
        "monthly_plan": monthly_plan,
        "history": history,
        "curators": curators,
        "is_own": target_user.id == request.user.id,
        "can_manage": can_manage,
        "breakdown_plans": breakdown_plans,
        "breakdown_orders": breakdown_orders,
        "schemes": MotivationScheme.objects.all(),
    })


@manager_required
def monthly_plan_upsert(request):
    """Управляющая задаёт/правит индивидуальный план куратора на месяц."""
    if request.method != "POST":
        return redirect("crm:motivation_page")

    curator_id = request.POST.get("curator_id")
    year = int(request.POST.get("year", timezone.now().year))
    month = int(request.POST.get("month", timezone.now().month))

    curator = get_object_or_404(CustomUser, id=curator_id)

    scheme_id = request.POST.get("scheme_id")
    scheme = get_object_or_404(MotivationScheme, id=scheme_id) if scheme_id else None
    if not scheme:
        messages.error(request, "Выберите модель мотивации — расчёт без модели не выполняется.")
        return redirect("crm:motivation")

    plan, created = CuratorMonthlyPlan.objects.update_or_create(
        curator=curator, year=year, month=month, scheme = scheme, 
        defaults={
            "plan_amount": _to_decimal(request.POST.get("plan_amount")),
            "adjustments": _to_decimal(request.POST.get("adjustments")),
            "workorders_amount": _to_decimal(request.POST.get("workorders_amount")),
            "workorders_adjustment": _to_decimal(request.POST.get("workorders_adjustment", 0)),
            "set_by": request.user,
            "work_days": int(request.POST.get("work_days", 0) or 0),
        },
    )

    log_action(
        action=AuditLog.Action.UPDATE if not created else AuditLog.Action.CREATE,
        instance=plan,
        field_name="monthly_plan",
        new_value=f"план {plan.plan_amount}, корр. {plan.adjustments}, з/н {plan.workorders_amount}",
        comment=f"Месячный план {curator} на {month:02d}.{year}",
    )
    messages.success(request, f"План для {curator} на {month:02d}.{year} сохранён.")
    return redirect(f"{reverse('crm:motivation_page')}?user_id={curator.id}")

@login_required
def contract_create(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    if request.method != "POST":
        return redirect("crm:case_detail", case_id=patient.cases.first().id if patient.cases.exists() else "crm:cases_list")

    number = request.POST.get("number", "").strip()
    date = parse_date(request.POST.get("date", ""))

    if not number or not date:
        messages.error(request, "Номер и дата договора обязательны.")
        return redirect("crm:case_detail", case_id=patient.cases.first().id)

    contract = Contract.objects.create(
        patient=patient,
        number=number,
        date=date,
        status=request.POST.get("status", Contract.Status.DRAFT),
        comment=request.POST.get("comment", "").strip(),
    )

    log_action(
        action=AuditLog.Action.CREATE, instance=contract,
        field_name="contract",
        new_value=f"№{contract.number} от {contract.date:%d.%m.%Y} ({contract.get_status_display()})",
        comment="Договор создан",
    )
    messages.success(request, f"Договор №{number} создан.")
    return redirect("crm:case_detail", case_id=patient.cases.first().id)


@login_required
def contract_update(request, contract_id):
    contract = get_object_or_404(Contract, id=contract_id)
    if request.method != "POST":
        return redirect("crm:case_detail", case_id=contract.patient.cases.first().id)
    old_values = {
            "number": contract.number,
            "date": contract.date,
            "status": contract.status,
        }
    old_status = contract.status
    contract.number = request.POST.get("number", contract.number).strip()
    contract.date = parse_date(request.POST.get("date", "")) or contract.date
    contract.status = request.POST.get("status", contract.status)
    contract.comment = request.POST.get("comment", "").strip()
    contract.save()


    for f, old in old_values.items():
        new = getattr(contract, f)
        if str(old) != str(new):
            log_action(
                    action=AuditLog.Action.UPDATE, instance=contract,
                    field_name=f,
                    old_value=old or "—", new_value=new or "—",
                    comment="Изменение договора",
                )

    messages.success(request, "Договор обновлён.")
    return redirect("crm:case_detail", case_id=contract.patient.cases.first().id)

@login_required
def schedule_item_update(request, item_id):
    item = get_object_or_404(PaymentSchedule.objects.select_related("plan"), id=item_id)
    plan = item.plan
    case_id = plan.cases.first().id if plan.cases.exists() else None

    if request.method != "POST":
        return redirect("crm:case_detail", case_id=case_id)

    old_due, old_amount = item.due_date, item.planned_amount

    due_date = parse_date(request.POST.get("due_date", ""))
    amount = _to_decimal(request.POST.get("planned_amount", 0))

    errors = []
    if not due_date:
        errors.append("Укажите дату платежа.")
    if amount <= 0:
        errors.append("Сумма должна быть больше нуля.")
    if amount < item.paid_amount:
        errors.append(f"Сумма не может быть меньше уже оплаченной ({item.paid_amount} ₽).")

    if errors:
        for e in errors:
            messages.error(request, e)
        return redirect("crm:case_detail", case_id=case_id)

    old = f"{item.due_date:%d.%m.%Y} · {item.planned_amount} ₽"
    item.due_date = due_date
    item.planned_amount = amount
    item.is_advance = request.POST.get("is_advance") == "on"
    item.comment = request.POST.get("comment", "").strip()
    item.save()

    if str(old_due) != str(item.due_date):
        log_action(
                action=AuditLog.Action.UPDATE, instance=item,
                field_name="due_date",
                old_value=f"{old_due:%d.%m.%Y}", new_value=f"{item.due_date:%d.%m.%Y}",
                comment="Пункт графика",
            )
    if old_amount != item.planned_amount:
        log_action(
                action=AuditLog.Action.UPDATE, instance=item,
                field_name="planned_amount",
                old_value=old_amount, new_value=item.planned_amount,
                amount=item.planned_amount,
                comment="Пункт графика",
            )
    messages.success(request, "Пункт графика обновлён.")
    return redirect("crm:case_detail", case_id=case_id)

@login_required
def schedule_item_delete(request, item_id):
    item = get_object_or_404(PaymentSchedule.objects.select_related("plan"), id=item_id)
    case = item.plan.cases.first()
    if case and not can_lead_case(case, request.user):
        messages.error(request, "Удалять пункты графика чужого КПЛ может только владелец или руководитель.")
        return redirect(_next_url(request) or reverse("crm:case_detail", kwargs={"case_id": case.id}))
    plan = item.plan
    case_id = plan.cases.first().id if plan.cases.exists() else None

    if request.method != "POST":
        return redirect("crm:case_detail", case_id=case_id)

    # ПРАВИЛО: пункт с привязанной оплатой удалять нельзя
    if item.has_payments or item.paid_amount > 0:
        messages.error(request, "Нельзя удалить пункт графика, к которому привязана оплата.")
        return redirect("crm:case_detail", case_id=case_id)

    old = f"{item.due_date:%d.%m.%Y} · {item.planned_amount} ₽"
    log_action(
        action=AuditLog.Action.DELETE, instance=item,
        field_name="schedule_item",
        old_value=f"{item.planned_amount} ₽ на {item.due_date:%d.%m.%Y}",
        amount=item.planned_amount,
        comment="Пункт графика удалён",
    )
    item.delete()
    item.delete()
    messages.success(request, "Пункт графика удалён.")
    return redirect("crm:case_detail", case_id=case_id)

@login_required
def schedule_generate(request, plan_id):
    plan = get_object_or_404(TreatmentPlan, id=plan_id)
    case_id = plan.cases.first().id if plan.cases.exists() else None

    if request.method != "POST":
        return redirect("crm:case_detail", case_id=case_id)

    total = _to_decimal(request.POST.get("total_amount", 0)) or plan.agreed_sum
    advance = _to_decimal(request.POST.get("advance_amount", 0))
    parts = int(request.POST.get("parts", 0) or 0)
    start_date = parse_date(request.POST.get("start_date", "")) or timezone.now().date()

    errors = []
    if total <= 0:
        errors.append("Укажите сумму графика.")
    if parts < 1:
        errors.append("Количество платежей — минимум 1.")
    if advance < 0 or advance >= total:
        errors.append("Аванс должен быть больше нуля и меньше суммы графика.")
    if Payment.objects.filter(schedule_item__plan=plan).exists():
        errors.append("Нельзя перегенерировать график: к пунктам уже привязаны оплаты.")

    if errors:
        for e in errors:
            messages.error(request, e)
        return redirect("crm:case_detail", case_id=case_id)

    with transaction.atomic():
        plan.schedule.all().delete()  # безопасно: оплат нет (проверено выше)
        items = []
        offset = 0
        if advance > 0:
            # Первый пункт — аванс, автоматически
            items.append(PaymentSchedule(
                plan=plan, due_date=start_date, planned_amount=advance, is_advance=True,
                comment="Аванс",
            ))
            offset = 1
        remainder = total - advance
        base = (remainder / parts).quantize(Decimal("0.01"))
        for i in range(parts):
            # Хвост копеек кладём в последний платёж — сумма сходится копейка в копейку
            amount = base if i < parts - 1 else (remainder - base * (parts - 1))
            items.append(PaymentSchedule(
                plan=plan, due_date=add_months(start_date, i + offset),
                planned_amount=amount, is_advance=False,
            ))
        PaymentSchedule.objects.bulk_create(items)

    log_action(
        action=AuditLog.Action.CREATE, instance=plan, field_name="schedule",
        new_value=f"Сгенерирован: аванс {advance} ₽ + {parts} платежей, итого {total} ₽",
        comment="График платежей сгенерирован автоматически",
    )
    messages.success(request, "График платежей сгенерирован.")
    return redirect("crm:case_detail", case_id=case_id)


def months_in_range(start_date, end_date):
    """Список (год, месяц), попадающих в период отчёта."""
    months = []
    y, m = start_date.year, start_date.month
    while (y, m) <= (end_date.year, end_date.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months

from django.db.models import Min

@manager_required
def manager_report(request):
    now = timezone.now()
    today = now.date()
    period = request.GET.get("period", "month")
    start, end = _resolve_period(request, today)

    curator_id = request.GET.get("curator", "")
    status_filter = request.GET.get("status", "")

    curators = CustomUser.objects.filter(role=CustomUser.Role.CURATOR).order_by("last_name", "first_name")

    rows = [("ОТДЕЛ", compute_curator_metrics(None, start, end, today, now))]
    for u in (curators.filter(pk=curator_id) if curator_id else curators):
        rows.append((u.get_full_name() or u.username, compute_curator_metrics(u, start, end, today, now)))

    # Детализация сопровождений за период
    cases_qs = CuratorCase.objects.filter(curator__role=CustomUser.Role.CURATOR)
    if curator_id:
        cases_qs = cases_qs.filter(curator_id=curator_id)
    cohort = cases_qs.filter(created_at__date__range=(start, end))
    if status_filter:
        cohort = cohort.filter(status=status_filter)

    detail_rows = []
    for c in cohort.select_related("patient", "curator", "plan").order_by("-created_at")[:100]:
        paid = Payment.objects.filter(plan=c.plan).aggregate(s=Coalesce(Sum("amount"), Decimal("0")))["s"]
        detail_rows.append({
            "case": c,
            "paid": paid,
            "remainder": Decimal("0") if c.status == CuratorCase.Status.LOST
                         else (c.plan.agreed_sum or Decimal("0")) - paid,
            "next_task": c.tasks.filter(status=Task.TaskStatus.PENDING).order_by("due_date").first(),
        })

    return render(request, "crm/manager_report.html", {
        "title": "Отчёт руководителя",
        "period": period, "start": start, "end": end,
        "curator_id": curator_id, "status_filter": status_filter,
        "curators": curators, "statuses": CuratorCase.Status.choices,
        "rows": rows, "detail_rows": detail_rows,
    })


@login_required
def day_plan(request):
    """План дня: расписание 9:00–18:00 по задачам ВСЕГО отдела (подмена на смене)."""
    now = timezone.now()
    today = now.date()
    mine_only = request.GET.get("mine") == "1"

    base = Task.objects.select_related("case", "case__patient", "assignee")
    if mine_only:
        base = base.filter(assignee=request.user)

    # В сетку: просроченные + сегодняшние (открытые) и сделанные сегодня
    grid_tasks = sorted(
        list(base.filter(status=Task.TaskStatus.PENDING, due_date__date__lte=today))
        + list(base.filter(status=Task.TaskStatus.DONE, due_date__date=today)),
        key=lambda t: t.due_date,
    )

    upcoming = base.filter(
        status=Task.TaskStatus.PENDING,
        due_date__date__range=(today + timedelta(days=1), today + timedelta(days=7)),
    ).order_by("due_date")[:8]

    slots = []
    for m in range(9 * 60, 18 * 60, 30):
        slots.append({"time": f"{m // 60:02d}:{m % 60:02d}", "tasks": []})

    outside = []
    for t in grid_tasks:
        local = timezone.localtime(t.due_date)
        mins = local.hour * 60 + local.minute
        if 9 * 60 <= mins < 18 * 60:
            slots[(mins - 9 * 60) // 30]["tasks"].append(t)
        else:
            outside.append(t)

    not_done = sum(1 for t in grid_tasks if t.status == Task.TaskStatus.PENDING)
    done_count = len(grid_tasks) - not_done

    return render(request, "crm/day_plan.html", {
        "title": "План дня",
        "slots": slots,
        "outside": outside,
        "upcoming": upcoming,
        "not_done": not_done,
        "done_count": done_count,
        "today": today,
        "mine_only": mine_only,
        "task_types": Task.TaskType.choices,
        "overdue_count": sum(
            1 for t in grid_tasks
            if t.status == Task.TaskStatus.PENDING and t.is_overdue
        ),
        "today_count": sum(
            1 for t in grid_tasks
            if t.status == Task.TaskStatus.PENDING and not t.is_overdue
        ),
        "upcoming_count": upcoming.count(),
    })


ACTIVE_STATUSES = [
    CuratorCase.Status.TRANSFERRED, CuratorCase.Status.PRESENTED,
    CuratorCase.Status.IN_DECISION, CuratorCase.Status.AGREED,
    CuratorCase.Status.IN_PROGRESS,
]

WD = ["ПОНЕДЕЛЬНИК", "ВТОРНИК", "СРЕДА", "ЧЕТВЕРГ", "ПЯТНИЦА", "СУББОТА", "ВОСКРЕСЕНЬЕ"]
MN = ["ЯНВАРЯ", "ФЕВРАЛЯ", "МАРТА", "АПРЕЛЯ", "МАЯ", "ИЮНЯ",
      "ИЮЛЯ", "АВГУСТА", "СЕНТЯБРЯ", "ОКТЯБРЯ", "НОЯБРЯ", "ДЕКАБРЯ"]


def docs_state(case):
    """Состояние документов по кейсу (колонка «Документы»)."""
    p = case.plan
    if not p.is_signed_by_patient:
        return "Нет подписи плана"
    if p.plan_type == TreatmentPlan.PlanType.WITH_ADVANCE and not p.schedule.exists():
        return "Нет графика платежей"
    if p.plan_type == TreatmentPlan.PlanType.WITH_ADVANCE and not p.is_schedule_signed:
        return "Нет подписи графика"
    return "План и график подписаны"


@login_required
def home(request):
    """Рабочий стол: кабинет куратора или руководителя (макет v2)."""
    now = timezone.now()
    today = now.date()
    is_manager = request.user.role in (
        CustomUser.Role.MANAGER, CustomUser.Role.ADMIN, CustomUser.Role.SENIOR_CURATOR
    )
    ctx = {
        "title": "Рабочий стол",
        "today_ru": f"{WD[today.weekday()]}, {today.day} {MN[today.month - 1]}",
        "is_manager": is_manager,
    }

    if not is_manager:
        my_pending = Task.objects.filter(assignee=request.user, status=Task.TaskStatus.PENDING)
        day_tasks = list(my_pending.filter(due_date__date__lt=today).order_by("due_date")) \
            + list(my_pending.filter(due_date__date=today).order_by("due_date"))
        active = CuratorCase.objects.filter(curator=request.user, status__in=ACTIVE_STATUSES)
        with_next = active.annotate(
            f=Count("tasks", filter=Q(tasks__status=Task.TaskStatus.PENDING, tasks__due_date__date__gte=today))
        ).filter(f__gt=0).count()
        mp = CuratorMonthlyPlan.objects.filter(curator=request.user, year=today.year, month=today.month).first()
        month_agreed = TreatmentPlan.objects.filter(
            cases__curator=request.user,
            agreement_date__year=today.year, agreement_date__month=today.month,
        ).exclude(cases__status=CuratorCase.Status.LOST).distinct().aggregate(
            s=Coalesce(Sum("agreed_sum"), Decimal("0"))
        )["s"]
        od_items = PaymentSchedule.objects.filter(
            plan__cases__curator=request.user, due_date__lt=today
        ).exclude(paid_amount__gte=F("planned_amount")).distinct()
        od_pay_sum = sum((i.remaining for i in od_items), Decimal("0"))

        ctx.update({
            "day_tasks": day_tasks[:5],
            "tasks_today_count": my_pending.filter(due_date__date=today).count(),
            "overdue_count": my_pending.filter(due_date__lt=now).count(),
            "active_cases": active.count(),
            "with_next": with_next,
            "no_next_count": active.count() - with_next,
            "monthly_plan": mp,
            "month_agreed": month_agreed,
            "month_percent": round(month_agreed / mp.plan_amount * 100) if mp and mp.plan_amount else 0,
            "progress_width": min(100, mp.completion_percent) if mp else 0,
            "od_pay_sum": od_pay_sum,
        })
    else:
        cases = CuratorCase.objects.filter(curator__role=CustomUser.Role.CURATOR)
        cohort = cases.filter(created_at__year=today.year, created_at__month=today.month)
        stage_order = [
            (CuratorCase.Status.TRANSFERRED, "Передано"),
            (CuratorCase.Status.PRESENTED, "Презентовано"),
            (CuratorCase.Status.AGREED, "Согласовано"),
            (CuratorCase.Status.IN_PROGRESS, "В лечении"),
            (CuratorCase.Status.COMPLETED, "Завершено"),
        ]
        counts = [cohort.filter(status=s).count() for s, _ in stage_order]
        base = max(counts) or 1
        stages = [
            {"label": label, "count": c, "width": min(100, round(c / base * 100))}
            for c, (s, label) in zip(counts, stage_order)
        ]
        pres = cohort.filter(status__in=[CuratorCase.Status.PRESENTED, CuratorCase.Status.IN_DECISION,
                                         CuratorCase.Status.AGREED, CuratorCase.Status.IN_PROGRESS,
                                         CuratorCase.Status.COMPLETED]).count()
        agr = cohort.filter(status__in=[CuratorCase.Status.AGREED, CuratorCase.Status.IN_PROGRESS,
                                        CuratorCase.Status.COMPLETED]).count()
        od_tasks = Task.objects.filter(case__in=cases, status=Task.TaskStatus.PENDING, due_date__lt=now)
        pending_checks = cases.filter(status__in=[CuratorCase.Status.AGREED, CuratorCase.Status.IN_PROGRESS]) \
            .annotate(nc=Count("document_checks")).filter(nc=0).count()
        curator_rows = []
        for u in CustomUser.objects.filter(role=CustomUser.Role.CURATOR).order_by("last_name"):
            ump = CuratorMonthlyPlan.objects.filter(curator=u, year=today.year, month=today.month).first()
            fact = compute_k1(u, today.year, today.month)
            curator_rows.append({
                "user": u,
                "plan": ump.plan_amount if ump else Decimal("0"),
                "fact": fact,
                "percent": round(fact / ump.plan_amount * 100) if ump and ump.plan_amount else 0,
                "overdue": Task.objects.filter(case__curator=u, status=Task.TaskStatus.PENDING, due_date__lt=now).count(),
            })
        
        deviations = collect_deviations()
        deviation_summary = [
            (label, len(deviations[key])) for key, label in DEVIATION_LABELS
        ]
        ctx.update({"deviation_summary": deviation_summary})

        ctx.update({
            "stages": stages,
            "conv": round(agr / pres * 100) if pres else 0,
            "od_tasks_count": od_tasks.count(),
            "od_tasks_curators": od_tasks.values("case__curator").distinct().count(),
            "pending_checks": pending_checks,
            "curator_rows": curator_rows,
        })
    return render(request, "crm/home.html", ctx)


@login_required
def patients_board(request):
    """Пациенты: сопровождения с этапом и суммой (макет v2)."""
    cases = CuratorCase.objects.select_related("patient", "plan", "curator")
    if request.user.role == CustomUser.Role.CURATOR:
        cases = cases.filter(curator=request.user)
    ctx = {
        "title": "Пациенты",
        "cases": cases.order_by("-created_at")[:100],
        "is_manager": request.user.role in (CustomUser.Role.MANAGER, CustomUser.Role.ADMIN),
    }
    return render(request, "crm/patients_board.html", ctx)


@login_required
def doc_checks(request):
    """Проверка документов (макет v2, реестр ТЗ п.12)."""
    cases = CuratorCase.objects.filter(
        status__in=[CuratorCase.Status.AGREED, CuratorCase.Status.IN_PROGRESS]
    ).select_related("patient", "plan", "curator").prefetch_related("document_checks")
    if request.user.role == CustomUser.Role.CURATOR:
        cases = cases.filter(curator=request.user)

    rows = []
    for c in cases:
        rows.append({
            "case": c,
            "docs": docs_state(c),
            "check": c.document_checks.first(),
        })
    return render(request, "crm/doc_checks.html", {
        "title": "Проверка документов",
        "rows": rows,
        "can_check": request.user.role in (CustomUser.Role.MANAGER, CustomUser.Role.ADMIN),
        "check_statuses": DocumentCheck.Status.choices,
        "discrepancies": DocumentCheck.Discrepancy.choices,
    })


@manager_required
def doc_check_create(request):
    """Внести проверку; расхождение порождает задачу куратору (ТЗ п.18)."""
    if request.method != "POST":
        return redirect("crm:doc_checks")

    case = get_object_or_404(CuratorCase, id=request.POST.get("case_id"))
    status = request.POST.get("status", DocumentCheck.Status.NOT_CHECKED)
    check = DocumentCheck.objects.create(
        case=case,
        status=status,
        discrepancy_type=request.POST.get("discrepancy_type", "") if status == DocumentCheck.Status.DISCREPANCY else "",
        comment=request.POST.get("comment", "").strip(),
        checked_by=request.user,
    )
    log_action(
        action=AuditLog.Action.CREATE, instance=check,
        field_name="status",
        new_value=check.get_status_display()
                  + (f": {check.get_discrepancy_type_display()}" if check.discrepancy_type else ""),
        comment="Проверка документов",
    )

    if status == DocumentCheck.Status.DISCREPANCY and case.curator:
        Task.objects.create(
            case=case,
            task_type=Task.TaskType.DOCUMENTS,
            description=f"Устранить расхождение: {check.get_discrepancy_type_display() or 'см. комментарий'}",
            due_date=timezone.now() + timedelta(days=1),
            assignee=case.curator,
            priority=Task.Priority.HIGH,
        )
        messages.warning(request, "Расхождение зафиксировано, куратору создана задача.")
    else:
        messages.success(request, "Проверка сохранена.")
    return redirect("crm:doc_checks")


@login_required
def case_next_visit(request, case_id):
    """Дата и комментарий следующего визита по КПЛ."""
    case = get_object_or_404(CuratorCase, id=case_id)
    touch_case(case)
    if request.method == "POST":
        old = f"{case.next_control_date or '—'} | {case.next_visit_comment or '—'}"
        case.next_control_date = parse_date(request.POST.get("next_control_date", "")) or case.next_control_date
        case.next_visit_comment = request.POST.get("next_visit_comment", "").strip()
        case.save(update_fields=["next_control_date", "next_visit_comment"])
        log_action(
            action=AuditLog.Action.UPDATE, instance=case,
            field_name="next_visit", old_value=old,
            new_value=f"{case.next_control_date or '—'} | {case.next_visit_comment or '—'}",
            comment="Следующий визит: дата и комментарий",
        )
        messages.success(request, "Следующий визит сохранён.")
    return redirect(_next_url(request) or reverse("crm:case_detail", kwargs={"case_id": case.id}))


@login_required
def work_order_create(request, plan_id):
    """Закрыть наряд на лечение (независимо от платежей)."""
    plan = get_object_or_404(TreatmentPlan, id=plan_id)
    case_id = plan.cases.first().id if plan.cases.exists() else None
    back = _next_url(request) or reverse("crm:case_detail", kwargs={"case_id": case_id})

    if request.method != "POST":
        return redirect(back)

    amount = _to_decimal(request.POST.get("amount", 0))
    closed_date = parse_date(request.POST.get("closed_date", "")) or timezone.now().date()

    if amount <= 0:
        messages.error(request, "Сумма наряда должна быть больше нуля.")
        return redirect(back)

    wo = None
    for _ in range(5):
        try:
            wo = WorkOrder.objects.create(
                plan=plan,
                number=next_workorder_number(closed_date),
                amount=amount,
                closed_date=closed_date,
                comment=request.POST.get("comment", "").strip(),
                created_by=request.user,
            )
            break
        except IntegrityError:
            continue
    if wo is None:
        messages.error(request, "Не удалось присвоить номер наряда, попробуйте ещё раз.")
        return redirect(back)

    log_action(
        action=AuditLog.Action.CREATE, instance=wo,
        field_name="work_order",
        new_value=f"№{wo.number}, {wo.amount} ₽ от {wo.closed_date:%d.%m.%Y}",
        amount=wo.amount,
        comment="Закрыт наряд на лечение",
    )
    messages.success(request, f"Наряд №{wo.number} на {amount} ₽ закрыт.")
    return redirect(back)


@manager_required
def work_order_delete(request, wo_id):
    """Удалить ошибочный наряд (только управляющая/админ, с аудитом)."""
    wo = get_object_or_404(WorkOrder.objects.select_related("plan"), id=wo_id)
    case = wo.plan.cases.first()
    if case and not can_lead_case(case, request.user):
        messages.error(request, "Удалять наряды чужого КПЛ может только владелец или руководитель.")
        return redirect(_next_url(request) or reverse("crm:case_detail", kwargs={"case_id": case.id}))
    plan = wo.plan
    case_id = plan.cases.first().id if plan.cases.exists() else None
    back = _next_url(request) or reverse("crm:case_detail", kwargs={"case_id": case_id})

    if request.method == "POST":
        old = f"{wo.amount} ₽ от {wo.closed_date:%d.%m.%Y}"
        log_action(
            action=AuditLog.Action.DELETE, instance=wo,
            field_name="work_order",
            old_value=f"№{wo.number}, {wo.amount} ₽ от {wo.closed_date:%d.%m.%Y}",
            amount=wo.amount,
            comment="Наряд удалён",
        )
        wo.delete()
        messages.success(request, "Наряд удалён.")
    return redirect(back)


def next_workorder_number(d):
    """Авто-нумерация нарядов: Н-<год>-<порядковый>. Позже номер может приходить из iDent."""
    prefix = f"Н-{d.year}-"
    last = WorkOrder.objects.filter(number__startswith=prefix).order_by("-number").first()
    if last:
        try:
            n = int(last.number[len(prefix):]) + 1
        except ValueError:
            n = WorkOrder.objects.filter(number__startswith=prefix).count() + 1
    else:
        n = 1
    return f"{prefix}{n:04d}"




@login_required
def patient_detail(request, patient_id):
    """Карточка пациента: связанные объекты и история."""
    patient = get_object_or_404(Patient, id=patient_id)
    
    # Связанные объекты
    contracts = patient.contracts.order_by("-date", "-id")
    plans = TreatmentPlan.objects.filter(patient=patient).select_related("doctor", "contract").order_by("-id")
    cases = CuratorCase.objects.filter(plan__patient=patient).select_related("curator").order_by("-created_at")
    
    # История из AuditLog (последние 50 записей по пациенту)
    patient_logs = AuditLog.objects.filter(patient=patient).select_related("actor", "owner")[:100]
    
    return render(request, "crm/patient_detail.html", {
        "title": f"{patient.last_name} {patient.first_name}",
        "patient": patient,
        "contracts": contracts,
        "plans": plans,
        "cases": cases,
        "audit_logs": patient_logs,
    })


def touch_case(case):
    """Отметить активность по КПЛ (ТЗ гл.6, контроль бездействия)."""
    CuratorCase.objects.filter(pk=case.pk).update(last_activity_at=timezone.now())


DEVIATION_LABELS = [
    ("no_next_task", "Нет следующей задачи"),
    ("overdue_tasks", "Просроченные задачи"),
    ("overdue_payments", "Просроченные платежи по графику"),
    ("no_activity", "Нет активности по КПЛ"),
    ("long_stage", "Долгое нахождение на этапе"),
    ("agreed_no_start", "Согласованный план без старта лечения"),
    ("stopped_treatment", "Остановленное лечение"),
    ("no_next_visit", "Нет следующего визита"),
    ("doc_discrepancy", "Расхождения документов"),
]


def collect_deviations(curator=None):
    """ТЗ гл.6: автоматический контроль отклонений по активным КПЛ."""
    now = timezone.now()
    today = now.date()
    t_inact = get_threshold("INACTIVITY_DAYS")
    t_stage = get_threshold("STAGE_DAYS")
    t_agreed = get_threshold("AGREED_NO_START_DAYS")
    t_stop = get_threshold("STOPPED_TREATMENT_DAYS")
    t_visit = get_threshold("NEXT_VISIT_OVERDUE_DAYS")
    t_pay = get_threshold("PAYMENT_OVERDUE_DAYS")

    qs = CuratorCase.objects.filter(status__in=ACTIVE_STATUSES).select_related(
        "curator", "plan", "patient"
    )
    if curator:
        qs = qs.filter(curator=curator)

    dev = {key: [] for key, _ in DEVIATION_LABELS}

    for case in qs:
        has_future = case.tasks.filter(
            status=Task.TaskStatus.PENDING, due_date__date__gte=today
        ).exists()

        if not has_future:
            dev["no_next_task"].append(case)
        if case.tasks.filter(status=Task.TaskStatus.PENDING, due_date__lt=now).exists():
            dev["overdue_tasks"].append(case)
        if any(
            item.is_overdue and (today - item.due_date).days >= t_pay
            for item in case.plan.schedule.all()
        ):
            dev["overdue_payments"].append(case)
        if case.last_activity_at and (now - case.last_activity_at).days >= t_inact:
            dev["no_activity"].append(case)
        if case.status_changed_at and (now - case.status_changed_at).days >= t_stage:
            dev["long_stage"].append(case)
        if (
            case.status == CuratorCase.Status.AGREED
            and case.plan.agreement_date
            and (today - case.plan.agreement_date).days >= t_agreed
        ):
            dev["agreed_no_start"].append(case)
        if (
            case.status == CuratorCase.Status.IN_PROGRESS
            and not has_future
            and case.status_changed_at
            and (now - case.status_changed_at).days >= t_stop
        ):
            dev["stopped_treatment"].append(case)
        if case.status == CuratorCase.Status.IN_PROGRESS and (
            case.next_control_date is None
            or case.next_control_date < today - timedelta(days=t_visit)
        ):
            dev["no_next_visit"].append(case)
        last_check = case.document_checks.first()
        if docs_state(case) != "План и график подписаны" or (
            last_check and last_check.status == DocumentCheck.Status.DISCREPANCY
        ):
            dev["doc_discrepancy"].append(case)

    return dev


@login_required
def settings_view(request):
    if request.user.role != CustomUser.Role.ADMIN:
        messages.error(request, "Раздел доступен только администратору.")
        return redirect("crm:home")

    for key, value, desc in DEFAULT_THRESHOLDS:
        get_threshold(key)  # гарантирует существование строк

    if request.method == "POST":
        for row in SystemSetting.objects.all():
            raw = request.POST.get(row.key)
            if raw is not None and raw.isdigit():
                old = row.value
                row.value = int(raw)
                row.save(update_fields=["value"])
                if old != row.value:
                    log_action(
                        action=AuditLog.Action.UPDATE, instance=row,
                        field_name=row.key, old_value=str(old), new_value=str(row.value),
                        comment="Изменение пороговой настройки",
                    )
        messages.success(request, "Пороговые настройки сохранены.")
        return redirect("crm:settings")

    return render(request, "crm/settings.html", {
        "title": "Настройки",
        "settings_rows": SystemSetting.objects.order_by("key"),
    })

@manager_required
def motivation_schemes(request):
    """Реестр моделей мотивации: ставки и даты действия (ТЗ гл.8)."""
    if request.method == "POST":
        scheme = MotivationScheme.objects.create(
            name=request.POST.get("name", "").strip() or "Модель без названия",
            valid_from=parse_date(request.POST.get("valid_from", "")) or timezone.now().date(),
            bonus1_rate=_to_decimal(request.POST.get("bonus1_rate", "0.007"), 4),
            bonus2_rate=_to_decimal(request.POST.get("bonus2_rate", "0.002"), 4),
            under100_penalty_rate=_to_decimal(request.POST.get("under100_penalty_rate", "1"), 2),
            over110_bonus_rate=_to_decimal(request.POST.get("over110_bonus_rate", "0.2"), 2),
            threshold_mid=_to_decimal(request.POST.get("threshold_mid", "100"), 2),
            threshold_high=_to_decimal(request.POST.get("threshold_high", "110"), 2),
            daily_rate=_to_decimal(request.POST.get("daily_rate", "1000"), 2),
            comment=request.POST.get("comment", "").strip(),
        )
        log_action(
            action=AuditLog.Action.CREATE, instance=scheme,
            field_name="scheme", new_value=str(scheme),
            comment="Создана модель мотивации",
        )
        messages.success(request, f"Модель «{scheme.name}» создана.")
        return redirect("crm:motivation_schemes")

    return render(request, "crm/motivation_schemes.html", {
        "title": "Модели мотивации",
        "schemes": MotivationScheme.objects.all(),
    })

@login_required
def case_reassign(request, case_id):
    if request.user.role not in LEAD_ROLES:
        messages.error(request, "Перераспределять сопровождение может старший куратор и выше.")
        return redirect(_next_url(request) or reverse("crm:case_detail", kwargs={"case_id": case_id}))
    case = get_object_or_404(CuratorCase, id=case_id)
    if request.method == "POST":
        new_curator = get_object_or_404(CustomUser, id=request.POST.get("curator_id"))
        old = case.curator
        if old != new_curator:
            case.curator = new_curator
            case.save(update_fields=["curator"])
            log_action(AuditLog.Action.REASSIGN, case, field_name="curator",
                       old_value=str(old), new_value=str(new_curator),
                       comment="Смена ответственного куратора")
            messages.success(request, f"Ответственный изменён: {new_curator}.")
    return redirect(_next_url(request) or reverse("crm:case_detail", kwargs={"case_id": case.id}))



@login_required
def task_next_create(request, task_id):
    """Быстрое создание следующей задачи по тому же КПЛ (ТЗ гл.4)."""
    task = get_object_or_404(
        Task.objects.select_related("case", "case__curator", "assignee"), id=task_id
    )
    case = task.case
    back = _next_url(request) or reverse("crm:case_detail", kwargs={"case_id": task.case_id})

    if request.method == "POST" and case:
        next_task = Task.objects.create(
            case=case,
            task_type=request.POST.get("task_type") or Task.TaskType.CONTACT,
            description=(
                request.POST.get("description", "").strip()
                or f"Следующий шаг после: {task.description or task.get_task_type_display()}"
            ),
            due_date=_parse_dt(request.POST.get("due_date", "")) or (timezone.now() + timedelta(days=1)),
            assignee_id=request.POST.get("assignee_id") or task.assignee_id or case.curator_id,
            priority=task.priority,
        )
        log_action(
            action=AuditLog.Action.CREATE, instance=next_task,
            field_name="task",
            new_value=f"{next_task.get_task_type_display()} · {next_task.due_date:%d.%m.%Y %H:%M}",
            comment="Создана следующая задача (быстрое действие)",
        )
        messages.success(request, "Следующая задача создана на завтра.")
    return redirect(back)


@login_required
def registers(request):
    """Реестры: платежи, календарь, наряды, договоры, презентации/согласования, приёмы, корректировки.
    Все периоды и статистика — по ДАТЕ СОБЫТИЯ, агрегации без ограниченных диапазонов."""
    now = timezone.now()
    today = now.date()
    period = request.GET.get("period", "month")

    if period == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif period == "custom":
        start = parse_date(request.GET.get("from", "")) or today.replace(day=1)
        end = parse_date(request.GET.get("to", "")) or today
    else:
        start = today.replace(day=1)
        end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    curator_id = request.GET.get("curator", "")
    cases = CuratorCase.objects.all()
    if request.user.role == CustomUser.Role.CURATOR:
        cases = cases.filter(curator=request.user)
    elif curator_id:
        cases = cases.filter(curator_id=curator_id)
    cases = cases.select_related("patient", "curator", "plan")

    plan_ids = TreatmentPlan.objects.filter(cases__in=cases).distinct().values_list("id", flat=True)
    patient_ids = cases.values_list("patient_id", flat=True)

    # ---- Реестры за выбранный период (по дате события) ----
    payments = Payment.objects.filter(plan_id__in=plan_ids, payment_date__range=(start, end)).select_related("plan", "plan__patient")
    schedule = PaymentSchedule.objects.filter(plan_id__in=plan_ids, due_date__range=(start, end)).select_related("plan", "plan__patient")
    workorders = WorkOrder.objects.filter(plan_id__in=plan_ids, closed_date__range=(start, end)).select_related("plan", "plan__patient")
    contracts = Contract.objects.filter(patient_id__in=patient_ids, date__range=(start, end)).select_related("patient")
    presentations = TreatmentPlan.objects.filter(cases__in=cases, presentation_date__range=(start, end)).select_related("patient")
    agreements = TreatmentPlan.objects.filter(cases__in=cases, agreement_date__range=(start, end)).select_related("patient")
    visits = Visit.objects.filter(patient_id__in=patient_ids, visit_date__range=(start, end)).select_related("patient", "category", "case")
    adjustments = FinanceAdjustment.objects.filter(patient_id__in=patient_ids, event_date__range=(start, end)).select_related("patient", "case")

    # ---- Недельная и месячная статистика (независимо от выбранного периода) ----
    def stats(s, e):
        p = Payment.objects.filter(plan_id__in=plan_ids, payment_date__range=(s, e))
        w = WorkOrder.objects.filter(plan_id__in=plan_ids, closed_date__range=(s, e))
        a = FinanceAdjustment.objects.filter(patient_id__in=patient_ids, event_date__range=(s, e))
        v = Visit.objects.filter(patient_id__in=patient_ids, visit_date__range=(s, e))
        pr = TreatmentPlan.objects.filter(cases__in=cases, presentation_date__range=(s, e)).count()
        ag = TreatmentPlan.objects.filter(cases__in=cases, agreement_date__range=(s, e)).count()
        from django.db.models import Sum
        return {
            "paid": p.aggregate(s=Sum("amount"))["s"] or Decimal("0"),
            "advances": p.filter(is_advance=True).aggregate(s=Sum("amount"))["s"] or Decimal("0"),
            "workorders": w.aggregate(s=Sum("amount"))["s"] or Decimal("0"),
            "adjustments": a.aggregate(s=Sum("amount"))["s"] or Decimal("0"),
            "visits": v.count(),
            "presentations": pr,
            "agreements": ag,
        }

    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    week_stats = stats(week_start, week_start + timedelta(days=6))
    month_stats = stats(month_start, today)

    return render(request, "crm/registers.html", {
        "title": "Реестры",
        "period": period, "start": start, "end": end,
        "curator_id": curator_id,
        "curators": CustomUser.objects.filter(role=CustomUser.Role.CURATOR).order_by("last_name"),
        "payments": payments, "schedule": schedule, "workorders": workorders,
        "contracts": contracts, "presentations": presentations, "agreements": agreements,
        "visits": visits, "adjustments": adjustments,
        "week_stats": week_stats, "month_stats": month_stats,
        "visit_categories": VisitCategory.objects.filter(is_active=True),
        "adjustment_kinds": FinanceAdjustment.Kind.choices,
        "patients": Patient.objects.filter(id__in=patient_ids).order_by("last_name"),
        "cases_list": cases,
    })


@login_required
def visit_create(request):
    if request.method == "POST":
        case = CuratorCase.objects.filter(id=request.POST.get("case_id") or 0).first()
        patient = get_object_or_404(Patient, id=request.POST.get("patient_id"))
        visit = Visit.objects.create(
            patient=patient,
            case=case,
            visit_date=parse_date(request.POST.get("visit_date", "")) or timezone.now().date(),
            category_id=request.POST.get("category_id") or None,
            diagnosis=request.POST.get("diagnosis", "").strip(),
            comment=request.POST.get("comment", "").strip(),
            created_by=request.user,
        )
        log_action(AuditLog.Action.CREATE, visit, field_name="visit",
                   new_value=f"{visit.visit_date:%d.%m.%Y} · {visit.category or '—'}",
                   comment="Записан приём")
        messages.success(request, "Приём записан.")
    return redirect(_next_url(request) or reverse("crm:registers"))


@login_required
def adjustment_create(request):
    if request.method == "POST":
        case = CuratorCase.objects.filter(id=request.POST.get("case_id") or 0).first()
        patient = get_object_or_404(Patient, id=request.POST.get("patient_id"))
        adj = FinanceAdjustment.objects.create(
            patient=patient,
            case=case,
            kind=request.POST.get("kind", FinanceAdjustment.Kind.OTHER),
            amount=_to_decimal(request.POST.get("amount", 0)),
            event_date=parse_date(request.POST.get("event_date", "")) or timezone.now().date(),
            comment=request.POST.get("comment", "").strip(),
            created_by=request.user,
        )
        log_action(AuditLog.Action.CREATE, adj, field_name="adjustment",
                   new_value=f"{adj.get_kind_display()} · {adj.amount} ₽",
                   amount=adj.amount, comment="Возврат / корректировка")
        messages.success(request, "Корректировка записана.")
    return redirect(_next_url(request) or reverse("crm:registers"))


def _period_months(start, end):
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def compute_curator_metrics(curator, start, end, today, now):
    """Все метрики отчёта за период + текущее состояние (ТЗ гл.9)."""
    cases = CuratorCase.objects.all()
    if curator:
        cases = cases.filter(curator=curator)
    plans = TreatmentPlan.objects.filter(cases__in=cases).distinct()
    live_plans = plans.exclude(cases__status=CuratorCase.Status.LOST)

    # План и мотивация: месяцы, пересекающие период
    month_q = Q()
    for y, m in _period_months(start, end):
        month_q |= Q(year=y, month=m)
    mps = CuratorMonthlyPlan.objects.filter(month_q)
    mps = mps.filter(curator=curator) if curator else mps.filter(curator__role=CustomUser.Role.CURATOR)
    plan_sum = mps.aggregate(s=Coalesce(Sum("plan_amount"), Decimal("0")))["s"]
    motivation = sum((mp.total_payout for mp in mps), Decimal("0"))

    # Факт (К1) за период, отказы исключены
    agreed_qs = live_plans.filter(agreement_date__range=(start, end))
    fact = agreed_qs.aggregate(s=Coalesce(Sum("agreed_sum"), Decimal("0")))["s"]
    agreed_count = agreed_qs.count()

    # Воронка за период по датам событий
    transferred = cases.filter(created_at__date__range=(start, end)).count()
    presented_qs = plans.filter(presentation_date__range=(start, end))
    presented = presented_qs.count()
    presentation_sum = presented_qs.aggregate(s=Coalesce(Sum("presentation_sum"), Decimal("0")))["s"]
    in_progress = cases.filter(
        status=CuratorCase.Status.IN_PROGRESS, status_changed_at__date__range=(start, end)
    ).count()
    completed = cases.filter(
        status=CuratorCase.Status.COMPLETED, status_changed_at__date__range=(start, end)
    ).count()
    lost = cases.filter(status=CuratorCase.Status.LOST, lost_date__range=(start, end)).count()

    def conv(a, b):
        return round(a / b * 100, 1) if b else 0

    conversions = {
        "transfer_present": conv(presented, transferred),
        "present_agree": conv(agreed_count, presented),
        "agree_progress": conv(in_progress, agreed_count),
        "progress_done": conv(completed, in_progress),
    }
    avg_check = (fact / agreed_count) if agreed_count else Decimal("0")

    payments_qs = Payment.objects.filter(plan__in=plans, payment_date__range=(start, end))
    payments_sum = payments_qs.aggregate(s=Coalesce(Sum("amount"), Decimal("0")))["s"]
    advances_sum = payments_qs.filter(is_advance=True).aggregate(s=Coalesce(Sum("amount"), Decimal("0")))["s"]

    workorders_qs = WorkOrder.objects.filter(plan__in=plans, closed_date__range=(start, end))
    workorders_sum = workorders_qs.aggregate(s=Coalesce(Sum("amount"), Decimal("0")))["s"]
    workorders_count = workorders_qs.count()

    tasks_created = Task.objects.filter(case__in=cases, created_at__date__range=(start, end)).count()
    tasks_done = Task.objects.filter(
        case__in=cases, status=Task.TaskStatus.DONE, completed_at__date__range=(start, end)
    ).count()

    # Текущее состояние (на сегодня), не за период
    active_now = cases.filter(status__in=ACTIVE_STATUSES).count()
    remainder_now = sum((p.remaining for p in live_plans.filter(agreement_date__isnull=False)), Decimal("0"))
    overdue_now = Task.objects.filter(case__in=cases, status=Task.TaskStatus.PENDING, due_date__lt=now).count()
    no_next_now = (
        cases.filter(status__in=ACTIVE_STATUSES)
        .annotate(f=Count("tasks", filter=Q(tasks__status=Task.TaskStatus.PENDING, tasks__due_date__date__gte=today)))
        .filter(f=0).count()
    )

    return {
        "plan": plan_sum, "fact": fact,
        "percent": round(fact / plan_sum * 100, 1) if plan_sum else 0,
        "conversions": conversions, "avg_check": avg_check,
        "presentation_sum": presentation_sum, "agreed_sum": fact,
        "advances": advances_sum, "payments": payments_sum,
        "workorders_sum": workorders_sum, "workorders_count": workorders_count,
        "tasks_created": tasks_created, "tasks_done": tasks_done,
        "motivation": motivation,
        "funnel": {"transferred": transferred, "presented": presented, "agreed": agreed_count,
                   "in_progress": in_progress, "completed": completed, "lost": lost},
        "now": {"active": active_now, "remainder": remainder_now,
                "overdue": overdue_now, "no_next": no_next_now},
    }


@manager_required
def manager_report_export(request):
    """Табличная выгрузка отчёта (CSV с BOM и ';' — открывается в Excel)."""
    now = timezone.now()
    today = now.date()
    start, end = _resolve_period(request, today)

    curator_id = request.GET.get("curator", "")
    curators = CustomUser.objects.filter(role=CustomUser.Role.CURATOR).order_by("last_name", "first_name")
    if curator_id:
        curators = curators.filter(pk=curator_id)

    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="report_{start:%Y-%m-%d}_{end:%Y-%m-%d}.csv"'
    writer = csv.writer(response, delimiter=";", lineterminator="\n")

    writer.writerow(["Отчёт руководителя", f"{start:%d.%m.%Y} — {end:%d.%m.%Y}"])
    writer.writerow([])
    writer.writerow([
        "Куратор", "План", "Факт К1", "%", "Ср. чек",
        "Передано", "Презентовано", "Согласовано", "В лечении", "Завершено", "Отказ",
        "Конв. перед→през", "Конв. през→согл", "Конв. согл→леч", "Конв. леч→зав",
        "Презентации ₽", "Согласования ₽", "Авансы ₽", "Оплаты ₽",
        "Наряды ₽", "Наряды шт", "Задачи созд", "Задачи вып", "Мотивация ₽",
        "Активных (тек)", "Остаток (тек)", "Просрочено (тек)", "Без след. действия (тек)",
    ])

    def write_row(name, m):
        c = m["conversions"]
        f = m["funnel"]
        n = m["now"]
        writer.writerow([
            name, m["plan"], m["fact"], m["percent"], m["avg_check"],
            f["transferred"], f["presented"], f["agreed"], f["in_progress"], f["completed"], f["lost"],
            c["transfer_present"], c["present_agree"], c["agree_progress"], c["progress_done"],
            m["presentation_sum"], m["agreed_sum"], m["advances"], m["payments"],
            m["workorders_sum"], m["workorders_count"], m["tasks_created"], m["tasks_done"], m["motivation"],
            n["active"], n["remainder"], n["overdue"], n["no_next"],
        ])

    write_row("ОТДЕЛ", compute_curator_metrics(None, start, end, today, now))
    for u in curators:
        write_row(u.get_full_name() or u.username, compute_curator_metrics(u, start, end, today, now))

    return response

@login_required
def doc_checks(request):
    """Реестр проверки планов и документов (ТЗ гл.9)."""
    status_filter = request.GET.get("check_status", "")
    curator_id = request.GET.get("curator", "")

    cases = CuratorCase.objects.filter(
        status__in=[CuratorCase.Status.AGREED, CuratorCase.Status.IN_PROGRESS]
    ).select_related("patient", "plan", "plan__doctor", "curator").prefetch_related("document_checks")
    if request.user.role == CustomUser.Role.CURATOR:
        cases = cases.filter(curator=request.user)
    elif curator_id:
        cases = cases.filter(curator_id=curator_id)

    rows = []
    for c in cases.order_by("-created_at"):
        p = c.plan
        rows.append({
            "case": c,
            "docs": docs_state(c),
            "check": c.document_checks.first(),
            "doctor": p.doctor,
            "plan_type": p.get_plan_type_display() or "—",
            "initial_sum": p.initial_sum,
            "presentation_sum": p.presentation_sum,
            "agreed_sum": p.agreed_sum,
            "advance": p.advance_paid,
            "schedule_ok": p.is_schedule_signed,
            "plan_signed": p.is_signed_by_patient,
            "contract": c.patient.contracts.filter(status=Contract.Status.ACTIVE).first(),
        })

    # Счётчики для чипов-фильтров
    counts = {"none": 0, "NOT_CHECKED": 0, "RECEIVED": 0, "CHECKED": 0, "DISCREPANCY": 0}
    for r in rows:
        counts[r["check"].status if r["check"] else "none"] += 1
    counts["total"] = len(rows)
    counts["unchecked"] = counts["none"] + counts["NOT_CHECKED"]

    # Фильтр по результату проверки
    if status_filter == "unchecked":
        rows = [r for r in rows if not r["check"] or r["check"].status == DocumentCheck.Status.NOT_CHECKED]
    elif status_filter:
        rows = [r for r in rows if r["check"] and r["check"].status == status_filter]

    history = DocumentCheck.objects.select_related(
        "case", "case__patient", "case__curator", "checked_by"
    ).order_by("-created_at")
    if request.user.role == CustomUser.Role.CURATOR:
        history = history.filter(case__curator=request.user)
    elif curator_id:
        history = history.filter(case__curator_id=curator_id)
    history = history[:200]

    return render(request, "crm/doc_checks.html", {
        "title": "Проверка документов",
        "rows": rows,
        "history": history,
        "counts": counts,
        "check_status_filter": status_filter,
        "curator_id": curator_id,
        "curators": CustomUser.objects.filter(role=CustomUser.Role.CURATOR).order_by("last_name"),
        "can_check": request.user.role in (CustomUser.Role.MANAGER, CustomUser.Role.ADMIN),
        "check_statuses": DocumentCheck.Status.choices,
        "discrepancies": DocumentCheck.Discrepancy.choices,
    })

def _resolve_period(request, today):
    """Пресеты периодов отчётов (ТЗ гл.9): день, неделя, месяц, квартал, произвольный."""
    period = request.GET.get("period", "month")

    if period == "day":
        return today, today

    if period == "week":
        start = today - timedelta(days=today.weekday())   # понедельник
        return start, start + timedelta(days=6)           # воскресенье

    if period == "quarter":
        q_start_month = (today.month - 1) // 3 * 3 + 1
        start = date(today.year, q_start_month, 1)
        end_month = q_start_month + 2
        # последний день последнего месяца квартала
        end = (date(today.year, end_month, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return start, end

    if period == "custom":
        start = parse_date(request.GET.get("from", "")) or today.replace(day=1)
        end = parse_date(request.GET.get("to", "")) or today
        if end < start:
            start, end = end, start
        return start, end

    # по умолчанию — текущий месяц, конец = сегодня
    return today.replace(day=1), today