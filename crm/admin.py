from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    AuditLog,
    Contract,
    CuratorCase,
    CuratorMonthlyPlan,
    CustomUser,
    Doctor,
    LostReason,
    Patient,
    Payment,
    PaymentSchedule,
    PlanDirection,
    Task,
    TreatmentPlan,
)


# ============================================================
# ПОЛЬЗОВАТЕЛИ
# ============================================================

@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = (
        "username", "last_name", "first_name", "role", "phone",
        "is_active", "is_staff",
    )
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("username", "first_name", "last_name", "email", "phone")
    fieldsets = UserAdmin.fieldsets + (
        ("CRM-роль", {"fields": ("role", "phone", "position")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("CRM-роль", {"fields": ("role", "phone", "position")}),
    )


# ============================================================
# ПАЦИЕНТЫ И ДОГОВОРЫ
# ============================================================

@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ("id", "last_name", "first_name", "phone", "birth_date", "is_active", "created_at")
    list_display_links = ("id", "last_name", "first_name")
    search_fields = ("first_name", "last_name", "middle_name", "phone", "ident_patient_id")
    list_filter = ("is_active", "created_at")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ("id", "number", "date", "patient", "status", "created_at")
    list_display_links = ("id", "number")
    search_fields = ("number", "patient__first_name", "patient__last_name", "patient__phone")
    list_filter = ("status", "date")
    autocomplete_fields = ("patient",)
    list_select_related = ("patient",)


# ============================================================
# ВРАЧИ
# ============================================================

@admin.register(Doctor)
class DoctorAdmin(admin.ModelAdmin):
    list_display = (
        "id", "last_name", "first_name", "direction",
        "phone", "is_active", "ident_doctor_id",
    )
    list_display_links = ("id", "last_name", "first_name")
    search_fields = ("first_name", "last_name", "middle_name", "phone", "ident_doctor_id")
    list_filter = ("direction", "is_active")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        ("Основные данные", {"fields": ("last_name", "first_name", "middle_name", "direction")}),
        ("Контакты", {"fields": ("phone", "email")}),
        ("Статус", {"fields": ("is_active",)}),
        ("Интеграция", {"fields": ("ident_doctor_id",), "classes": ("collapse",)}),
        ("Прочее", {"fields": ("comment", "created_at", "updated_at"), "classes": ("collapse",)}),
    )


# ============================================================
# ПЛАНЫ ЛЕЧЕНИЯ + ФИНАНСЫ (инлайны)
# ============================================================

class PlanDirectionInline(admin.TabularInline):
    model = PlanDirection
    extra = 4  # четыре фиксированных направления
    fields = ("direction", "amount")


class PaymentScheduleInline(admin.TabularInline):
    model = PaymentSchedule
    extra = 0
    fields = ("due_date", "planned_amount", "paid_amount", "payment_date", "is_advance", "comment")


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("payment_date", "amount", "is_advance", "schedule_item", "comment")


@admin.register(TreatmentPlan)
class TreatmentPlanAdmin(admin.ModelAdmin):
    list_display = (
        "id", "patient", "doctor", "plan_type", "agreed_sum",
        "presentation_date", "agreement_date", "is_signed_by_patient", "is_ready_for_work",
    )
    list_display_links = ("id", "patient")
    search_fields = (
        "patient__first_name", "patient__last_name",
        "patient__phone", "doctor__last_name", "doctor__first_name",
    )
    list_filter = ("plan_type", "is_signed_by_patient", "is_schedule_signed", "agreement_date", "doctor")
    autocomplete_fields = ("patient", "doctor")
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("patient", "doctor")
    inlines = [PlanDirectionInline, PaymentScheduleInline, PaymentInline]

    fieldsets = (
        ("Участники", {"fields": ("patient", "doctor")}),
        ("Суммы", {"fields": ("initial_sum", "presentation_sum", "agreed_sum")}),
        ("Даты и статусы", {
            "fields": (
                "presentation_date", "presentation_result", "agreement_date",
                "is_signed_by_patient", "plan_type", "is_schedule_signed",
                "start_date", "end_date",
            ),
        }),
        ("Интеграция", {"fields": ("ident_treatment_plan_id",), "classes": ("collapse",)}),
        ("Системное", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(boolean=True, description="Готов к работе")
    def is_ready_for_work(self, obj):
        return obj.is_ready_for_work


@admin.register(PlanDirection)
class PlanDirectionAdmin(admin.ModelAdmin):
    list_display = ("plan", "direction", "amount")
    list_filter = ("direction",)
    autocomplete_fields = ("plan",)
    list_select_related = ("plan",)


@admin.register(PaymentSchedule)
class PaymentScheduleAdmin(admin.ModelAdmin):
    list_display = ("id", "plan", "due_date", "planned_amount", "paid_amount", "is_advance", "is_overdue")
    list_filter = ("is_advance", "due_date")
    search_fields = ("plan__patient__last_name", "plan__patient__first_name")
    autocomplete_fields = ("plan",)
    list_select_related = ("plan",)

    @admin.display(boolean=True, description="Просрочен")
    def is_overdue(self, obj):
        return obj.is_overdue


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "plan", "amount", "payment_date", "is_advance", "schedule_item")
    list_filter = ("is_advance", "payment_date")
    search_fields = ("plan__patient__last_name", "plan__patient__first_name")
    autocomplete_fields = ("plan", "schedule_item")
    date_hierarchy = "payment_date"
    list_select_related = ("plan", "schedule_item")


# ============================================================
# КЕЙСЫ И ЗАДАЧИ
# ============================================================

class TaskInline(admin.TabularInline):
    model = Task
    extra = 0
    fields = ("task_type", "description", "due_date", "assignee", "priority", "status")


@admin.register(CuratorCase)
class CuratorCaseAdmin(admin.ModelAdmin):
    list_display = (
        "id", "patient", "plan", "curator", "status",
        "lost_date", "created_at",
    )
    list_display_links = ("id", "patient")
    search_fields = (
        "patient__first_name", "patient__last_name",
        "patient__phone", "curator__last_name",
    )
    list_filter = ("status", "curator", "created_at")
    autocomplete_fields = ("patient", "plan", "curator")
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("patient", "plan", "curator")
    inlines = [TaskInline]

    fieldsets = (
        ("Участники", {"fields": ("patient", "plan", "curator")}),
        ("Воронка", {"fields": ("status", "decision_reason", "decision_comment", "next_control_date", "completed_at")}),
        ("Закрывающий исход (Отказ / потерян)", {
            "fields": ("lost_date", "lost_reason", "lost_potential_sum", "lost_comment"),
            "classes": ("collapse",),
        }),
        ("Системное", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )


@admin.register(LostReason)
class LostReasonAdmin(admin.ModelAdmin):
    list_display = ("title", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("id", "case", "task_type", "due_date", "assignee", "priority", "status", "is_overdue")
    list_filter = ("task_type", "status", "priority", "due_date")
    search_fields = ("description", "case__patient__last_name")
    autocomplete_fields = ("case", "assignee")
    list_select_related = ("case", "assignee")

    @admin.display(boolean=True, description="Просрочена")
    def is_overdue(self, obj):
        return obj.is_overdue


# ============================================================
# МОТИВАЦИЯ
# ============================================================

@admin.register(CuratorMonthlyPlan)
class CuratorMonthlyPlanAdmin(admin.ModelAdmin):
    list_display = (
        "curator", "period_label", "plan_amount", "adjustments",
        "workorders_amount", "work_days", "fact_amount", "completion_percent", "total_payout",
    )
    list_filter = ("year", "month", "curator")
    search_fields = ("curator__last_name", "curator__first_name")
    autocomplete_fields = ("curator", "set_by")
    list_select_related = ("curator",)
    ordering = ("-year", "-month", "curator__last_name")
    readonly_fields = ("created_at", "updated_at", "fact_amount", "kpl_for_calc", "total_bonus", "total_payout")

    fieldsets = (
        (None, {"fields": ("curator", "year", "month", "set_by")}),
        ("Показатели месяца", {"fields": ("plan_amount", "adjustments", "workorders_amount", "work_days")}),
        ("Расчёт (автоматически)", {"fields": ("fact_amount", "kpl_for_calc", "total_bonus", "total_payout")}),
        ("Системное", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Факт (К1)")
    def fact_amount(self, obj):
        return obj.fact_amount

    @admin.display(description="Выполнение")
    def completion_percent(self, obj):
        return f"{obj.completion_percent}%"

    @admin.display(description="К выплате")
    def total_payout(self, obj):
        return obj.total_payout


# ============================================================
# АУДИТ (только чтение)
# ============================================================

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "model_name", "object_repr", "field_name", "old_value", "new_value")
    list_filter = ("action", "model_name", "created_at")
    search_fields = ("object_repr", "actor__username", "old_value", "new_value")
    readonly_fields = [f.name for f in AuditLog._meta.fields]
    date_hierarchy = "created_at"
    list_select_related = ("actor",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False