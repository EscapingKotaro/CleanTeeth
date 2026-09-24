from django.db import models
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError


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
    """Договор на обслуживание пациента с клиникой в целом."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Проект"
        ACTIVE = "ACTIVE", "Действующий"
        CLOSED = "CLOSED", "Закрыт"

    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="contracts", verbose_name="Пациент"
    )
    number = models.CharField("Номер договора", max_length=50)
    date = models.DateField("Дата договора")
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Договор на обслуживание"
        verbose_name_plural = "Договоры на обслуживание"
        ordering = ["-date"]
        unique_together = ("patient", "number")

    def __str__(self):
        return f"Договор №{self.number} от {self.date} ({self.patient})"


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



import calendar
from datetime import date
from decimal import Decimal


class CuratorMonthlyPlan(models.Model):
    """Индивидуальный месячный план куратора (задаёт управляющая).
    Факт (К1) считается динамически по согласованным планам."""

    # ============================================================
    # КОНФИГ МОТИВАЦИИ (пока константы-заглушки).
    # По ТЗ формулы бонусов желательно вынести в настраиваемую
    # конфигурацию. Реальные формулы берём из Google-файла заказчика.
    # ============================================================
    BONUS_1_THRESHOLD = Decimal("80")    # % выполнения
    BONUS_1_AMOUNT = Decimal("5000")     # ₽ — ЗАГЛУШКА, согласовать!
    BONUS_2_THRESHOLD = Decimal("100")
    BONUS_2_AMOUNT = Decimal("10000")    # ЗАГЛУШКА
    BONUS_3_THRESHOLD = Decimal("120")
    BONUS_3_AMOUNT = Decimal("15000")    # ЗАГЛУШКА

    curator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="monthly_plans",
        verbose_name="Куратор",
        limit_choices_to={"role__in": [CustomUser.Role.CURATOR, CustomUser.Role.SENIOR_CURATOR]},
    )
    year = models.PositiveIntegerField("Год")
    month = models.PositiveIntegerField("Месяц")  # 1-12

    plan_amount = models.DecimalField(
        "Индивидуальный план (₽)", max_digits=12, decimal_places=2, default=0
    )
    adjustments = models.DecimalField(
        "Корректировки (₽)", max_digits=12, decimal_places=2, default=0
    )
    workorders_amount = models.DecimalField(
        "Заказ-наряды (₽)", max_digits=12, decimal_places=2, default=0
    )
    comment = models.TextField("Комментарий", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Месячный план куратора"
        verbose_name_plural = "Месячные планы кураторов"
        unique_together = ("curator", "year", "month")
        ordering = ["-year", "-month"]

    def __str__(self):
        return f"{self.curator} · {self.month:02d}.{self.year} · план {self.plan_amount} ₽"

    # ------------------------------------------------------------
    # ФАКТ (К1): сумма согласованных планов куратора за этот месяц
    # ------------------------------------------------------------
    @property
    def fact_amount(self):
        from django.db.models import Sum
        plan_ids = (
            CuratorCase.objects.filter(
                curator=self.curator,
                plan__agreement_date__year=self.year,
                plan__agreement_date__month=self.month,
            )
            .exclude(status=CuratorCase.Status.LOST)
            .values_list("plan_id", flat=True)
            .distinct()
        )
        total = TreatmentPlan.objects.filter(id__in=plan_ids).aggregate(
            s=Sum("agreed_sum")
        )["s"]
        return total or Decimal("0")

    @property
    def completion_percent(self):
        """Процент выполнения: факт / план × 100%"""
        if not self.plan_amount:
            return Decimal("0")
        return round(self.fact_amount / self.plan_amount * 100, 1)

    @property
    def remaining(self):
        """Остаток до плана"""
        return max(self.plan_amount - self.fact_amount, Decimal("0"))

    @property
    def days_in_month(self):
        return calendar.monthrange(self.year, self.month)[1]

    @property
    def is_current_month(self):
        today = date.today()
        return today.year == self.year and today.month == self.month

    @property
    def pace_percent(self):
        """Темп относительно текущей даты месяца.
        100% = идём ровно по графику."""
        if not self.plan_amount:
            return Decimal("0")
        today = date.today()
        if self.is_current_month:
            elapsed_days = today.day
        elif date(self.year, self.month, 1) < today:
            elapsed_days = self.days_in_month  # месяц уже прошёл
        else:
            return Decimal("0")  # месяц ещё не начался
        expected = self.plan_amount * elapsed_days / self.days_in_month
        if not expected:
            return Decimal("0")
        return round(self.fact_amount / expected * 100, 1)

    @property
    def forecast(self):
        """Линейный прогноз выполнения на конец месяца"""
        today = date.today()
        if not self.is_current_month or today.day == 0:
            return self.fact_amount
        return round(self.fact_amount / today.day * self.days_in_month, 2)

    # ------------------------------------------------------------
    # БОНУСЫ (заглушки пороговой модели — заменить формулами из
    # Google-файла заказчика, когда получим)
    # ------------------------------------------------------------
    @property
    def bonus_1(self):
        return self.BONUS_1_AMOUNT if self.completion_percent >= self.BONUS_1_THRESHOLD else Decimal("0")

    @property
    def bonus_2(self):
        return self.BONUS_2_AMOUNT if self.completion_percent >= self.BONUS_2_THRESHOLD else Decimal("0")

    @property
    def bonus_3(self):
        return self.BONUS_3_AMOUNT if self.completion_percent >= self.BONUS_3_THRESHOLD else Decimal("0")

    @property
    def total_bonus(self):
        return self.bonus_1 + self.bonus_2 + self.bonus_3

    @property
    def total_payout(self):
        """Итог к выплате = бонусы + корректировки.
        ⚠️ Допущение — уточнить у заказчика формулу из Google-файла."""
        return self.total_bonus + self.adjustments

    @property
    def period_label(self):
        months = [
            "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
            "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
        ]
        return f"{months[self.month]} {self.year}"


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