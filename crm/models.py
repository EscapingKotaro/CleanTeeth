from django.db import models
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.utils.functional import cached_property
from .motivation import calculate_bonuses, rub, DAILY_RATE
from .views import compute_k1

# ============================================================
# НАПРАВЛЕНИЯ ЛЕЧЕНИЯ (единый справочник)
# ============================================================

class Direction(models.TextChoices):
    """Четыре фиксированных направления лечения"""
    THERAPY = "THERAPY", "Терапия"
    IMPLANTATION = "IMPLANT", "Имплантация и хирургия"
    ORTHOPEDICS = "ORTHO", "Ортопедия"
    ORTHODONTICS = "ORTHO_D", "Ортодонтия"



# ============================================================
# ПОЛЬЗОВАТЕЛЬ
# ============================================================

class CustomUser(AbstractUser):
    """Кастомный пользователь системы с ролями."""

    class Role(models.TextChoices):
        CURATOR = "CURATOR", "Куратор"
        SENIOR_CURATOR = "SENIOR_CURATOR", "Старший куратор"
        MANAGER = "MANAGER", "Управляющая"
        ADMIN = "ADMIN", "Админ"

    role = models.CharField(
        "Роль",
        max_length=20,
        choices=Role.choices,
        default=Role.CURATOR,
    )
    phone = models.CharField("Телефон", max_length=20, blank=True)
    position = models.CharField("Должность", max_length=100, blank=True)

    class Meta:
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def is_curator(self):
        return self.role in (self.Role.CURATOR, self.Role.SENIOR_CURATOR)
    
    def save(self, *args, **kwargs):
        from .audit import log_action, get_current_user

        is_new = self._state.adding
        old = None
        if not is_new and self.pk:
            old = CustomUser.objects.filter(pk=self.pk).values(
                "role", "is_active"
            ).first()

        super().save(*args, **kwargs)

        # --- Аудит ---
        if is_new:
            log_action(
                action=AuditLog.Action.CREATE,
                instance=self,
                field_name="role",
                new_value=self.get_role_display(),
                comment="Создан пользователь",
            )
        elif old:
            if old["role"] != self.role:
                log_action(
                    action=AuditLog.Action.ROLE_CHANGE,
                    instance=self,
                    field_name="role",
                    old_value=old["role"],
                    new_value=self.role,
                    comment="Смена роли",
                )
            if old["is_active"] != self.is_active:
                act = AuditLog.Action.ACTIVATE if self.is_active else AuditLog.Action.DEACTIVATE
                log_action(
                    action=act,
                    instance=self,
                    field_name="is_active",
                    old_value=str(old["is_active"]),
                    new_value=str(self.is_active),
                    comment="Деактивация" if not self.is_active else "Активация",
                )


class Doctor(models.Model):
    """Врач клиники — отдельная сущность, не обязательно пользователь CRM."""



    last_name = models.CharField("Фамилия", max_length=100)
    first_name = models.CharField("Имя", max_length=100)
    middle_name = models.CharField("Отчество", max_length=100, blank=True)
    phone = models.CharField("Телефон", max_length=20, blank=True)
    email = models.EmailField("Email", blank=True)

    direction = models.CharField(
        "Направление лечения",
        max_length=20,
        choices=Direction.choices,
    )

    is_active = models.BooleanField("Активен в клинике", default=True)

    # Интеграция с iDent
    ident_doctor_id = models.CharField(
        "ID врача в iDent", max_length=50, blank=True, null=True, unique=True
    )

    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Врач"
        verbose_name_plural = "Врачи"
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.last_name} {self.first_name} {self.middle_name}".strip()


# ============================================================
# ПАЦИЕНТ / ДОГОВОР / ПЛАН ЛЕЧЕНИЯ (из прошлой итерации)
# ============================================================

