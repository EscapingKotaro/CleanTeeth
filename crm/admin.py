from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, Patient, Contract, TreatmentPlan, CuratorCase


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


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ("id", "last_name", "first_name", "phone", "birth_date", "created_at")
    list_display_links = ("id", "last_name", "first_name")
    search_fields = ("first_name", "last_name", "phone", "ident_patient_id")
    list_filter = ("created_at",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ("id", "number", "date", "patient", "status", "created_at")
    list_display_links = ("id", "number")
    search_fields = ("number", "patient__first_name", "patient__last_name", "patient__phone")
    list_filter = ("status", "date")
    autocomplete_fields = ("patient",)


from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, Patient, Contract, TreatmentPlan, CuratorCase, Doctor


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
        ("Основные данные", {
            "fields": ("last_name", "first_name", "middle_name", "direction"),
        }),
        ("Контакты", {
            "fields": ("phone", "email"),
        }),
        ("Статус", {
            "fields": ("is_active",),
        }),
        ("Интеграция", {
            "fields": ("ident_doctor_id",),
            "classes": ("collapse",),
        }),
        ("Прочее", {
            "fields": ("comment", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


@admin.register(TreatmentPlan)
class TreatmentPlanAdmin(admin.ModelAdmin):
    list_display = (
        "id", "patient", "doctor", "plan_type", "agreed_sum",
        "presentation_date", "agreement_date", "is_signed_by_patient",
    )
    list_display_links = ("id", "patient")
    search_fields = (
        "patient__first_name", "patient__last_name",
        "patient__phone", "doctor__last_name", "doctor__first_name",
    )
    list_filter = ("plan_type", "is_signed_by_patient", "agreement_date", "doctor")
    autocomplete_fields = ("patient", "doctor")  # doctor теперь указывает на Doctor
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        ("Участники", {
            "fields": ("patient", "doctor"),
        }),
        ("Суммы", {
            "fields": ("initial_sum", "presentation_sum", "agreed_sum"),
        }),
        ("Даты и статусы", {
            "fields": (
                "presentation_date",
                "agreement_date",
                "is_signed_by_patient",
                "plan_type",
                "start_date",
                "end_date",
            ),
        }),
        ("Системное", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


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

    fieldsets = (
        ("Участники", {
            "fields": ("patient", "plan", "curator"),
        }),
        ("Воронка", {
            "fields": ("status",),
        }),
        ("Закрывающий исход (Отказ / потерян)", {
            "fields": ("lost_date", "lost_reason", "lost_potential_sum", "lost_comment"),
            "classes": ("collapse",),
        }),
        ("Системное", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


from .models import CuratorMonthlyPlan

@admin.register(CuratorMonthlyPlan)
class CuratorMonthlyPlanAdmin(admin.ModelAdmin):
    list_display = (
        "curator", "period_label", "plan_amount", "adjustments",
        "workorders_amount", "fact_amount", "completion_percent",
    )
    list_filter = ("year", "month", "curator")
    search_fields = ("curator__last_name", "curator__first_name")
    autocomplete_fields = ("curator",)

    def fact_amount(self, obj):
        return obj.fact_amount
    fact_amount.short_description = "Факт (К1)"

    def completion_percent(self, obj):
        return f"{obj.completion_percent}%"
    completion_percent.short_description = "Выполнение"


from .models import Task, LostReason


@admin.register(LostReason)
class LostReasonAdmin(admin.ModelAdmin):
    list_display = ("title", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("id", "case", "task_type", "due_date", "assignee", "priority", "status")
    list_filter = ("task_type", "status", "priority")
    search_fields = ("description", "case__patient__last_name")
    autocomplete_fields = ("case", "assignee")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "model_name", "object_repr", "field_name", "old_value", "new_value")
    list_filter = ("action", "model_name", "created_at")
    search_fields = ("object_repr", "actor__username", "old_value", "new_value")
    readonly_fields = [f.name for f in AuditLog._meta.fields]  # журнал только для чтения
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False  # записи аудита нельзя создавать вручную

    def has_delete_permission(self, request, obj=None):
        return False  # и нельзя удалять