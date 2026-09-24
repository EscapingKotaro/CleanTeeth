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
        "use_sidebar": True,
        # Данные для модалок
        "patients": Patient.objects.all().order_by("last_name")[:200],
        "plans": TreatmentPlan.objects.all().order_by("-id")[:200],
        "doctors": Doctor.objects.filter(is_active=True).order_by("last_name"),
        "curators": CustomUser.objects.filter(
            role__in=[CustomUser.Role.CURATOR, CustomUser.Role.SENIOR_CURATOR]
        ),
    })


@login_required
def case_detail(request, case_id):
    case = get_object_or_404(
        CuratorCase.objects.select_related("patient", "plan", "curator", "lost_reason"),
        id=case_id,
    )
    if request.user.role == CustomUser.Role.CURATOR and case.curator_id != request.user.id:
        return HttpResponseForbidden("У вас нет доступа к этому кейсу.")

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

from django.utils.dateparse import parse_date

@login_required
def patient_create(request):
    if request.method != "POST":
        return redirect("crm:cases_list")

    last_name = request.POST.get("last_name", "").strip()
    first_name = request.POST.get("first_name", "").strip()
    phone = request.POST.get("phone", "").strip()

    if not last_name or not first_name or not phone:
        messages.error(request, "Фамилия, имя и телефон обязательны.")
        return redirect("crm:cases_list")

    Patient.objects.create(
        last_name=last_name,
        first_name=first_name,
        middle_name=request.POST.get("middle_name", "").strip(),
        phone=phone,
        birth_date=parse_date(request.POST.get("birth_date", "")) or None,
        comment=request.POST.get("comment", "").strip(),
    )
    messages.success(request, f"Пациент {last_name} {first_name} создан.")
    return redirect("crm:cases_list")

@login_required
def patient_update(request, patient_id):
    if request.method == 'POST':
        patient = Patient.objects.get(id=patient_id)
        patient.last_name = request.POST.get('last_name')
        patient.first_name = request.POST.get('first_name')
        patient.middle_name=request.POST.get("middle_name")
        patient.phone = request.POST.get('phone')
        patient.comment = request.POST.get('comment', '')
        patient.birth_date=parse_date(request.POST.get("birth_date"))
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
    """
    Создаёт план лечения и ОДНОВРЕМЕННО кураторский кейс,
    связывая пациент + план + куратор (по умолчанию — тот, кто создаёт).
    """
    if request.method != "POST":
        return redirect("crm:cases_list")

    patient_id = request.POST.get("patient_id")
    if not patient_id:
        messages.error(request, "Не выбран пациент. План не создан.")
        return redirect("crm:cases_list")

    with transaction.atomic():
        # 1. Создаём план лечения
        plan = TreatmentPlan.objects.create(
            patient_id=patient_id,
            doctor_id=request.POST.get("doctor_id") or None,
            initial_sum=_to_decimal(request.POST.get("initial_sum")),
            presentation_sum=_to_decimal(request.POST.get("presentation_sum")),
            agreed_sum=_to_decimal(request.POST.get("agreed_sum")),
            plan_type=request.POST.get("plan_type", ""),
        )

        # 2. Куратор: по умолчанию тот, кто создаёт план
        curator_id = request.POST.get("curator_id") or request.user.id

        # 3. Создаём кейс и связываем всё вместе
        case = CuratorCase.objects.create(
            patient=plan.patient,
            plan=plan,
            curator_id=curator_id,
            status=CuratorCase.Status.TRANSFERRED,
        )

        # Автоматизация ТЗ №18: первая задача при создании кейса
        Task.objects.create(
            case=case,
            task_type=Task.TaskType.CONTACT,
            description="Связаться с пациентом и презентовать план лечения",
            due_date=timezone.now() + timedelta(days=1),
            assignee_id=curator_id,
            priority=Task.Priority.HIGH,
        )

    messages.success(request, f"План #{plan.id} и кейс #{case.id} созданы.")
    return redirect("crm:cases_list")


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
    case = get_object_or_404(
        CuratorCase.objects.select_related("plan", "curator", "patient"), id=case_id
    )
    if request.method != "POST":
        return redirect("crm:case_detail", case_id=case.id)

    new_status = request.POST.get("status")
    errors = _apply_status_transition(case, new_status, request.POST, request.user)

    if errors:
        for e in errors:
            messages.error(request, e)
    else:
        case.status = new_status
        case.save()
        messages.success(request, f"Статус изменён: «{case.get_status_display()}».")

    return redirect("crm:case_detail", case_id=case.id)


@login_required
def task_create(request, case_id):
    case = get_object_or_404(CuratorCase, id=case_id)
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
            messages.success(request, "Задача создана.")
    return redirect("crm:case_detail", case_id=case_id)


@login_required
def task_complete(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if request.method == "POST":
        task.status = Task.TaskStatus.DONE
        task.result = request.POST.get("result", "").strip()
        task.completed_at = timezone.now()
        task.save(update_fields=["status", "result", "completed_at"])
        messages.success(request, "Задача выполнена. Не забудьте создать следующий шаг.")
    return redirect("crm:case_detail", case_id=task.case_id)


@login_required
def task_postpone(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if request.method == "POST":
        new_dt = _parse_dt(request.POST.get("due_date"))
        if new_dt:
            task.due_date = new_dt
            task.save(update_fields=["due_date"])
            messages.success(request, "Задача перенесена.")
        else:
            messages.error(request, "Укажите новую дату и время.")
    return redirect("crm:case_detail", case_id=task.case_id)


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
        "directions": Direction.choices,
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