class Patient(models.Model):
    """Постоянная карточка человека"""
    first_name = models.CharField("Имя", max_length=100)
    last_name = models.CharField("Фамилия", max_length=100)
    middle_name = models.CharField("Отчество", max_length=100, blank=True)
    phone = models.CharField("Телефон", max_length=20, unique=True)
    birth_date = models.DateField("Дата рождения", null=True, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    ident_patient_id = models.CharField(
        "ID в iDent", max_length=50, blank=True, null=True, unique=True
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)
    is_active = models.BooleanField("Активен", default=True)

    class Meta:
        verbose_name = "Пациент"
        verbose_name_plural = "Пациенты"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.last_name} {self.first_name} {self.middle_name}".strip()


class Contract(models.Model):
    """Договор пациента с клиникой в целом. Один действующий покрывает несколько планов."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Черновик"
        ACTIVE = "ACTIVE", "Действующий"
        CLOSED = "CLOSED", "Закрыт"

    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="contracts", verbose_name="Пациент"
    )
    number = models.CharField("Номер договора", max_length=64)
    date = models.DateField("Дата договора")
    status = models.CharField("Статус", max_length=16, choices=Status.choices, default=Status.DRAFT)
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Договор на обслуживание"
        verbose_name_plural = "Договоры на обслуживание"
        ordering = ["-date"]
        unique_together = ("patient", "number")

    def __str__(self):
        return f"Договор №{self.number} от {self.date:%d.%m.%Y} · {self.patient}"



class TreatmentPlan(models.Model):
    """Конкретный план лечения пациента."""

    class PlanType(models.TextChoices):
        WITH_ADVANCE = "ADVANCE", "План с авансом"
        WITHOUT_ADVANCE = "FACT", "Без аванса / лечение по факту"

    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="plans", verbose_name="Пациент"
    )
    doctor = models.ForeignKey(
    Doctor,
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name="treatment_plans",
    verbose_name="Лечащий врач",
)
    initial_sum = models.DecimalField("Первоначальная сумма", max_digits=12, decimal_places=2, default=0)
    presentation_sum = models.DecimalField("Сумма презентации", max_digits=12, decimal_places=2, default=0)
    agreed_sum = models.DecimalField("Согласованная сумма", max_digits=12, decimal_places=2, default=0)
    presentation_date = models.DateField("Дата презентации", null=True, blank=True)
    agreement_date = models.DateField("Дата согласования", null=True, blank=True)
    presentation_result = models.TextField("Результат презентации", blank=True)
    is_signed_by_patient = models.BooleanField("План подписан пациентом", default=False)
    plan_type = models.CharField("Тип плана", max_length=20, choices=PlanType.choices, blank=True)
    start_date = models.DateField("Дата начала лечения", null=True, blank=True)
    end_date = models.DateField("Дата завершения", null=True, blank=True)
    ident_treatment_plan_id = models.CharField(
        "ID плана в iDent", max_length=50, blank=True, null=True, unique=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # В поля модели:
    is_schedule_signed = models.BooleanField("График платежей подписан", default=False)

    # --- Финансовые свойства ---
    @property
    def advance_paid(self):
        """Сумма внесённых авансов (аванс входит в график — п.10)."""
        from django.db.models import Sum, Q
        return self.payments.filter(
            Q(is_advance=True) | Q(schedule_item__is_advance=True)
        ).aggregate(total=Sum("amount"))["total"] or 0

    @property
    def total_paid(self):
        """Все фактические оплаты по плану."""
        from django.db.models import Sum
        return self.payments.aggregate(total=Sum("amount"))["total"] or 0

    @property
    def remaining(self):
        """Остаток = согласованная сумма - оплачено."""
        return (self.agreed_sum or 0) - self.total_paid

    @property
    def has_advance(self):
        return self.advance_paid > 0

    # --- Направления: контроль суммы (п.5.1) ---
    @property
    def directions_total(self):
        from django.db.models import Sum
        return self.directions.aggregate(total=Sum("amount"))["total"] or 0

    @property
    def directions_mismatch(self):
        """True, если сумма направлений не сходится с согласованной суммой."""
        if not self.directions.exists():
            return False
        return self.directions_total != (self.agreed_sum or 0)

    # --- Чек-лист «план готов к работе» (п.5.3) ---
    @property
    def is_ready_for_work(self):
        if not self.agreement_date or not self.is_signed_by_patient or not self.plan_type:
            return False
        if self.plan_type == self.PlanType.WITH_ADVANCE:
            # План с авансом: нужен аванс + подписанный график
            return self.has_advance and self.is_schedule_signed
        # Лечение по факту: согласован + подписан
        return True

    @property
    def schedule_total(self):
        """Сумма всех пунктов графика."""
        from django.db.models import Sum
        return self.schedule.aggregate(total=Sum("planned_amount"))["total"] or 0

    @property
    def schedule_mismatch(self):
        """Согласованное правило: сумма графика обязана равняться согласованной сумме."""
        if not self.schedule.exists():
            return False
        return self.schedule_total != (self.agreed_sum or 0)

    class Meta:
        verbose_name = "План лечения"
        verbose_name_plural = "Планы лечения"
        ordering = ["-created_at"]

    def __str__(self):
        return f"План #{self.id} — {self.patient} ({self.agreed_sum} ₽)"



class LostReason(models.Model):
    """Структурированная причина отказа (справочник, редактируется в админке)."""
    title = models.CharField("Причина", max_length=255)
    sort_order = models.PositiveIntegerField("Порядок сортировки", default=0)
    is_active = models.BooleanField("Активна", default=True)

    class Meta:
        verbose_name = "Причина отказа"
        verbose_name_plural = "Причины отказа (справочник)"
        ordering = ["sort_order", "title"]

    def __str__(self):
        return self.title
# ============================================================
# КУРАТОРСКИЙ КЕЙС
# ============================================================

class CuratorCase(models.Model):
    """Объект движения по CRM-воронке.
    Связка: Пациент + План лечения + Куратор.
    """

    class Status(models.TextChoices):
        TRANSFERRED = "TRANSFERRED", "Передан куратору"
        PRESENTED = "PRESENTED", "План презентован"
        IN_DECISION = "IN_DECISION", "Решение в работе"
        AGREED = "AGREED", "План согласован"
        IN_PROGRESS = "IN_PROGRESS", "Лечение в процессе"
        COMPLETED = "COMPLETED", "Лечение завершено"
        LOST = "LOST", "Отказ / потерян"

    FUNNEL_ORDER = [
        Status.TRANSFERRED,
        Status.PRESENTED,
        Status.IN_DECISION,
        Status.AGREED,
        Status.IN_PROGRESS,
        Status.COMPLETED,
    ]
    NEXT_STATUSES = {
        Status.TRANSFERRED: [Status.PRESENTED],
        Status.PRESENTED: [Status.IN_DECISION, Status.AGREED],   # ← развилка
        Status.IN_DECISION: [Status.AGREED],
        Status.AGREED: [Status.IN_PROGRESS],
        Status.IN_PROGRESS: [Status.COMPLETED],
        Status.COMPLETED: [],
        Status.LOST: [],
    }

    patient = models.ForeignKey(
        Patient,
        on_delete=models.CASCADE,
        related_name="cases",
        verbose_name="Пациент",
    )
    plan = models.ForeignKey(
        TreatmentPlan,
        on_delete=models.CASCADE,
        related_name="cases",
        verbose_name="План лечения",
    )
    curator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="curated_cases",
        verbose_name="Ответственный куратор",
        limit_choices_to={"role__in": [CustomUser.Role.CURATOR, CustomUser.Role.SENIOR_CURATOR]},
    )

    status = models.CharField(
        "Статус воронки",
        max_length=20,
        choices=Status.choices,
        default=Status.TRANSFERRED,
    )

    # Статус «Решение в работе»
    decision_reason = models.CharField("Почему решение не принято", max_length=255, blank=True)
    decision_comment = models.TextField("Комментарий к решению", blank=True)

    # Статус «Лечение в процессе»
    next_control_date = models.DateField("Следующая контрольная дата", null=True, blank=True)

    # Статус «Лечение завершено»
    completed_at = models.DateField("Дата закрытия кейса", null=True, blank=True)

    # --- Поля для закрывающего исхода "Отказ / потерян" ---
    lost_date = models.DateField("Дата отказа", null=True, blank=True)
    lost_reason = models.ForeignKey(
        LostReason,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="cases",
        verbose_name="Причина отказа",
    )
    lost_potential_sum = models.DecimalField(
        "Сумма потенциального плана",
        max_digits=12, decimal_places=2,
        null=True, blank=True,
    )
    lost_comment = models.TextField("Комментарий к отказу", blank=True)

    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Кураторский кейс"
        verbose_name_plural = "Кураторские кейсы"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Кейс #{self.id} | {self.patient} | {self.get_status_display()}"

    def clean(self):
        """Валидация: пациент в кейсе должен совпадать с пациентом в плане."""
        if self.patient_id and self.plan_id and self.patient_id != self.plan.patient_id:
            raise ValidationError("Пациент в кейсе должен совпадать с пациентом в плане лечения.")

        if self.status == self.Status.LOST and not self.lost_date:
            raise ValidationError("Для статуса «Отказ / потерян» обязательно укажите дату отказа.")

    @property
    def is_active(self):
        return self.status not in (self.Status.COMPLETED, self.Status.LOST)

    @property
    def has_future_tasks(self):
        from django.utils import timezone
        return self.tasks.filter(
            status=Task.TaskStatus.PENDING,
            due_date__gte=timezone.now(),
        ).exists()

    @property
    def continuity_broken(self):
        """Активный кейс без будущей задачи = отклонение (правило непрерывности)."""
        return self.is_active and not self.has_future_tasks

    @classmethod
    def funnel_choices(cls):
        """Статусы воронки без закрывающего исхода."""
        return [c for c in cls.Status.choices if c[0] != cls.Status.LOST]

    @property
    def next_statuses(self):
        """Список статусов, в которые можно перейти из текущего."""
        return self.NEXT_STATUSES.get(self.status, [])



import calendar
from datetime import date
from decimal import Decimal


class CuratorMonthlyPlan(models.Model):
    """Индивидуальный месячный план куратора. Задаётся управляющей (ТЗ п.13)."""

    curator = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, related_name="monthly_plans", verbose_name="Куратор"
    )
    year = models.PositiveIntegerField("Год")
    month = models.PositiveIntegerField("Месяц")  # 1–12
    plan_amount = models.DecimalField("Индивидуальный план, ₽", max_digits=12, decimal_places=2, default=0)
    adjustments = models.DecimalField(
        "Корректировки (возвраты по КПЛ / мат. капитал), ₽",
        max_digits=12, decimal_places=2, default=0,
    )
    workorders_amount = models.DecimalField(
        "Итого заказ-нарядов, ₽", max_digits=12, decimal_places=2, default=0,
    )
    work_days = models.PositiveIntegerField("Рабочих дней", default=0)
    set_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="plans_set", verbose_name="Кто назначил"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Месячный план куратора"
        verbose_name_plural = "Месячные планы кураторов"
        unique_together = ("curator", "year", "month")
        ordering = ["-year", "-month"]

    def __str__(self):
        return f"{self.curator} · {self.month:02d}.{self.year} · план {self.plan_amount}"

    # ---------- ФАКТ (К1) ----------
    @property
    def fact_amount(self):
        """К1 = сумма согласованных планов куратора за этот месяц (ТЗ п.11)."""
        return compute_k1(self.curator, self.year, self.month)

    @property
    def completion_percent(self):
        if not self.plan_amount:
            return 0
        return round(self.fact_amount / self.plan_amount * 100, 1)

    @property
    def remaining(self):
        return self.plan_amount - self.fact_amount

    # ---------- ТЕМП И ПРОГНОЗ (ТЗ п.13) ----------
    @property
    def is_current(self):
        today = timezone.now().date()
        return self.year == today.year and self.month == today.month

    @property
    def period_label(self):
        """Период в формате '09.2026' — для админки и шаблонов."""
        return f"{self.month:02d}.{self.year}"
    @property
    def pace_percent(self):
        """Темп: факт к тому, что должно было быть к сегодняшнему дню."""
        if not self.is_current or not self.plan_amount:
            return None
        today = timezone.now().date()
        days_in_month = calendar.monthrange(self.year, self.month)[1]
        expected_by_now = self.plan_amount * (today.day / days_in_month)
        if not expected_by_now:
            return None
        return round(self.fact_amount / expected_by_now * 100, 1)

    @property
    def forecast(self):
        """Прогноз выполнения к концу месяца по текущему темпу."""
        if not self.is_current:
            return None
        today = timezone.now().date()
        if today.day == 0 or not self.fact_amount:
            return None
        days_in_month = calendar.monthrange(self.year, self.month)[1]
        return round(self.fact_amount / today.day * days_in_month, 2)

    # ---------- БОНУСЫ (ТЗ п.14) ----------
    @cached_property
    def kpl_for_calc(self):
        """Итого КПЛ для расчёта = К1 + корректировки (возвраты)."""
        return (self.fact_amount or 0) + (self.adjustments or 0)

    @cached_property
    def bonuses_raw(self):
        return calculate_bonuses(self)

    @property
    def bonus_1(self):
        return rub(self.bonuses_raw[0])

    @property
    def bonus_2(self):
        return rub(self.bonuses_raw[1])

    @property
    def bonus_3(self):
        return rub(self.bonuses_raw[2])

    @property
    def total_bonus(self):
        # как в файле: итог из НЕокруглённых бонусов, потом округление
        return rub(sum(self.bonuses_raw))

    @property
    def daily_component(self):
        """Компонент выплаты за рабочие дни."""
        return (self.work_days or 0) * DAILY_RATE

    @property
    def total_payout(self):
        return self.total_bonus + self.daily_component


from datetime import datetime, timedelta



class Task(models.Model):
    """Операционное действие по кейсу. НЕ является статусом воронки."""

    class TaskType(models.TextChoices):
        CONTACT = "CONTACT", "Связаться с пациентом"
        ADVANCE = "ADVANCE", "Получить аванс"
        DOCUMENTS = "DOCUMENTS", "Оформить документы"
        VISIT_REMINDER = "VISIT_REMINDER", "Напомнить о визите"
        PAYMENT_REMINDER = "PAYMENT_REMINDER", "Напомнить об оплате"
        NEXT_STAGE = "NEXT_STAGE", "Проконтролировать следующий этап"
        DELIVERY = "DELIVERY", "Сдача работы"
        FEEDBACK = "FEEDBACK", "Получить обратную связь"
        OTHER = "OTHER", "Другое"

    class TaskStatus(models.TextChoices):
        PENDING = "PENDING", "В ожидании"
        DONE = "DONE", "Выполнена"
        CANCELLED = "CANCELLED", "Отменена"

    class Priority(models.TextChoices):
        LOW = "1", "Низкий"
        MEDIUM = "2", "Средний"
        HIGH = "3", "Высокий"
        CRITICAL = "4", "Критичный"

    case = models.ForeignKey(
        "CuratorCase", on_delete=models.CASCADE, related_name="tasks", verbose_name="Кейс"
    )
    task_type = models.CharField(
        "Тип задачи", max_length=30, choices=TaskType.choices, default=TaskType.CONTACT
    )
    description = models.TextField("Описание", blank=True)
    due_date = models.DateTimeField("Дата и время")
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="tasks",
        verbose_name="Ответственный",
    )
    priority = models.CharField(
        "Приоритет", max_length=10, choices=Priority.choices, default=Priority.MEDIUM
    )
    status = models.CharField(
        "Статус", max_length=20, choices=TaskStatus.choices, default=TaskStatus.PENDING
    )
    comment = models.TextField("Комментарий", blank=True)
    result = models.TextField("Результат", blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    completed_at = models.DateTimeField("Выполнена", null=True, blank=True)
    schedule_item = models.ForeignKey(
        "PaymentSchedule", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tasks", verbose_name="Пункт графика"
    )

    class Meta:
        verbose_name = "Задача"
        verbose_name_plural = "Задачи"
        ordering = ["due_date"]

    def __str__(self):
        return f"{self.get_task_type_display()} · {self.case} · {self.due_date:%d.%m %H:%M}"

    @property
    def is_overdue(self):
        from django.utils import timezone
        return self.status == self.TaskStatus.PENDING and self.due_date < timezone.now()


class AuditLog(models.Model):
    """Журнал критичных действий (ТЗ п.17). Кто / когда / что / старое / новое."""

    class Action(models.TextChoices):
        CREATE = "CREATE", "Создание"
        UPDATE = "UPDATE", "Изменение"
        DELETE = "DELETE", "Удаление"
        ROLE_CHANGE = "ROLE_CHANGE", "Смена роли"
        ACTIVATE = "ACTIVATE", "Активация"
        DEACTIVATE = "DEACTIVATE", "Деактивация"
        PASSWORD_CHANGE = "PASSWORD_CHANGE", "Смена пароля"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="audit_actions",
        verbose_name="Кто выполнил",
    )
    action = models.CharField("Действие", max_length=30, choices=Action.choices)
    model_name = models.CharField("Сущность", max_length=100)
    object_id = models.CharField("ID объекта", max_length=64, blank=True)
    object_repr = models.CharField("Объект", max_length=255, blank=True)
    field_name = models.CharField("Поле", max_length=100, blank=True)
    old_value = models.TextField("Старое значение", blank=True)
    new_value = models.TextField("Новое значение", blank=True)
    comment = models.TextField("Комментарий", blank=True)
    ip_address = models.GenericIPAddressField("IP", null=True, blank=True)
    created_at = models.DateTimeField("Когда", auto_now_add=True)

    class Meta:
        verbose_name = "Запис аудита"
        verbose_name_plural = "Аудит (журнал действий)"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_action_display()} · {self.model_name} #{self.object_id} · {self.created_at:%d.%m.%Y %H:%M}"


class PlanDirection(models.Model):
    """Сумма по одному из 4 фиксированных направлений внутри плана."""
    plan = models.ForeignKey(
        TreatmentPlan, on_delete=models.CASCADE, related_name="directions", verbose_name="План"
    )
    direction = models.CharField("Направление", max_length=20, choices=Direction.choices)
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2, default=0)

    class Meta:
        verbose_name = "Направление плана"
        verbose_name_plural = "Направления плана"
        unique_together = ("plan", "direction")

    def __str__(self):
        return f"{self.plan} · {self.get_direction_display()} · {self.amount}"


class PaymentSchedule(models.Model):
    """Пункт графика платежей (обязателен для плана с авансом)."""
    plan = models.ForeignKey(
        TreatmentPlan, on_delete=models.CASCADE, related_name="schedule", verbose_name="План"
    )
    due_date = models.DateField("Дата платежа")
    planned_amount = models.DecimalField("Плановая сумма", max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField("Фактически оплачено", max_digits=12, decimal_places=2, default=0)
    payment_date = models.DateField("Дата оплаты", null=True, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    is_advance = models.BooleanField("Это аванс", default=False)

    class Meta:
        verbose_name = "Пункт графика платежей"
        verbose_name_plural = "График платежей"
        ordering = ["due_date"]

    def __str__(self):
        return f"{self.plan} · {self.due_date:%d.%m.%Y} · {self.planned_amount}"

    @property
    def remaining(self):
        return self.planned_amount - self.paid_amount

    @property
    def is_overdue(self):
        from django.utils import timezone
        return self.remaining > 0 and self.due_date < timezone.now().date()

    @property
    def is_paid(self):
        return self.remaining <= 0

    @property
    def has_payments(self):
        """Есть ли привязанные оплаты — защита от удаления."""
        return self.payments.exists()



class Payment(models.Model):
    """Фактическое движение денег по плану. Аванс — платёж с флагом."""
    plan = models.ForeignKey(
        TreatmentPlan, on_delete=models.CASCADE, related_name="payments", verbose_name="План"
    )
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)
    payment_date = models.DateField("Дата оплаты")
    is_advance = models.BooleanField("Это аванс", default=False)
    schedule_item = models.ForeignKey(
        PaymentSchedule, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payments", verbose_name="Пункт графика"
    )
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Платёж"
        verbose_name_plural = "Платежи"
        ordering = ["-payment_date"]

    def __str__(self):
        kind = "Аванс" if self.is_advance else "Оплата"
        return f"{kind} · {self.plan} · {self.amount} · {self.payment_date:%d.%m.%Y}"
